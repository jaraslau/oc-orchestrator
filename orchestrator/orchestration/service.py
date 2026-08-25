"""Service layer: the operations exposed to the manager agent via MCP."""

from __future__ import annotations

from pathlib import Path

from orchestrator.core.config import Config, ledger_path, load_config
from orchestrator.core.errors import DispatchBlocked, InvalidState, TaskNotFound
from orchestrator.core.ledger import Ledger, Task, TaskStatus
from orchestrator.orchestration.prompts import render_delegation
from orchestrator.runtime.client import OpencodeClient
from orchestrator.runtime.dispatcher import (
    Dispatcher,
    DispatchRecord,
    parse_handoff,
    wait_for_completion,
)
from orchestrator.runtime.events import EventTap
from orchestrator.runtime.github import GhClient, pr_number_from_url
from orchestrator.runtime.llm import run_llm
from orchestrator.runtime.opencode_server import OpencodeServer
from orchestrator.runtime.runner import SessionRunner
from orchestrator.runtime.worktrees import branch_name, ensure_worktree, remove_worktree

_TERMINAL_STATUSES = {TaskStatus.MERGED, TaskStatus.CANCELLED}

_dispatchers: dict[str, Dispatcher] = {}


class ServerRuntime:
    def __init__(self, root: Path, config: Config) -> None:
        self.server = OpencodeServer(config.opencode_bin, config.server_port, Path(root))
        self.base_url = self.server.start()
        self.client = OpencodeClient(self.base_url)
        self.tap = EventTap(self.client)
        self.tap.start()
        self.runner = SessionRunner(self.client, self.tap, fallback_models=config.fallback_models)

    def close(self) -> None:
        self.tap.stop()
        self.server.stop()


_runtimes: dict[str, ServerRuntime] = {}


def get_runtime(root: Path) -> ServerRuntime | None:
    """Shared opencode-server runtime per repo; None when backend is 'cli'."""
    key = str(Path(root).resolve())
    if key in _runtimes:
        return _runtimes[key]
    config = load_config(Path(key))
    if config.execution_backend == "cli":
        return None
    runtime = ServerRuntime(Path(key), config)
    _runtimes[key] = runtime
    return runtime


def shutdown_runtime(root: Path) -> None:
    runtime = _runtimes.pop(str(Path(root).resolve()), None)
    if runtime is not None:
        runtime.close()


def get_dispatcher(root: Path) -> Dispatcher:
    """One Dispatcher per repo per process; server-backed when available."""
    key = str(Path(root).resolve())
    if key in _dispatchers:
        return _dispatchers[key]
    runtime = get_runtime(root)
    runner = runtime.runner if runtime is not None else None
    _dispatchers[key] = Dispatcher(Path(key), runner=runner)
    return _dispatchers[key]


def call_llm(
    root: Path,
    prompt: str,
    *,
    model: str | None = None,
    agent: str | None = None,
    effort: str | None = None,
    timeout: float = 900.0,
) -> str:
    """Planner/reviewer calls: server session with failover when possible."""
    runtime = get_runtime(root)
    if runtime is None:
        config = load_config(Path(root))
        return run_llm(
            Path(root), config, prompt, model=model, agent=agent, timeout=timeout, effort=effort
        )
    result = runtime.runner.run(
        prompt,
        Path(root),
        agent=agent,
        model=model,
        variant=effort,
        timeout=timeout,
    )
    return result.text


def create_task(
    root: Path,
    *,
    title: str,
    objective: str = "",
    acceptance_criteria: list[str] | None = None,
    dependencies: list[str] | None = None,
    risks: list[str] | None = None,
    role: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    config = load_config(root)
    ledger = Ledger.load(ledger_path(root))
    task = ledger.create_task(
        title,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        dependencies=dependencies,
        risks=risks,
        model=model,
        effort=effort,
    )
    if role:
        _validate_agent(root, role)
        task.role = role
    task.branch = branch_name(config, task.id, title)
    ledger.save()
    return task.to_dict()


def _validate_agent(root: Path, name: str) -> Path:
    path = Path(root) / ".opencode" / "agent" / f"{name}.md"
    if not path.exists():
        raise InvalidState(
            f"agent definition not found: {path}; "
            "use a built-in role (orchestrator-worker/tester/reviewer) or add a custom .md"
        )
    return path


def resolve_agent(root: Path, config: Config, explicit: str | None, task: Task) -> str:
    """Precedence: dispatch-time override > task's assigned role > default worker."""
    name = explicit or task.role or config.worker_agent
    _validate_agent(root, name)
    return name


def list_tasks(root: Path, status: str | None = None) -> list[dict]:
    ledger = Ledger.load(ledger_path(root))
    return [t.to_dict() for t in ledger.filter(status=status)]


def get_task(root: Path, task_id: str) -> dict:
    return _load_ledger(root).get(task_id).to_dict()


def dispatch_task(
    root: Path,
    task_id: str,
    model: str | None = None,
    instructions: str | None = None,
    role: str | None = None,
    effort: str | None = None,
) -> dict:
    root = Path(root)
    config = load_config(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)

    if task.status in _TERMINAL_STATUSES:
        raise InvalidState(f"{task_id} is {task.status.value} and cannot be dispatched")
    unmet = [
        dep
        for dep in task.dependencies
        if (dep_task := _find(ledger, dep)) is None or dep_task.status != TaskStatus.MERGED
    ]
    if unmet:
        raise DispatchBlocked(f"{task_id} blocked by unmet dependencies: {', '.join(unmet)}")

    agent_name = resolve_agent(root, config, role, task)
    worktree, branch = ensure_worktree(root, config, task.id, task.title, task.branch)
    task.branch = branch
    prompt = render_delegation(
        config, task, extra_instructions=instructions, worker_agent=agent_name
    )

    dispatcher = get_dispatcher(root)
    record = dispatcher.spawn(
        config=config,
        task_id=task.id,
        branch=branch,
        worktree=worktree,
        prompt=prompt,
        model=model or task.model or config.worker_model,
        agent_name=agent_name,
        variant=effort or task.effort,
    )
    task.agent = agent_name
    ledger.update_status(task.id, TaskStatus.DISPATCHED)
    ledger.save()
    return {
        "task": task.to_dict(),
        "worktree": str(worktree),
        "log": record.log_path,
        "pid": record.pid,
        "worker_engine": record.engine,
        "model": model or task.model or config.worker_model,
    }


def task_status(root: Path, task_id: str, timeout: float = 0.0) -> dict:
    """Return current task state, reconciling with the worker process first."""
    root = Path(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)

    dispatcher = get_dispatcher(root)
    record = (
        wait_for_completion(dispatcher, task_id, timeout)
        if timeout > 0
        else dispatcher.poll(task_id)
    )
    if record is not None:
        _reconcile(ledger, task, record)
    ledger.save()
    return {"task": task.to_dict(), "worker": _record_dict(record)}


def cancel_task(root: Path, task_id: str) -> dict:
    root = Path(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)
    if task.status in _TERMINAL_STATUSES:
        raise InvalidState(f"{task_id} is already {task.status.value}")
    get_dispatcher(root).terminate(task_id)
    ledger.update_status(task_id, TaskStatus.CANCELLED)
    ledger.save()
    return task.to_dict()


def cleanup_worktree(root: Path, task_id: str) -> bool:
    config = load_config(Path(root))
    task = _load_ledger(Path(root)).get(task_id)
    if task.branch is None:
        return False
    return remove_worktree(Path(root), config, task.branch)


def _load_ledger(root: Path) -> Ledger:
    path = ledger_path(Path(root))
    if not path.exists():
        raise TaskNotFound(f"no ledger at {path}; run 'oc-orchestrator init' first")
    return Ledger.load(path)


def _find(ledger: Ledger, task_id: str) -> Task | None:
    try:
        return ledger.get(task_id)
    except KeyError:
        return None


def _reconcile(ledger: Ledger, task: Task, record: DispatchRecord) -> None:
    """Fold worker process state into the ledger."""
    if task.status not in {TaskStatus.PLANNED, TaskStatus.DISPATCHED, TaskStatus.WORKING}:
        return
    running = record.exit_code is None and record.ended_at is None
    if running:
        if task.status == TaskStatus.DISPATCHED:
            ledger.update_status(task.id, TaskStatus.WORKING)
        return

    log_text = Dispatcher.read_log(record)
    handoff = parse_handoff(log_text)
    task.handoff = handoff

    if record.exit_code == 0:
        status = TaskStatus.REVIEWING
        note = "worker exited 0"
        if handoff and handoff.get("STATUS") == "FAILED":
            status = TaskStatus.FAILED
            note = "handoff reports FAILED"
        elif handoff and handoff.get("STATUS") == "BLOCKED":
            status = TaskStatus.BLOCKED
            note = "handoff reports BLOCKED"
        elif handoff is None:
            note = "worker exited 0 but produced no handoff block"
    elif record.exit_code is None:
        # ended while orchestrator was down; infer from handoff
        if handoff and handoff.get("STATUS") == "DONE":
            status, note = TaskStatus.REVIEWING, "process ended (restart); handoff DONE"
        else:
            status, note = TaskStatus.FAILED, "process ended without success handoff"
    else:
        status = TaskStatus.FAILED
        note = f"worker exited {record.exit_code}"

    task.last_result = note
    ledger.update_status(task.id, status)


def _record_dict(record: DispatchRecord | None) -> dict | None:
    return (
        None
        if record is None
        else {
            "pid": record.pid,
            "exit_code": record.exit_code,
            "log": record.log_path,
            "worktree": record.worktree,
            "engine": record.engine,
            "session_id": record.session_id,
            "model_used": record.model_used or None,
        }
    )


# ---------------------------------------------------------------------------
# Review & integration (GitHub via gh)
# ---------------------------------------------------------------------------

_PR_ELIGIBLE = {TaskStatus.REVIEWING, TaskStatus.READY_TO_MERGE, TaskStatus.CHANGES_REQUESTED}


def _client(root: Path, config: Config, gh_runner=None) -> GhClient:
    return GhClient(root, gh_bin=config.gh_bin, runner=gh_runner)


def _default_pr_body(task: Task) -> str:
    lines = [f"Closes out {task.id} - {task.title}."]
    if task.objective:
        lines += ["", "## Objective", task.objective]
    if task.acceptance_criteria:
        lines += ["", "## Acceptance criteria"]
        lines += [f"- {c}" for c in task.acceptance_criteria]
    handoff = task.handoff or {}
    if handoff.get("SUMMARY"):
        lines += ["", "## Worker summary", handoff["SUMMARY"]]
    if handoff.get("KNOWN ISSUES") and handoff["KNOWN ISSUES"].lower() != "none":
        lines += ["", "## Known issues", handoff["KNOWN ISSUES"]]
    return "\n".join(lines) + "\n"


def open_pr(
    root: Path,
    task_id: str,
    title: str | None = None,
    body: str | None = None,
    gh_runner=None,
) -> dict:
    root = Path(root)
    config = load_config(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)
    if task.status not in _PR_ELIGIBLE:
        raise InvalidState(f"{task_id} is {task.status.value}; open_pr requires REVIEWING first")
    if task.branch is None:
        raise InvalidState(f"{task_id} has no branch assigned")

    client = _client(root, config, gh_runner)
    client.check()
    pr = client.find_by_head(task.branch)
    if pr is None:
        pr = client.create_pr(
            base=config.primary_branch,
            head=task.branch,
            title=title or f"{task.id}: {task.title}",
            body=body or _default_pr_body(task),
        )
    task.pr = pr.url
    if task.status == TaskStatus.REVIEWING:
        ledger.update_status(task_id, TaskStatus.PR_OPEN)
    ledger.save()
    return {"task": task.to_dict(), "pr": {"number": pr.number, "url": pr.url}}


def request_changes(root: Path, task_id: str, comment: str, gh_runner=None) -> dict:
    root = Path(root)
    config = load_config(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)
    if task.status in _TERMINAL_STATUSES:
        raise InvalidState(f"{task_id} is already {task.status.value}")

    posted = False
    if task.pr:
        number = pr_number_from_url(task.pr)
        if number is not None:
            client = _client(root, config, gh_runner)
            client.comment(number, f"[oc-orchestrator] Changes requested:\n\n{comment}")
            posted = True

    task.last_result = f"changes requested: {comment}"
    ledger.update_status(task_id, TaskStatus.CHANGES_REQUESTED)
    ledger.save()
    return {"task": task.to_dict(), "posted_to_pr": posted}


def merge_task(root: Path, task_id: str, gh_runner=None) -> dict:
    root = Path(root)
    config = load_config(root)
    ledger = _load_ledger(root)
    task = ledger.get(task_id)

    if task.status in {TaskStatus.MERGED, TaskStatus.CANCELLED}:
        raise InvalidState(f"{task_id} is already {task.status.value}")
    unmet = [
        dep
        for dep in task.dependencies
        if (dep_task := _find(ledger, dep)) is None or dep_task.status != TaskStatus.MERGED
    ]
    if unmet:
        raise DispatchBlocked(
            f"cannot merge {task_id}; dependencies not merged: {', '.join(unmet)}"
        )
    if not task.pr:
        raise InvalidState(f"{task_id} has no pull request; call open_pr first")

    number = pr_number_from_url(task.pr)
    if number is None:
        raise InvalidState(f"cannot parse PR number from {task.pr}")
    client = _client(root, config, gh_runner)
    client.merge(number, method=config.merge_method)
    ledger.update_status(task_id, TaskStatus.MERGED)
    task.last_result = f"merged PR #{number}"
    remove_worktree(root, config, task.branch or "")
    ledger.save()
    return task.to_dict()


def pr_diff(root: Path, task_id: str, gh_runner=None) -> str:
    root = Path(root)
    config = load_config(root)
    task = _load_ledger(root).get(task_id)
    if not task.pr:
        raise InvalidState(f"{task_id} has no pull request; call open_pr first")
    number = pr_number_from_url(task.pr)
    if number is None:
        raise InvalidState(f"cannot parse PR number from {task.pr}")
    return _client(root, config, gh_runner).pr_diff(number)


def list_open_prs(root: Path, gh_runner=None) -> list[dict]:
    root = Path(root)
    config = load_config(root)
    client = _client(root, config, gh_runner)
    return [
        {"number": p.number, "url": p.url, "head": p.head, "title": p.title, "state": p.state}
        for p in client.list_prs()
    ]


def generate_report(root: Path) -> str:
    """Human-facing project report in the playbook's completion format."""
    ledger = Ledger.load(ledger_path(Path(root)))
    tasks = ledger.filter()
    merged = [t for t in tasks if t.status == TaskStatus.MERGED]
    active = [t for t in tasks if t.status not in {TaskStatus.MERGED, TaskStatus.CANCELLED}]
    attention = [t for t in active if t.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}]
    lines = ["PROJECT REPORT", ""]
    lines.append(f"Merged ({len(merged)}):")
    lines += [f"- {t.id} {t.title} [{t.last_result or ''}]" for t in merged] or ["- (none)"]
    lines.append("")
    lines.append(f"Open/active ({len(active)}):")
    lines += [f"- {t.id} {t.status.value:<17} {t.branch or '-'}" for t in active] or ["- (none)"]
    if attention:
        lines += ["", "Needs attention:"]
        lines += [f"- {t.id}: {t.last_result or 'no result recorded'}" for t in attention]
    return "\n".join(lines)
