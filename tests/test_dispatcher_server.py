import threading
from dataclasses import replace
from pathlib import Path

from orchestrator.core.config import Config
from orchestrator.runtime.dispatcher import Dispatcher, DispatchRecord
from tests.conftest import FakeRunner, wait_until


def spawn(
    dispatcher: Dispatcher,
    tmp_path: Path,
    *,
    model: str | None = None,
    agent_name: str | None = None,
    variant: str | None = None,
) -> DispatchRecord:
    worktree = tmp_path / "wt-fake"
    worktree.mkdir(exist_ok=True)
    return dispatcher.spawn(
        config=Config(logs_dirname="logs", worker_agent="w", worker_timeout=10.0),
        task_id="TASK-900",
        branch="agent/task-900",
        worktree=worktree,
        prompt="do things",
        model=model,
        agent_name=agent_name,
        variant=variant,
    )


def test_spawn_runs_and_finalizes_success(repo: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    dispatcher = Dispatcher(repo, runner)
    spawn(dispatcher, tmp_path)
    assert wait_until(
        lambda: (record := dispatcher.poll("TASK-900")) is not None and record.exit_code == 0
    )
    final = dispatcher.poll("TASK-900")
    assert final is not None
    assert final.session_id == "ses_1"
    assert final.model_used == "m/default"
    assert "STATUS: DONE" in Path(final.log_path).read_text()


def test_failure_recorded_with_diagnosis(repo: Path, tmp_path: Path) -> None:
    dispatcher = Dispatcher(repo, FakeRunner(RuntimeError("502 bad gateway from provider")))
    spawn(dispatcher, tmp_path)
    assert wait_until(
        lambda: (record := dispatcher.poll("TASK-900")) is not None and record.exit_code == 1
    )
    final = dispatcher.poll("TASK-900")
    assert final is not None
    assert "502 bad gateway" in Path(final.log_path).read_text()


def test_agent_model_variant_forwarded(repo: Path, tmp_path: Path) -> None:
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


def test_terminate_aborts_live_session(repo: Path, tmp_path: Path) -> None:
    gate = threading.Event()
    runner = FakeRunner(lambda prompt, cwd: (gate.wait(timeout=5), "late")[1])
    dispatcher = Dispatcher(repo, runner)
    spawn(dispatcher, tmp_path)
    assert wait_until(lambda: dispatcher._handles.get("TASK-900"))
    assert dispatcher.terminate("TASK-900") is True
    assert runner.aborted == ["ses_1"]
    gate.set()


def test_poll_returns_none_for_unknown_task(repo: Path) -> None:
    assert Dispatcher(repo, FakeRunner()).poll("NOPE") is None


def test_concurrent_registry_updates_preserve_every_task(repo: Path, tmp_path: Path) -> None:
    dispatcher = Dispatcher(repo, FakeRunner())
    template = spawn(dispatcher, tmp_path)
    records = [replace(template, task_id=f"TASK-{i:03d}") for i in range(20)]
    threads = [
        threading.Thread(target=dispatcher._store_record, args=(record,)) for record in records
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    registry = dispatcher._load_registry()
    assert all(record.task_id in registry for record in records)
