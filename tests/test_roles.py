import pytest

from orchestrator import service
from orchestrator.config import Config
from orchestrator.dispatcher import Dispatcher
from orchestrator.errors import InvalidState
from orchestrator.ledger import Ledger
from tests.conftest import HANDOFF_OK, configured


def make_agent_def(repo, name="orchestrator-tester", content=None):
    d = repo / ".opencode" / "agent"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(content or f"---\ndescription: {name}\nmode: primary\n---\nrole body\n")
    return path


class TestRoleAssignment:
    def test_create_task_persists_role(self, repo):
        make_agent_def(repo)
        data = service.create_task(repo, title="Cover auth module", role="orchestrator-tester")
        assert data["role"] == "orchestrator-tester"
        stored = Ledger.load(repo / ".orchestrator" / "ledger.json").get(data["id"])
        assert stored.role == "orchestrator-tester"

    def test_create_task_rejects_unknown_role(self, repo):
        with pytest.raises(InvalidState, match="agent definition not found"):
            service.create_task(repo, title="x", role="orchestrator-ninja")

    def test_dispatch_resolves_task_role_over_default(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        make_agent_def(repo)  # orchestrator-tester
        task = service.create_task(repo, title="Test it", role="orchestrator-tester")
        result = service.dispatch_task(repo, task["id"])
        assert result["task"]["agent"] == "orchestrator-tester"

    def test_dispatch_explicit_role_beats_task_role(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        make_agent_def(repo, "orchestrator-tester")
        make_agent_def(repo, "orchestrator-reviewer")
        task = service.create_task(repo, title="Check", role="orchestrator-tester")
        result = service.dispatch_task(repo, task["id"], role="orchestrator-reviewer")
        assert result["task"]["agent"] == "orchestrator-reviewer"

    def test_dispatch_default_when_no_roles_assigned(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        # no custom agents installed: default worker must still validate via its file
        make_agent_def(repo, "orchestrator-worker", content="---\nmode: primary\n---\ndefault\n")
        task = service.create_task(repo, title="Plain")
        result = service.dispatch_task(repo, task["id"])
        assert result["task"]["agent"] == "orchestrator-worker"

    def test_dispatch_fails_fast_on_missing_role_file(self, repo, fake_worker):
        configured(repo, fake_worker(HANDOFF_OK))
        task = service.create_task(repo, title="x")
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        t = ledger.get(task["id"])
        t.role = "orchestrator-ghost"  # simulate stale/deleted definition
        ledger.save()
        with pytest.raises(InvalidState, match="orchestrator-ghost"):
            service.dispatch_task(repo, task["id"])

    def test_build_command_uses_agent_override(self, tmp_path):
        cmd = Dispatcher.build_command(
            Config(worker_agent="orchestrator-worker"),
            tmp_path,
            "p",
            agent_name="orchestrator-reviewer",
        )
        assert cmd[cmd.index("--agent") + 1] == "orchestrator-reviewer"

    def test_prompt_greets_resolved_role(self, repo):
        from orchestrator.prompts import render_delegation

        data = service.create_task(repo, title="T")
        task = Ledger.load(repo / ".orchestrator" / "ledger.json").get(data["id"])
        prompt = render_delegation(Config(), task, worker_agent="orchestrator-reviewer")
        assert prompt.startswith("You are Worker Agent orchestrator-reviewer.")
