import pytest

from orchestrator import service
from orchestrator.config import load_config
from orchestrator.errors import DispatchBlocked, InvalidState
from orchestrator.ledger import Ledger, TaskStatus
from tests.conftest import HANDOFF_FAIL, HANDOFF_OK, configured, wait_until


def make_task(repo, **kw):
    return service.create_task(repo, title=kw.pop("title", "Do Thing"), **kw)


class TestCreateTask:
    def test_assigns_branch(self, repo):
        data = make_task(repo, title="Auth API")
        assert data["id"] == "TASK-001"
        assert data["branch"] == "agent/task-001-auth-api"

    def test_persisted(self, repo):
        make_task(repo)
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        assert ledger.get("TASK-001").branch == "agent/task-001-do-thing"


class TestDispatchLifecycle:
    def test_success_flow(self, repo, fake_worker):
        worker = fake_worker(HANDOFF_OK)
        configured(repo, worker)
        task = make_task(repo)

        result = service.dispatch_task(repo, task["id"])
        assert result["task"]["status"] == "DISPATCHED"
        assert (repo / ".orchestrator" / "worktrees" / task["branch"]).is_dir()

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "REVIEWING"
        assert final["worker"]["exit_code"] == 0
        assert final["task"]["handoff"]["SUMMARY"] == "did the thing"

    def test_failure_exit_code(self, repo, fake_worker):
        worker = fake_worker('echo "boom" >&2\nexit 3')
        configured(repo, worker)
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "FAILED"
        assert "exited 3" in final["task"]["last_result"]

    def test_handoff_failed_status_wins_on_zero_exit(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_FAIL))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "FAILED"

    def test_zero_exit_without_handoff(self, repo, fake_worker):
        configured(repo, fake_worker('echo "done"'))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        final = service.task_status(repo, task["id"], timeout=10.0)
        assert final["task"]["status"] == "REVIEWING"
        assert "no handoff block" in final["task"]["last_result"]

    def test_working_transition_observed(self, repo, fake_worker):
        configured(repo, fake_worker("sleep 1.5\n" + HANDOFF_OK))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])

        saw_working = wait_until(
            lambda: service.task_status(repo, task["id"])["task"]["status"] == "WORKING",
            timeout=2.0,
        )
        assert saw_working is True
        final = service.task_status(repo, task["id"], timeout=15.0)
        assert final["task"]["status"] == "REVIEWING"


class TestDispatchGuards:
    def test_unmet_dependency_blocks(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
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

    def test_terminal_task_cannot_dispatch(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        task = make_task(repo)
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(task["id"], TaskStatus.MERGED)
        ledger.save()
        with pytest.raises(InvalidState):
            service.dispatch_task(repo, task["id"])

    def test_redispatch_after_failure_reuses_branch(self, repo, fake_worker):
        configured(repo, fake_worker("exit 1"))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        service.task_status(repo, task["id"], timeout=10.0)  # FAILED now

        configured(repo, fake_worker(HANDOFF_OK))  # fixed worker
        again = service.dispatch_task(repo, task["id"])
        assert again["task"]["branch"] == task["branch"]

    def test_cancel_terminates_worker(self, repo, fake_worker):
        configured(repo, fake_worker("sleep 30\n" + HANDOFF_OK))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        cancelled = service.cancel_task(repo, task["id"])
        assert cancelled["status"] == "CANCELLED"


class TestCleanup:
    def test_cleanup_worktree(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        task = make_task(repo)
        service.dispatch_task(repo, task["id"])
        wt = repo / ".orchestrator" / "worktrees" / task["branch"]
        assert wt.is_dir()
        assert service.cleanup_worktree(repo, task["id"]) is True
        assert not wt.exists()

    def test_config_roundtrip_has_opencode_bin(self, repo, fake_worker):
        configured(repo, fake_worker("exit 0"))
        assert load_config(repo).opencode_bin.endswith("opencode")
