from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.cli import MANAGER_AGENT_FILENAME, WORKER_AGENT_FILENAME, build_parser, main
from orchestrator.core.ledger import TaskStatus


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def run(argv: list[str]) -> int:
    return main(argv)


def test_run_pr_flag() -> None:
    args = build_parser().parse_args(["run", "ship it", "--pr"])
    assert args.pr is True


class TestInit:
    def test_creates_expected_files(self, repo: Path, capsys: Any) -> None:
        rc: int = run(["init", str(repo)])
        out: str = capsys.readouterr().out
        assert rc == 0
        assert (repo / ".orchestrator" / "config.json").exists()
        assert (repo / ".orchestrator" / "ledger.json").exists()
        assert (repo / ".opencode" / "agent" / MANAGER_AGENT_FILENAME).exists()
        assert (repo / ".opencode" / "agent" / WORKER_AGENT_FILENAME).exists()
        opencode_cfg: dict[str, Any] = json.loads(
            (repo / ".opencode" / "opencode.json").read_text()
        )
        assert opencode_cfg["$schema"].startswith("https://")
        entry: dict[str, Any] = opencode_cfg["mcp"]["oc-orchestrator"]
        # regression: generated config must be machine-independent (no absolute paths)
        assert entry["command"] == ["oc-orchestrator", "serve"]
        assert str(repo) not in json.dumps(opencode_cfg)
        assert entry["enabled"] is True
        manager_md: str = (repo / ".opencode" / "agent" / MANAGER_AGENT_FILENAME).read_text()
        worker_md: str = (repo / ".opencode" / "agent" / WORKER_AGENT_FILENAME).read_text()
        assert "Repository Manager Agent" in manager_md
        assert "dispatch_task" in manager_md
        assert "```handoff" in worker_md
        roles_dir: Path = repo / ".opencode" / "agent"
        assert (roles_dir / "orchestrator-tester.md").exists()
        assert (roles_dir / "orchestrator-reviewer.md").exists()
        assert "initialized orchestration state" in out

    def test_idempotent_rerun(self, repo: Path) -> None:
        run(["init", str(repo)])
        rc: int = run(["init", str(repo)])
        assert rc == 0
        gitignore: str = (repo / ".gitignore").read_text()
        assert gitignore.count(".orchestrator/") == 1
        json.loads((repo / ".opencode" / "opencode.json").read_text())

    def test_init_never_clobbers_existing_ledger(self, repo: Path) -> None:
        from orchestrator.core.config import ledger_path
        from orchestrator.core.ledger import Ledger

        run(["init", str(repo)])
        lg = Ledger(ledger_path(repo))
        t = lg.create_task("precious history")
        lg.update_status(t.id, TaskStatus.REVIEWING)
        lg.save()

        rc: int = run(["init", str(repo)])
        assert rc == 0
        restored = Ledger.load(ledger_path(repo)).get(t.id)
        assert restored.status == TaskStatus.REVIEWING

    def test_preserves_existing_opencode_config(self, repo: Path) -> None:
        cfg_dir: Path = repo / ".opencode"
        cfg_dir.mkdir()
        existing: dict[str, Any] = {"theme": "monokai", "mcp": {"other": {"type": "local"}}}
        (cfg_dir / "opencode.json").write_text(json.dumps(existing))

        rc: int = run(["init", str(repo)])
        assert rc == 0
        merged: dict[str, Any] = json.loads((cfg_dir / "opencode.json").read_text())
        assert merged["theme"] == "monokai"
        assert set(merged["mcp"]) == {"other", "oc-orchestrator"}

    def test_invalid_opencode_json_aborts_without_touching_file(self, repo: Path) -> None:
        cfg_dir: Path = repo / ".opencode"
        cfg_dir.mkdir()
        broken: str = '{"mcp": oops'
        (cfg_dir / "opencode.json").write_text(broken)

        rc: int = run(["init", str(repo)])
        assert rc == 1
        assert (cfg_dir / "opencode.json").read_text() == broken

    def test_warns_outside_git_repo(self, tmp_path: Path, capsys: Any) -> None:
        rc: int = run(["init", str(tmp_path)])
        err: str = capsys.readouterr().err
        assert rc == 0
        assert "does not look like a git repository" in err


class TestStatus:
    def test_fails_before_init(self, tmp_path: Path, capsys: Any) -> None:
        rc: int = run(["status", str(tmp_path)])
        assert rc == 1
        assert "no ledger found" in capsys.readouterr().err

    def test_empty_ledger_after_init(self, repo: Path, capsys: Any) -> None:
        run(["init", str(repo)])
        rc: int = run(["status", str(repo)])
        assert rc == 0
        assert "ledger is empty" in capsys.readouterr().out

    def test_lists_tasks_with_branch(self, repo: Path, capsys: Any) -> None:
        from orchestrator.core.config import ledger_path
        from orchestrator.core.ledger import Ledger

        run(["init", str(repo)])
        ledger = Ledger.load(ledger_path(repo))
        task = ledger.create_task("Do work", branch="agent/task-001-work")
        ledger.update_status(task.id, "WORKING")
        ledger.save()

        rc: int = run(["status", str(repo)])
        out: str = capsys.readouterr().out
        assert rc == 0
        assert "TASK-001" in out
        assert "WORKING" in out
        assert "agent/task-001-work" in out

    def test_unexpected_failure_returns_clean_error_and_logs_traceback(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        def explode(args: Any) -> int:
            raise RuntimeError("kaboom")

        monkeypatch.setattr("orchestrator.cli.cmd_status", explode)
        rc: int = run(["status", str(tmp_path)])

        assert rc == 1
        assert "RuntimeError: kaboom" in capsys.readouterr().err
        log: Path = tmp_path / ".orchestrator" / "logs" / "orchestrator.log"
        text: str = log.read_text()
        assert "Traceback" in text
        assert "kaboom" in text


def test_file_log_captures_trace_level(tmp_path: Path) -> None:
    from orchestrator.logs import TRACE, get, setup_logging

    path: Path = setup_logging(tmp_path)
    get("test").log(TRACE, "deep event detail")
    parent = get("test").parent
    assert parent is not None
    for handler in parent.handlers:
        handler.flush()

    assert "deep event detail" in path.read_text()


class TestVersion:
    def test_version_flag(self, capsys: Any) -> None:
        from orchestrator import __version__

        with pytest.raises(SystemExit) as exc:
            run(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestRootResolution:
    def test_explicit_flag_wins(self, tmp_path: Path, monkeypatch: Any) -> None:
        from orchestrator.cli import _resolve_root

        monkeypatch.setenv("OC_ORCHESTRATOR_ROOT", "/elsewhere")
        assert _resolve_root(tmp_path) == tmp_path.resolve()

    def test_env_var_fallback(self, tmp_path: Path, monkeypatch: Any) -> None:
        from orchestrator.cli import _resolve_root

        monkeypatch.setenv("OC_ORCHESTRATOR_ROOT", str(tmp_path))
        assert _resolve_root(None) == tmp_path.resolve()

    def test_defaults_to_cwd(self, tmp_path: Path, monkeypatch: Any) -> None:
        from orchestrator.cli import _resolve_root

        monkeypatch.delenv("OC_ORCHESTRATOR_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _resolve_root(None) == tmp_path.resolve()
