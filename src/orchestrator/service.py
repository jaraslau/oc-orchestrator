"""Service layer: the operations exposed to the manager agent via MCP."""

from __future__ import annotations

from pathlib import Path

from orchestrator.config import ledger_path, load_config
from orchestrator.dispatcher import Dispatcher, DispatchRecord, parse_handoff, wait_for_completion
from orchestrator.errors import DispatchBlocked, InvalidState, TaskNotFound
from orchestrator.ledger import Ledger, Task, TaskStatus
from orchestrator.prompts import render_delegation
from orchestrator.worktrees import branch_name, ensure_worktree, remove_worktree

_TERMINAL_STATUSES = {TaskStatus.MERGED, TaskStatus.CANCELLED}

_dispatchers: dict[str, Dispatcher] = {}


def get_dispatcher(root: Path) -> Dispatcher:
    """One Dispatcher per repo per process, so Popen handles survive across calls."""
    key = str(Path(root).resolve())
    if key not in _dispatchers:
        _dispatchers[key] = Dispatcher(Path(key))
    return _dispatchers[key]


def create_task(
    root: Path,
    *,
    title: str,
    objective: str = "",
    acceptance_criteria: list[str] | None = None,
    dependencies: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict:
    config = load_config(root)
    ledger = Ledger.load(ledger_path(root))
    task = ledger.create_task(
        title,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        dependencies=dependencies,
        risks=risks,
    )
    task.branch = branch_name(config, task.id, title)
    ledger.save()
    return task.to_dict()


def list_tasks(root: Path, status: str | None = None) -> list[dict]:
    ledger = Ledger.load(ledger_path(root))
    return [t.to_dict() for t in ledger.filter(status=status)]


def get_task(root: Path, task_id: str) -> dict:
    return _load_ledger(root).get(task_id).to_dict()


def dispatch_task(root: Path, task_id: str, model: str | None = None) -> dict:
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

    worktree, branch = ensure_worktree(root, config, task.id, task.title, task.branch)
    task.branch = branch
    prompt = render_delegation(config, task)

    dispatcher = get_dispatcher(root)
    record = dispatcher.spawn(
        config=config,
        task_id=task.id,
        branch=branch,
        worktree=worktree,
        prompt=prompt,
        model=model or config.worker_model,
    )
    task.agent = config.worker_agent
    ledger.update_status(task.id, TaskStatus.DISPATCHED)
    ledger.save()
    return {
        "task": task.to_dict(),
        "worktree": str(worktree),
        "log": record.log_path,
        "pid": record.pid,
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
        }
    )
