from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from orchestrator.core.config import load_config
from orchestrator.core.errors import DispatchBlocked, InvalidState
from orchestrator.core.ledger import Ledger, TaskStatus
from orchestrator.orchestration import service
from tests.conftest import HANDOFF_FAIL as _HANDOFF_FAIL
from tests.conftest import HANDOFF_OK as _HANDOFF_OK
from tests.conftest import configured as _configured
from tests.conftest import wait_until as _wait_until

HANDOFF_FAIL: str = _HANDOFF_FAIL
HANDOFF_OK: str = _HANDOFF_OK
configured: Any = _configured
wait_until: Any = _wait_until


def make_task(repo: Path, **kw: Any) -> dict[str, Any]:
    return service.create_task(repo, title=kw.pop("title", "Do Thing"), **kw)


class TestCreateTask:
    def test_assigns_branch(self, repo: Path) -> None:
        data = make_task(repo, title="Auth API")
        assert data["id"] == "TASK-001"
        assert data["branch"] == "agent/task-001-auth-api"

    def test_persisted(self, repo: Path) -> None:
        make_task(repo)
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        assert ledger.get("TASK-001").branch == "agent/task-001-do-thing"


class TestDispatchLifecycle:
    def test_success_flow(self, repo: Path) -> None:
        configured(repo)
        task = make_task(repo)

        result = service.dispatch_task(repo, task["id"])
        assert result["task"]["status"] == "DISPATCHED"
        assert (repo / ".orchestrator" / "worktrees" / task["branch"]).is_dir()

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "REVIEWING"
        assert final["worker"]["exit_code"] == 0
        assert final["task"]["handoff"]["SUMMARY"] == "did the thing"

    def test_failure_exit_code(self, repo: Path) -> None:
        configured(repo, RuntimeError("boom"))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "FAILED"
        assert "exited 1" in final["task"]["last_result"]

    def test_handoff_failed_status_wins_on_zero_exit(self, repo: Path) -> None:
        configured(repo, HANDOFF_FAIL)
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "FAILED"

    def test_zero_exit_without_handoff(self, repo: Path) -> None:
        configured(repo, "done")
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "REVIEWING"
        assert "no handoff block" in final["task"]["last_result"]

    def test_working_transition_observed(self, repo: Path) -> None:
        gate = threading.Event()

        def handler(prompt: str, cwd: Path) -> str:
            gate.wait(timeout=2)
            return HANDOFF_OK

        configured(repo, handler)
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        saw_working = wait_until(
            lambda: service.task_status(repo, task["id"])["task"]["status"] == "WORKING",
            timeout=2.0,
        )
        assert saw_working is True
        gate.set()
        final = service.task_status(repo, task["id"], timeout=15.0)
        assert final["task"]["status"] == "REVIEWING"


class TestDispatchGuards:
    def test_unmet_dependency_blocks(self, repo: Path) -> None:
        configured(repo)
        a = make_task(repo, title="Base")
        b = service.create_task(repo, title="Dependent", dependencies=[a["id"]])

        with pytest.raises(DispatchBlocked, match="TASK-001"):
            service.dispatch_task(repo, b["id"])
        # after dependency merges, dispatch proceeds
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(a["id"], TaskStatus.MERGED)
        ledger.save()
        result = service.dispatch_task(repo, b["id"])
        assert result["task"]["status"] == "DISPATCHED"
        service.task_status(repo, b["id"], timeout=1)

    def test_terminal_task_cannot_dispatch(self, repo: Path) -> None:
        configured(repo)
        task = make_task(repo)
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(task["id"], TaskStatus.MERGED)
        ledger.save()
        with pytest.raises(InvalidState):
            service.dispatch_task(repo, task["id"])

    def test_redispatch_after_failure_reuses_branch(self, repo: Path) -> None:
        configured(repo, RuntimeError("failed"))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        service.task_status(repo, task["id"], timeout=10.0)  # FAILED now

        configured(repo)  # fixed worker
        again = service.dispatch_task(repo, task["id"])
        assert again["task"]["branch"] == task["branch"]
        service.task_status(repo, task["id"], timeout=1)
        history = Path(again["log"]).read_text()
        assert "worker failed: RuntimeError: failed" in history
        assert "STATUS: DONE" in history

    def test_cancel_terminates_worker(self, repo: Path) -> None:
        gate = threading.Event()

        def handler(prompt: str, cwd: Path) -> str:
            gate.wait(timeout=2)
            return HANDOFF_OK

        configured(repo, handler)
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        cancelled = service.cancel_task(repo, task["id"])
        gate.set()
        assert cancelled["status"] == "CANCELLED"
        service.task_status(repo, task["id"], timeout=1)


class TestCleanup:
    def test_cleanup_worktree(self, repo: Path) -> None:
        configured(repo)
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        service.task_status(repo, task["id"], timeout=1)
        wt = repo / ".orchestrator" / "worktrees" / task["branch"]
        assert wt.is_dir()
        assert service.cleanup_worktree(repo, task["id"]) is True
        assert not wt.exists()

    def test_config_roundtrip_has_opencode_bin(self, repo: Path) -> None:
        configured(repo)
        assert load_config(repo).opencode_bin.endswith("opencode")
