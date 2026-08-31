import json

import pytest

from orchestrator.cli import MANAGER_AGENT_FILENAME, WORKER_AGENT_FILENAME, main
from orchestrator.core.ledger import TaskStatus


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def run(argv):
    return main(argv)


class TestInit:
    def test_creates_expected_files(self, repo, capsys):
        rc = run(["init", str(repo)])
        out = capsys.readouterr().out
        assert rc == 0
        assert (repo / ".orchestrator" / "config.json").exists()
        assert (repo / ".orchestrator" / "ledger.json").exists()
        assert (repo / ".opencode" / "agent" / MANAGER_AGENT_FILENAME).exists()
        assert (repo / ".opencode" / "agent" / WORKER_AGENT_FILENAME).exists()
        opencode_cfg = json.loads((repo / ".opencode" / "opencode.json").read_text())
        assert opencode_cfg["$schema"].startswith("https://")
        entry = opencode_cfg["mcp"]["oc-orchestrator"]
        # regression: generated config must be machine-independent (no absolute paths)
        assert entry["command"] == ["oc-orchestrator", "serve"]
        assert str(repo) not in json.dumps(opencode_cfg)
        assert entry["enabled"] is True
        manager_md = (repo / ".opencode" / "agent" / MANAGER_AGENT_FILENAME).read_text()
        worker_md = (repo / ".opencode" / "agent" / WORKER_AGENT_FILENAME).read_text()
        assert "Repository Manager Agent" in manager_md
        assert "dispatch_task" in manager_md
        assert "```handoff" in worker_md
        roles_dir = repo / ".opencode" / "agent"
        assert (roles_dir / "orchestrator-tester.md").exists()
        assert (roles_dir / "orchestrator-reviewer.md").exists()
        assert "initialized orchestration state" in out

    def test_idempotent_rerun(self, repo):
        run(["init", str(repo)])
        rc = run(["init", str(repo)])
        assert rc == 0
        gitignore = (repo / ".gitignore").read_text()
        assert gitignore.count(".orchestrator/") == 1
        json.loads((repo / ".opencode" / "opencode.json").read_text())

    def test_init_never_clobbers_existing_ledger(self, repo):
        from orchestrator.core.config import ledger_path
        from orchestrator.core.ledger import Ledger

        run(["init", str(repo)])
        lg = Ledger(ledger_path(repo))
        t = lg.create_task("precious history")
        lg.update_status(t.id, TaskStatus.REVIEWING)
        lg.save()

        rc = run(["init", str(repo)])
        assert rc == 0
        restored = Ledger.load(ledger_path(repo)).get(t.id)
        assert restored.status == TaskStatus.REVIEWING

    def test_preserves_existing_opencode_config(self, repo):
        cfg_dir = repo / ".opencode"
        cfg_dir.mkdir()
        existing = {"theme": "monokai", "mcp": {"other": {"type": "local"}}}
        (cfg_dir / "opencode.json").write_text(json.dumps(existing))

        rc = run(["init", str(repo)])
        assert rc == 0
        merged = json.loads((cfg_dir / "opencode.json").read_text())
        assert merged["theme"] == "monokai"
        assert set(merged["mcp"]) == {"other", "oc-orchestrator"}

    def test_invalid_opencode_json_aborts_without_touching_file(self, repo):
        cfg_dir = repo / ".opencode"
        cfg_dir.mkdir()
        broken = '{"mcp": oops'
        (cfg_dir / "opencode.json").write_text(broken)

        rc = run(["init", str(repo)])
        assert rc == 1
        assert (cfg_dir / "opencode.json").read_text() == broken

    def test_warns_outside_git_repo(self, tmp_path, capsys):
        rc = run(["init", str(tmp_path)])
        err = capsys.readouterr().err
        assert rc == 0
        assert "does not look like a git repository" in err


class TestStatus:
    def test_fails_before_init(self, tmp_path, capsys):
        rc = run(["status", str(tmp_path)])
        assert rc == 1
        assert "no ledger found" in capsys.readouterr().err

    def test_empty_ledger_after_init(self, repo, capsys):
        run(["init", str(repo)])
        rc = run(["status", str(repo)])
        assert rc == 0
        assert "ledger is empty" in capsys.readouterr().out

    def test_lists_tasks_with_branch(self, repo, capsys):
        from orchestrator.core.config import ledger_path
        from orchestrator.core.ledger import Ledger

        run(["init", str(repo)])
        ledger = Ledger.load(ledger_path(repo))
        task = ledger.create_task("Do work", branch="agent/task-001-work")
        ledger.update_status(task.id, "WORKING")
        ledger.save()

        rc = run(["status", str(repo)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "TASK-001" in out
        assert "WORKING" in out
        assert "agent/task-001-work" in out

    def test_unexpected_failure_returns_clean_error_and_logs_traceback(
        self, tmp_path, capsys, monkeypatch
    ):
        def explode(args):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("orchestrator.cli.cmd_status", explode)
        rc = run(["status", str(tmp_path)])

        assert rc == 1
        assert "RuntimeError: kaboom" in capsys.readouterr().err
        log = tmp_path / ".orchestrator" / "logs" / "orchestrator.log"
        text = log.read_text()
        assert "Traceback" in text
        assert "kaboom" in text


def test_file_log_captures_trace_level(tmp_path):
    from orchestrator.logs import TRACE, get, setup_logging

    path = setup_logging(tmp_path)
    get("test").log(TRACE, "deep event detail")
    for handler in get("test").parent.handlers:
        handler.flush()

    assert "deep event detail" in path.read_text()


class TestVersion:
    def test_version_flag(self, capsys):
        from orchestrator import __version__

        with pytest.raises(SystemExit) as exc:
            run(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestRootResolution:
    def test_explicit_flag_wins(self, tmp_path, monkeypatch):
        from orchestrator.cli import _resolve_root

        monkeypatch.setenv("OC_ORCHESTRATOR_ROOT", "/elsewhere")
        assert _resolve_root(tmp_path) == tmp_path.resolve()

    def test_env_var_fallback(self, tmp_path, monkeypatch):
        from orchestrator.cli import _resolve_root

        monkeypatch.setenv("OC_ORCHESTRATOR_ROOT", str(tmp_path))
        assert _resolve_root(None) == tmp_path.resolve()

    def test_defaults_to_cwd(self, tmp_path, monkeypatch):
        from orchestrator.cli import _resolve_root

        monkeypatch.delenv("OC_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _resolve_root(None) == tmp_path.resolve()
