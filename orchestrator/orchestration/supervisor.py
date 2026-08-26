"""Dark-factory supervisor: plan -> dispatch -> gate -> review -> integrate."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.core.config import Config, ledger_path, load_config
from orchestrator.core.errors import DispatchBlocked
from orchestrator.core.ledger import Ledger, TaskStatus
from orchestrator.logs import get
from orchestrator.orchestration.service import (
    call_llm,
    cleanup_worktree,
    create_task,
    dispatch_task,
    generate_report,
    shutdown_runtime,
    task_status,
)
from orchestrator.runtime.worktrees import worktree_path

Io = Callable[[str], None]
GateRunner = Callable[[Path, Config], tuple[bool, str]]
Reviewer = Callable[[dict, str, bool], tuple[str, str]]

log = get("supervisor")

TERMINAL_OK = {"MERGED"}
TERMINAL_GIVE_UP = {"BLOCKED", "FAILED", "CANCELLED"}


class PlanningError(RuntimeError):
    pass


@dataclass
class PlannedTask:
    title: str
    objective: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    role: str | None = None
    model: str | None = None
    effort: str | None = None


PLAN_PROMPT = """You are the planning module of an autonomous repo orchestrator.

Goal from the operator:
<goal>
{goal}
</goal>

Decompose the goal into the smallest set of concrete tasks that achieves it.
Rules:
- Each task must be completable by one worker alone in an isolated worktree.
- Express dependencies by referencing earlier task titles in depends_on.
- Prefer 1-5 tasks; do not invent filler work.
- Available roles: orchestrator-worker (default), orchestrator-tester,
  orchestrator-reviewer. Set role only when it clearly fits.
- model is optional; set it only when a task clearly needs more or less
  capability than the default.
- effort is the reasoning effort for the worker (provider-specific variant,
  e.g. high, medium, low, minimal). Choose it per task: high for tricky
  architecture, debugging, or security work; medium for standard features;
  low/minimal for mechanical chores (renames, formatting, boilerplate).
  Omit when unsure.

Respond with ONLY a fenced JSON block, no other text:
```json
{{"tasks": [{{"title": "...", "objective": "...", "acceptance_criteria": ["..."],
"depends_on": [], "role": null, "model": null}}]}}
```"""


def extract_json_block(text: str) -> dict:
    fenced_start = text.find("```")
    if fenced_start != -1:
        body = text[fenced_start:].split("```")[1]
        body = body.split("\n", 1)[1] if body.lstrip().lower().startswith("json") else body
        return json.loads(body)
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return json.loads(text[start : i + 1])
    raise PlanningError("no JSON object found in planner output")


def parse_plan(text: str) -> list[PlannedTask]:
    data = extract_json_block(text)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanningError("planner returned no tasks")
    plans = []
    for rt in raw_tasks:
        title = str(rt.get("title") or "").strip()
        if not title:
            raise PlanningError("planner produced a task without a title")
        criteria = [str(c) for c in (rt.get("acceptance_criteria") or [])]
        deps = [str(d) for d in (rt.get("depends_on") or [])]
        plans.append(
            PlannedTask(
                title=title,
                objective=str(rt.get("objective") or ""),
                acceptance_criteria=criteria,
                depends_on=deps,
                role=rt.get("role") or None,
                model=rt.get("model") or None,
                effort=rt.get("effort") or None,
            )
        )
    return plans


def plan_tasks(root: Path, config: Config, goal: str) -> list[PlannedTask]:
    prompt = PLAN_PROMPT.format(goal=goal)
    try:
        plans = parse_plan(call_llm(root, prompt, model=config.planner_model))
        log.info("planner produced %d task(s)", len(plans))
        return plans
    except (PlanningError, json.JSONDecodeError, RuntimeError):
        feedback = (
            "Your previous response was not valid plan JSON matching the schema. "
            "Respond again with ONLY the fenced JSON block."
        )
        log.warning("planner response invalid; retrying once with corrective feedback")
        return parse_plan(call_llm(root, prompt + "\n\n" + feedback, model=config.planner_model))


def create_planned_tasks(root: Path, plans: list[PlannedTask]) -> list[str]:
    title_to_id: dict[str, str] = {}
    ids: list[str] = []
    for p in plans:
        missing = [d for d in p.depends_on if d not in title_to_id]
        if missing:
            raise PlanningError(f"task '{p.title}' depends on unknown titles: {missing}")
        data = create_task(
            root,
            title=p.title,
            objective=p.objective,
            acceptance_criteria=p.acceptance_criteria,
            dependencies=[title_to_id[d] for d in p.depends_on],
            role=p.role,
            model=p.model,
            effort=p.effort,
        )
        title_to_id[p.title] = data["id"]
        ids.append(data["id"])
    return ids


def default_gate(worktree: Path, config: Config) -> tuple[bool, str]:
    outputs: list[str] = []
    for command in config.gate_commands:
        proc = subprocess.run(
            command, shell=True, cwd=str(worktree), capture_output=True, text=True, check=False
        )
        tail = (proc.stdout + proc.stderr).strip()[-2000:]
        outputs.append(f"$ {command}\n{tail}")
        if proc.returncode != 0:
            return False, "\n\n".join(outputs)
    return True, "\n\n".join(outputs)


REVIEW_PROMPT = """You are reviewing worker output before integration.

Task: {title}
Objective: {objective}

Acceptance criteria:
{criteria}

Diff vs base branch:
<diff>
{diff}
</diff>

Verification gate output:
<gate ok={gate_ok}>
{gate_output}
</gate>

Decide whether this may be merged. Judge correctness and scope, not style.
Respond with a short explanation, then a FINAL LINE of strict JSON:
{{"verdict": "approve"}} or {{"verdict": "changes", "instructions": "<what to fix>"}}"""


def llm_review(task: dict, diff: str, gate_ok: bool, gate_output: str) -> tuple[str, str]:
    prompt = REVIEW_PROMPT.format(
        title=task["title"],
        objective=task["objective"] or "(none)",
        criteria="\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none)",
        diff=diff[-12000:],
        gate_ok=gate_ok,
        gate_output=gate_output[-4000:] or "(no gates configured)",
    )
    root = Path(task["_root"])
    config = load_config(root)
    text = call_llm(
        root,
        prompt,
        model=config.reviewer_model,
        agent="orchestrator-reviewer",
        effort=task.get("effort"),
    )
    line = next((ln for ln in reversed(text.strip().splitlines()) if ln.strip()), "")
    line = line.strip().strip("`")
    try:
        verdict = json.loads(line)
        v = str(verdict.get("verdict", "")).lower()
        if v in ("approve", "changes"):
            return v, str(verdict.get("instructions") or "")
    except json.JSONDecodeError:
        pass
    return ("approve" if gate_ok else "changes"), ""


def _worktree_diff(worktree: Path, base: str) -> str:
    proc = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _merge_branch(root: Path, branch: str, title: str) -> None:
    subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"merge: {title}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


def run_goal(
    root: Path,
    goal: str,
    *,
    dry_run: bool = False,
    max_loops: int = 40,
    max_corrections: int = 2,
    push: bool = False,
    poll_seconds: float = 10.0,
    planner: Callable[[Path, Config, str], list[PlannedTask]] = plan_tasks,
    gate_runner: GateRunner | None = None,
    reviewer: Reviewer | None = llm_review,
    io: Io = print,
) -> int:
    root = Path(root)
    config = load_config(root)
    gate = gate_runner or default_gate
    log.info("run_goal start: %r", goal[:80])

    try:
        io(f"planning: {goal[:100]}")
        plans = planner(root, config, goal)
        if dry_run:
            for i, p in enumerate(plans, 1):
                deps = f" <- {'/'.join(p.depends_on)}" if p.depends_on else ""
                io(f"  {i}. {p.title}{deps} [{p.role or 'worker'}]")
            io(f"dry-run: {len(plans)} tasks planned, nothing created")
            return 0

        ids = create_planned_tasks(root, plans)
        io(f"created: {', '.join(ids)}")

        corrections: dict[str, int] = {tid: 0 for tid in ids}
        retries: dict[str, int] = {tid: 0 for tid in ids}
        merged = 0

        for loop in range(1, max_loops + 1):
            progressed = False
            log.debug("loop %d/%d", loop, max_loops)

            for tid in ids:
                snap = task_status(root, tid)
                if snap["task"]["status"] != TaskStatus.PLANNED.value:
                    continue
                try:
                    record = dispatch_task(root, tid)
                    model_note = f", model={record['model']}" if record.get("model") else ""
                    io(f"[{loop}] dispatched {tid}{model_note} ({snap['task']['title']})")
                    progressed = True
                except DispatchBlocked:
                    pass

            for tid in ids:
                snap = task_status(root, tid, timeout=poll_seconds)
                t = snap["task"]
                status = t["status"]
                worker = snap.get("worker") or {}
                if worker.get("model_used") and worker.get("exit_code") is None:
                    log.info(
                        "%s running on %s (session %s)",
                        tid,
                        worker["model_used"],
                        worker.get("session_id"),
                    )

                if status == "REVIEWING":
                    progressed = True
                    worktree = worktree_path(root, config, t["branch"])
                    if not worktree.exists():
                        io(f"[{loop}] warning: {tid} REVIEWING but worktree missing")
                        continue
                    ok, gate_out = gate(worktree, config)
                    verdict, instructions = ("approve", "") if ok else ("changes", gate_out)
                    if reviewer is not None:
                        diff = _worktree_diff(worktree, config.primary_branch)
                        task_ctx = {**t, "_root": str(root)}
                        verdict, instructions = reviewer(task_ctx, diff, ok, gate_out)

                    if verdict == "approve":
                        _finalize_merge(root, config, tid, t, io)
                        merged += 1
                    elif corrections[tid] < max_corrections:
                        corrections[tid] += 1
                        dispatch_task(root, tid, instructions=instructions or gate_out[-1500:])
                        io(f"[{loop}] changes requested on {tid} (correction {corrections[tid]})")
                    else:
                        _give_up(root, tid, "correction budget exhausted", io)

                elif status in ("FAILED", "BLOCKED"):
                    progressed = True
                    note = t.get("last_result") or ""
                    if retries[tid] < 1:
                        retries[tid] += 1
                        dispatch_task(
                            root, tid, instructions=f"Previous attempt failed: {note[-800:]}"
                        )
                        io(f"[{loop}] retrying {tid} after failure ({retries[tid]}/1)")
                    else:
                        _give_up(root, tid, note, io)

            if all(_is_terminal(root, tid) for tid in ids):
                break
            if not progressed:
                time.sleep(poll_seconds)

        report = generate_report(root)
        io(report)
        if push and merged:
            subprocess.run(
                ["git", "push", "origin", config.primary_branch], cwd=str(root), check=False
            )
        total = len(ids)
        log.info("run_goal done: %d/%d merged", merged, total)
        return 0 if merged == total else 1
    finally:
        shutdown_runtime(root)


def _is_terminal(root: Path, tid: str) -> bool:
    return task_status(root, tid)["task"]["status"] in TERMINAL_OK | TERMINAL_GIVE_UP


def _give_up(root: Path, tid: str, note: str, io: Io) -> None:
    lg = Ledger.load(ledger_path(root))
    t = lg.get(tid)
    lg.update_status(tid, TaskStatus.BLOCKED)
    t.last_result = f"supervisor gave up: {note[:300]}"
    lg.save()
    cleanup_worktree(root, tid)
    io(f"gave up on {tid}: {note[:120]}")


def _finalize_merge(root: Path, config: Config, tid: str, t: dict, io: Io) -> None:
    try:
        _merge_branch(root, t["branch"], f"{tid} {t['title']}")
    except subprocess.CalledProcessError as exc:
        _give_up(root, tid, f"merge failed: {exc.stderr[-300:]}", io)
        return
    lg = Ledger.load(ledger_path(root))
    lg.update_status(tid, TaskStatus.MERGED)
    got = lg.get(tid)
    got.last_result = "merged by supervisor"
    lg.save()
    cleanup_worktree(root, tid)
    io(f"merged {tid} ({t['title']})")
