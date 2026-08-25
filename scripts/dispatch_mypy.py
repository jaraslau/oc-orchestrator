#!/usr/bin/env python3
"""Dispatch mypy --strict fix tasks using the orchestrator service layer."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.core.config import ledger_path  # noqa: E402
from orchestrator.core.ledger import Ledger, TaskStatus  # noqa: E402
from orchestrator.orchestration import service  # noqa: E402
from orchestrator.orchestration.service import shutdown_runtime  # noqa: E402

# ── Task definitions ────────────────────────────────────────────────────────

MYPY_TASKS = [
    {
        "title": "Fix mypy --strict: orchestrator/server.py",
        "objective": (
            "Fix all mypy --strict errors in orchestrator/server.py. "
            "Run: poetry run mypy --strict orchestrator/server.py "
            "Add type annotations, return types, generic type args. "
            "Do NOT change runtime behavior."
        ),
        "acceptance_criteria": ["poetry run mypy --strict orchestrator/server.py reports 0 errors"],
    },
    {
        "title": "Fix mypy --strict: orchestrator/orchestration/",
        "objective": (
            "Fix all mypy --strict errors in orchestrator/orchestration/ "
            "(service.py and supervisor.py). "
            "Run: poetry run mypy --strict orchestrator/orchestration/ "
            "Add type annotations, return types, generic type args. "
            "Do NOT change runtime behavior."
        ),
        "acceptance_criteria": [
            "poetry run mypy --strict orchestrator/orchestration/ reports 0 errors"
        ],
    },
    {
        "title": "Fix mypy --strict: orchestrator/runtime/ + core/ + cli",
        "objective": (
            "Fix all mypy --strict errors across orchestrator/runtime/ "
            "(dispatcher, client, events, runner, resilience, "
            "opencode_server, github), "
            "orchestrator/core/ledger.py, and orchestrator/cli.py. "
            "Run: poetry run mypy --strict orchestrator/runtime/ "
            "orchestrator/core/ledger.py orchestrator/cli.py "
            "Add type annotations, return types. "
            "Do NOT change runtime behavior."
        ),
        "acceptance_criteria": [
            "poetry run mypy --strict orchestrator/runtime/ "
            "orchestrator/core/ledger.py orchestrator/cli.py "
            "reports 0 errors"
        ],
    },
    {
        "title": "Fix mypy --strict: tests test_runner + test_review",
        "objective": (
            "Fix all mypy --strict errors in tests/test_runner.py "
            "and tests/test_review.py. "
            "Mostly missing type annotations on test functions. "
            "Run: poetry run mypy --strict tests/test_runner.py "
            "tests/test_review.py "
            "Add return types, parameter types, generic type args. "
            "Do NOT change test logic."
        ),
        "acceptance_criteria": [
            "poetry run mypy --strict tests/test_runner.py tests/test_review.py reports 0 errors"
        ],
    },
    {
        "title": ("Fix mypy --strict: tests batch 2 (dispatcher, supervisor, cli, service)"),
        "objective": (
            "Fix all mypy --strict errors in "
            "tests/test_dispatcher_server.py, "
            "tests/test_supervisor.py, tests/test_cli.py, "
            "tests/test_service.py. "
            "Mostly missing type annotations. "
            "Run: poetry run mypy --strict "
            "tests/test_dispatcher_server.py "
            "tests/test_supervisor.py tests/test_cli.py "
            "tests/test_service.py "
            "Add return types, parameter types, "
            "generic type args. Do NOT change test logic."
        ),
        "acceptance_criteria": ["poetry run mypy --strict on those 4 files reports 0 errors"],
    },
    {
        "title": ("Fix mypy --strict: tests batch 3 (remaining test files)"),
        "objective": (
            "Fix all mypy --strict errors in "
            "tests/test_resilience.py, tests/test_server.py, "
            "tests/test_roles.py, tests/test_ledger.py, "
            "tests/test_dispatcher.py, "
            "tests/test_opencode_server.py, "
            "tests/test_worktrees.py, tests/test_config.py, "
            "tests/conftest.py. "
            "Run: poetry run mypy --strict on all 9 files. "
            "Add return types, parameter types, "
            "generic type args. Do NOT change test logic."
        ),
        "acceptance_criteria": ["poetry run mypy --strict on those 9 files reports 0 errors"],
    },
]

NON_TERMINAL = {
    TaskStatus.PLANNED,
    TaskStatus.DISPATCHED,
    TaskStatus.WORKING,
    TaskStatus.REVIEWING,
}


def cleanup_stale_tasks() -> int:
    """Cancel all non-terminal non-merged tasks."""
    ledger = Ledger.load(ledger_path(ROOT))
    stale = [t for t in ledger.filter() if t.status in NON_TERMINAL]
    count = 0
    for task in stale:
        try:
            service.cancel_task(ROOT, task.id)
            count += 1
            print(f"  cancelled {task.id} ({task.status.value})")
        except Exception as exc:
            print(f"  could not cancel {task.id}: {exc}")
    return count


def create_and_dispatch_tasks() -> list[str]:
    """Create 6 mypy tasks and dispatch them."""
    task_ids: list[str] = []
    for spec in MYPY_TASKS:
        result = service.create_task(
            ROOT,
            title=spec["title"],
            objective=spec["objective"],
            acceptance_criteria=spec["acceptance_criteria"],
        )
        tid = result["id"]
        task_ids.append(tid)
        print(f"  created {tid}: {spec['title']}")

    print()
    print("Dispatching...")
    for tid in task_ids:
        try:
            result = service.dispatch_task(ROOT, tid)
            engine = result.get("worker_engine", "?")
            print(f"  dispatched {tid} (engine={engine})")
        except Exception as exc:
            print(f"  FAILED to dispatch {tid}: {exc}")

    return task_ids


def poll_until_done(
    task_ids: list[str],
    timeout: float = 1800.0,
) -> dict[str, str]:
    """Poll until all tasks reach terminal state or timeout."""
    deadline = time.monotonic() + timeout
    statuses: dict[str, str] = {}
    terminal = {
        "MERGED",
        "CANCELLED",
        "FAILED",
        "REVIEWING",
        "READY_TO_MERGE",
    }

    while time.monotonic() < deadline:
        all_done = True
        for tid in task_ids:
            if tid in statuses:
                continue
            result = service.task_status(ROOT, tid, timeout=0.0)
            status = result["task"]["status"]
            if status in terminal:
                statuses[tid] = status
                print(f"  {tid}: {status}")
            else:
                all_done = False
        if all_done:
            break
        time.sleep(5)

    for tid in task_ids:
        if tid not in statuses:
            result = service.task_status(ROOT, tid, timeout=0.0)
            statuses[tid] = result["task"]["status"]

    return statuses


def print_results(statuses: dict[str, str]) -> None:
    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    for tid, status in sorted(statuses.items()):
        if status == "REVIEWING":
            marker = "+"
        elif status == "FAILED":
            marker = "x"
        else:
            marker = "."
        print(f"  {marker} {tid}: {status}")
    print()

    ledger = Ledger.load(ledger_path(ROOT))
    for tid in sorted(statuses):
        task = ledger.get(tid)
        if task.branch:
            log_path = ROOT / ".orchestrator" / "logs" / f"{tid.lower()}.log"
            if log_path.exists():
                text = log_path.read_text(errors="replace").strip()
                lines = text.splitlines()
                tail = "\n".join(lines[-20:])
                print(f"-- {tid} log tail --")
                print(tail)
                print()


def main() -> None:
    print("Cleaning up stale tasks...")
    cleanup_stale_tasks()

    print()
    print("Creating tasks...")
    task_ids = create_and_dispatch_tasks()

    print()
    n = len(task_ids)
    print(f"Polling {n} tasks (timeout 30 min)...")
    try:
        statuses = poll_until_done(task_ids)
    except KeyboardInterrupt:
        print("\nInterrupted -- shutting down...")
        shutdown_runtime(ROOT)
        sys.exit(1)

    print_results(statuses)
    shutdown_runtime(ROOT)


if __name__ == "__main__":
    main()
