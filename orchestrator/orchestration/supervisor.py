"""Dark-factory supervisor: plan -> dispatch -> gate -> review -> integrate."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from orchestrator.core.config import Config, ledger_path, load_config
from orchestrator.core.errors import DispatchBlocked
from orchestrator.core.ledger import Ledger, TaskStatus
from orchestrator.logs import TRACE, get
from orchestrator.orchestration.service import (
    call_llm,
    cancel_task,
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
Reviewer = Callable[[dict[str, Any], str, bool, str], tuple[str, str]]

log = get("supervisor")

TERMINAL_OK = {"MERGED"}
TERMINAL_GIVE_UP = {"BLOCKED", "FAILED", "CANCELLED"}
ACTIVE = {"DISPATCHED", "WORKING"}
MAX_PLANNED_TASKS = 8


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
- Choose the number of workers from the work itself. Prefer 1-5 tasks and never
  exceed {max_tasks}; do not invent filler work.
- Available roles: {roles}. Set role only when it clearly fits.
- Available configured models: {models}. Use null for the server default and
  never invent a model name.
- effort is the reasoning effort for the worker (provider-specific variant,
  e.g. high, medium, low, minimal). Choose it per task: high for tricky
  architecture, debugging, or security work; medium for standard features;
  low/minimal for mechanical chores (renames, formatting, boilerplate).
  Omit when unsure.

Respond with ONLY a fenced JSON block, no other text:
```json
{{"tasks": [{{"title": "...", "objective": "...", "acceptance_criteria": ["..."],
"depends_on": [], "role": null, "model": null, "effort": "medium"}}]}}
```"""


def extract_json_block(text: str) -> dict[str, Any]:
    fenced_start = text.find("```")
    if fenced_start != -1:
        body = text[fenced_start:].split("```")[1]
        body = body.split("\n", 1)[1] if body.lstrip().lower().startswith("json") else body
        return cast(dict[str, Any], json.loads(body))
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
                return cast(dict[str, Any], json.loads(text[start : i + 1]))
    raise PlanningError("no JSON object found in planner output")


def parse_plan(text: str) -> list[PlannedTask]:
    data = extract_json_block(text)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanningError("planner returned no tasks")
    plans = []
    for rt in raw_tasks:
        if not isinstance(rt, dict):
            raise PlanningError("planner produced a task that is not a JSON object")
        title = str(rt.get("title") or "").strip()
        if not title:
            raise PlanningError("planner produced a task without a title")
        raw_criteria = rt.get("acceptance_criteria") or []
        raw_dependencies = rt.get("depends_on") or []
        if not isinstance(raw_criteria, list) or not isinstance(raw_dependencies, list):
            raise PlanningError("planner task criteria/dependencies must be arrays")
        criteria = [str(c) for c in raw_criteria]
        deps = [str(d) for d in raw_dependencies]
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


def _available_roles(root: Path) -> list[str]:
    roles = sorted(p.stem for p in (Path(root) / ".opencode" / "agent").glob("*.md"))
    return [role for role in roles if role != "orchestrator-manager"]


def _role_catalog(root: Path) -> str:
    entries = []
    for role in _available_roles(root):
        path = Path(root) / ".opencode" / "agent" / f"{role}.md"
        description = next(
            (
                line.partition(":")[2].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("description:")
            ),
            "custom worker",
        )
        entries.append(f"{role} ({description})")
    return "; ".join(entries)


def _validate_plans(
    root: Path, plans: list[PlannedTask], allowed_models: set[str] | None = None
) -> None:
    if len(plans) > MAX_PLANNED_TASKS:
        raise PlanningError(f"planner produced {len(plans)} tasks; maximum is {MAX_PLANNED_TASKS}")
    roles = set(_available_roles(root))
    seen: set[str] = set()
    for plan in plans:
        if plan.title in seen:
            raise PlanningError(f"planner produced duplicate task title: {plan.title!r}")
        unknown = [dependency for dependency in plan.depends_on if dependency not in seen]
        if unknown:
            raise PlanningError(
                f"task {plan.title!r} depends on missing or later task(s): {', '.join(unknown)}"
            )
        if plan.role and plan.role not in roles:
            raise PlanningError(f"task {plan.title!r} uses unavailable role: {plan.role}")
        if allowed_models is not None and plan.model and plan.model not in allowed_models:
            raise PlanningError(f"task {plan.title!r} uses unconfigured model: {plan.model}")
        seen.add(plan.title)


def plan_tasks(root: Path, config: Config, goal: str) -> list[PlannedTask]:
    configured_models = {m for m in [config.worker_model, *config.fallback_models] if m}
    prompt = PLAN_PROMPT.format(
        goal=goal,
        max_tasks=MAX_PLANNED_TASKS,
        roles=_role_catalog(root) or config.worker_agent,
        models=", ".join(sorted(configured_models)) or "server default only",
    )
    feedback = (
        "Your previous response was not valid plan JSON or used unavailable roles/models. "
        "Respond again with ONLY JSON matching the schema and listed choices."
    )
    for attempt in range(2):
        try:
            plans = parse_plan(
                call_llm(
                    root,
                    prompt if attempt == 0 else f"{prompt}\n\n{feedback}",
                    model=config.planner_model,
                )
            )
            _validate_plans(root, plans, configured_models)
            log.info(
                "planner produced %d task(s): %s",
                len(plans),
                [
                    {"title": p.title, "role": p.role, "model": p.model, "effort": p.effort}
                    for p in plans
                ],
            )
            return plans
        except Exception:
            log.warning("planner attempt %d/2 failed", attempt + 1, exc_info=True)
    log.error("planner unavailable; falling back to one general worker")
    return [
        PlannedTask(
            title=goal.strip()[:80] or "Complete requested goal",
            objective=goal,
            acceptance_criteria=["The requested goal is complete and relevant checks pass"],
            model=config.worker_model,
            effort="high",
        )
    ]


def create_planned_tasks(root: Path, plans: list[PlannedTask]) -> list[str]:
    _validate_plans(root, plans)
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
        started = time.monotonic()
        log.info("gate start: cwd=%s command=%s", worktree, command)
        proc = subprocess.run(
            command, shell=True, cwd=str(worktree), capture_output=True, text=True, check=False
        )
        output = (proc.stdout + proc.stderr).strip()
        tail = output[-2000:]
        outputs.append(f"$ {command}\n{tail}")
        log.debug("gate output (exit=%d):\n%s", proc.returncode, output or "(empty)")
        log.info("gate done: exit=%d elapsed=%.3fs", proc.returncode, time.monotonic() - started)
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


def llm_review(task: dict[str, Any], diff: str, gate_ok: bool, gate_output: str) -> tuple[str, str]:
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
    for attempt in range(2):
        text = call_llm(
            root,
            prompt
            if attempt == 0
            else f"{prompt}\n\nYour previous verdict was invalid. End with the required JSON line.",
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
        except (AttributeError, json.JSONDecodeError):
            pass
        log.warning("reviewer attempt %d/2 returned invalid verdict: %r", attempt + 1, line)
    raise RuntimeError("reviewer returned no valid approve/changes verdict")


def _worktree_diff(worktree: Path, base: str) -> str:
    proc = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed in {worktree}: {proc.stderr.strip()}")
    log.debug("diff collected: worktree=%s chars=%d", worktree, len(proc.stdout))
    return proc.stdout


def _merge_branch(root: Path, branch: str, title: str) -> None:
    log.info("merge start: branch=%s", branch)
    subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"merge: {title}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    log.info("merge done: branch=%s", branch)


def run_goal(
    root: Path,
    goal: str,
    *,
    dry_run: bool = False,
    max_loops: int = 40,
    max_corrections: int = 2,
    max_retries: int = 1,
    max_workers: int | None = None,
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
    worker_limit = config.max_parallel_tasks if max_workers is None else max_workers
    if not goal.strip():
        raise ValueError("goal must not be empty")
    if max_loops < 1 or worker_limit < 1 or max_corrections < 0 or max_retries < 0:
        raise ValueError("loop/worker limits must be positive and retry budgets non-negative")
    log.info(
        "run_goal start: goal=%r workers=%d loops=%d corrections=%d retries=%d",
        goal[:80],
        worker_limit,
        max_loops,
        max_corrections,
        max_retries,
    )

    def emit(message: str) -> None:
        log.log(TRACE, "operator: %s", message)
        io(message)

    try:
        emit(f"planning: {goal[:100]}")
        plans = planner(root, config, goal)
        log.debug(
            "plan: %s",
            [
                {
                    "title": p.title,
                    "role": p.role or config.worker_agent,
                    "model": p.model or config.worker_model or "server-default",
                    "effort": p.effort or "default",
                    "dependencies": p.depends_on,
                }
                for p in plans
            ],
        )
        if dry_run:
            for i, p in enumerate(plans, 1):
                deps = f" <- {'/'.join(p.depends_on)}" if p.depends_on else ""
                assignment = ", ".join(
                    [
                        p.role or config.worker_agent,
                        p.model or config.worker_model or "server-default",
                        f"effort={p.effort or 'default'}",
                    ]
                )
                emit(f"  {i}. {p.title}{deps} [{assignment}]")
            emit(f"dry-run: {len(plans)} tasks planned, nothing created")
            return 0

        ids = create_planned_tasks(root, plans)
        emit(f"created: {', '.join(ids)}")

        corrections: dict[str, int] = {tid: 0 for tid in ids}
        retries: dict[str, int] = {tid: 0 for tid in ids}
        review_failures: dict[str, int] = {tid: 0 for tid in ids}
        pending_corrections: dict[str, str] = {}

        def send_correction(tid: str, instructions: str, loop: int) -> None:
            pending_corrections[tid] = instructions
            if corrections[tid] >= max_corrections:
                pending_corrections.pop(tid, None)
                _give_up(root, tid, "correction budget exhausted", emit)
                return
            if not _worker_slot_available(root, ids, worker_limit):
                log.debug("correction queued for %s: worker limit reached", tid)
                return
            try:
                dispatch_task(root, tid, instructions=instructions)
            except Exception as exc:
                retries[tid] += 1
                log.exception("correction dispatch failed: task=%s", tid)
                if retries[tid] > max_retries:
                    pending_corrections.pop(tid, None)
                    _give_up(root, tid, f"correction dispatch failed: {exc}", emit)
                return
            corrections[tid] += 1
            pending_corrections.pop(tid, None)
            emit(f"[{loop}] changes requested on {tid} (correction {corrections[tid]})")

        for loop in range(1, max_loops + 1):
            progressed = False
            snapshots = {tid: task_status(root, tid) for tid in ids}
            active = sum(snap["task"]["status"] in ACTIVE for snap in snapshots.values())
            slots = max(0, worker_limit - active)
            log.debug("loop %d/%d: active=%d slots=%d", loop, max_loops, active, slots)

            for tid in ids:
                if slots == 0:
                    break
                snap = snapshots[tid]
                if snap["task"]["status"] != TaskStatus.PLANNED.value:
                    continue
                if any(
                    dep in snapshots and snapshots[dep]["task"]["status"] != TaskStatus.MERGED.value
                    for dep in snap["task"]["dependencies"]
                ):
                    continue
                try:
                    record = dispatch_task(root, tid)
                    model_note = f", model={record['model']}" if record.get("model") else ""
                    emit(f"[{loop}] dispatched {tid}{model_note} ({snap['task']['title']})")
                    progressed = True
                    slots -= 1
                except DispatchBlocked as exc:
                    log.debug("dispatch deferred: %s", exc)
                except Exception as exc:
                    retries[tid] += 1
                    log.exception("dispatch failed: task=%s attempt=%d", tid, retries[tid])
                    if retries[tid] > max_retries:
                        _give_up(root, tid, f"dispatch failed: {exc}", emit)
                    else:
                        emit(f"[{loop}] dispatch failed for {tid}; will retry")

            for tid in ids:
                snap = task_status(root, tid)
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
                        log.error("task %s is REVIEWING but worktree is missing: %s", tid, worktree)
                        _give_up(root, tid, f"review worktree missing: {worktree}", emit)
                        continue
                    if tid in pending_corrections:
                        send_correction(tid, pending_corrections[tid], loop)
                        continue
                    try:
                        ok, gate_out = gate(worktree, config)
                    except Exception as exc:
                        log.exception("gate crashed: task=%s", tid)
                        ok = False
                        gate_out = f"verification gate crashed: {type(exc).__name__}: {exc}"
                    log.info("gate result: task=%s ok=%s", tid, ok)
                    verdict, instructions = ("approve", "") if ok else ("changes", gate_out)
                    if reviewer is not None:
                        try:
                            diff = _worktree_diff(worktree, config.primary_branch)
                            task_ctx = {**t, "_root": str(root)}
                            verdict, instructions = reviewer(task_ctx, diff, ok, gate_out)
                            if verdict not in {"approve", "changes"}:
                                raise RuntimeError(f"unknown review verdict: {verdict!r}")
                            review_failures[tid] = 0
                        except Exception as exc:
                            log.exception("review failed: task=%s", tid)
                            if not ok:
                                verdict, instructions = "changes", gate_out
                            else:
                                review_failures[tid] += 1
                                if review_failures[tid] <= max_retries:
                                    emit(f"[{loop}] review failed for {tid}; will retry")
                                    continue
                                _give_up(root, tid, f"review failed: {exc}", emit)
                                continue
                    if not ok and verdict == "approve":
                        log.warning("review approval overridden by failed gate: task=%s", tid)
                        verdict, instructions = "changes", gate_out
                    log.info("review result: task=%s verdict=%s", tid, verdict)

                    if verdict == "approve":
                        _finalize_merge(root, config, tid, t, emit)
                    else:
                        send_correction(tid, instructions or gate_out[-1500:], loop)

                elif status in ("FAILED", "BLOCKED"):
                    progressed = True
                    note = t.get("last_result") or ""
                    if retries[tid] < max_retries:
                        if not _worker_slot_available(root, ids, worker_limit):
                            log.debug("retry queued for %s: worker limit reached", tid)
                            continue
                        retries[tid] += 1
                        try:
                            dispatch_task(
                                root, tid, instructions=f"Previous attempt failed: {note[-800:]}"
                            )
                            emit(
                                f"[{loop}] retrying {tid} after failure "
                                f"({retries[tid]}/{max_retries})"
                            )
                        except Exception as exc:
                            log.exception("worker retry dispatch failed: task=%s", tid)
                            _give_up(root, tid, f"retry dispatch failed: {exc}", emit)
                    else:
                        _give_up(root, tid, note, emit)

            if all(_is_terminal(root, tid) for tid in ids):
                break
            if not progressed:
                time.sleep(poll_seconds)
        else:
            for tid in ids:
                if not _is_terminal(root, tid):
                    _give_up(root, tid, f"supervisor loop budget exhausted ({max_loops})", emit)

        report = generate_report(root)
        emit(report)
        merged = sum(
            task.status == TaskStatus.MERGED
            for task in Ledger.load(ledger_path(root)).tasks.values()
            if task.id in ids
        )
        push_failed = False
        if push and merged:
            proc = subprocess.run(
                ["git", "push", "origin", config.primary_branch],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
            push_failed = proc.returncode != 0
            if push_failed:
                log.error("push failed (exit=%d): %s", proc.returncode, proc.stderr.strip())
                emit(f"push failed: {proc.stderr.strip()[-300:]}")
            else:
                log.info("push succeeded: origin/%s", config.primary_branch)
        total = len(ids)
        log.info("run_goal done: %d/%d merged", merged, total)
        return 0 if merged == total and not push_failed else 1
    finally:
        try:
            shutdown_runtime(root)
        except Exception:
            log.exception("runtime shutdown failed")


def _is_terminal(root: Path, tid: str) -> bool:
    return task_status(root, tid)["task"]["status"] in TERMINAL_OK | TERMINAL_GIVE_UP


def _worker_slot_available(root: Path, ids: list[str], limit: int) -> bool:
    active = sum(task_status(root, tid)["task"]["status"] in ACTIVE for tid in ids)
    return active < limit


def _give_up(root: Path, tid: str, note: str, io: Io) -> None:
    lg = Ledger.load(ledger_path(root))
    t = lg.get(tid)
    if t.status.value in ACTIVE:
        try:
            cancel_task(root, tid)
        except Exception:
            log.exception("worker cancellation failed while giving up on %s", tid)
        lg = Ledger.load(ledger_path(root))
        t = lg.get(tid)
    lg.update_status(tid, TaskStatus.BLOCKED)
    t.last_result = f"supervisor gave up: {note[:300]}"
    lg.save()
    try:
        cleanup_worktree(root, tid)
    except Exception:
        log.exception("worktree cleanup failed after giving up on %s", tid)
    io(f"gave up on {tid}: {note[:120]}")


def _finalize_merge(root: Path, config: Config, tid: str, t: dict[str, Any], io: Io) -> bool:
    try:
        _merge_branch(root, t["branch"], f"{tid} {t['title']}")
    except Exception as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        _give_up(root, tid, f"merge failed: {detail[-300:]}", io)
        return False
    lg = Ledger.load(ledger_path(root))
    lg.update_status(tid, TaskStatus.MERGED)
    got = lg.get(tid)
    got.last_result = "merged by supervisor"
    lg.save()
    try:
        cleanup_worktree(root, tid)
    except Exception:
        log.exception("worktree cleanup failed after merging %s", tid)
    io(f"merged {tid} ({t['title']})")
    return True
