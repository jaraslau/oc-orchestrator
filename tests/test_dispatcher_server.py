import threading
import time
from pathlib import Path

from orchestrator.runtime.dispatcher import Dispatcher
from tests.conftest import FakeRunner


def wait_until(fn, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()


def spawn(dispatcher, tmp_path, **kwargs):
    worktree = tmp_path / "wt-fake"
    worktree.mkdir(exist_ok=True)
    return dispatcher.spawn(
        config=type(
            "C", (), {"logs_dirname": "logs", "worker_agent": "w", "worker_timeout": 10.0}
        )(),
        task_id="TASK-900",
        branch="agent/task-900",
        worktree=worktree,
        prompt="do things",
        **kwargs,
    )


def test_spawn_runs_and_finalizes_success(repo, tmp_path):
    runner = FakeRunner()
    dispatcher = Dispatcher(repo, runner)
    spawn(dispatcher, tmp_path)
    assert wait_until(lambda: dispatcher.poll("TASK-900").exit_code == 0)
    final = dispatcher.poll("TASK-900")
    assert final.session_id == "ses_1"
    assert final.model_used == "m/default"
    assert "STATUS: DONE" in Path(final.log_path).read_text()


def test_failure_recorded_with_diagnosis(repo, tmp_path):
    dispatcher = Dispatcher(repo, FakeRunner(RuntimeError("502 bad gateway from provider")))
    spawn(dispatcher, tmp_path)
    assert wait_until(lambda: dispatcher.poll("TASK-900").exit_code == 1)
    final = dispatcher.poll("TASK-900")
    assert "502 bad gateway" in Path(final.log_path).read_text()


def test_agent_model_variant_forwarded(repo, tmp_path):
    runner = FakeRunner()
    dispatcher = Dispatcher(repo, runner)
    spawn(
        dispatcher,
        tmp_path,
        model="prov/m1",
        agent_name="orchestrator-tester",
        variant="high",
    )
    assert wait_until(lambda: len(runner.calls) == 1)
    assert runner.calls[0]["model"] == "prov/m1"
    assert runner.calls[0]["agent"] == "orchestrator-tester"
    assert runner.calls[0]["variant"] == "high"


def test_terminate_aborts_live_session(repo, tmp_path):
    gate = threading.Event()
    runner = FakeRunner(lambda prompt, cwd: (gate.wait(timeout=5), "late")[1])
    dispatcher = Dispatcher(repo, runner)
    spawn(dispatcher, tmp_path)
    assert wait_until(lambda: dispatcher._handles.get("TASK-900"))
    assert dispatcher.terminate("TASK-900") is True
    assert runner.aborted == ["ses_1"]
    gate.set()


def test_poll_returns_none_for_unknown_task(repo):
    assert Dispatcher(repo, FakeRunner()).poll("NOPE") is None
