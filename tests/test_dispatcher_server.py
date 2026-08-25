import threading
import time

from orchestrator.runtime.client import SessionHandle
from orchestrator.runtime.dispatcher import Dispatcher


def wait_until(fn, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()


class FakeRunner:
    """Stands in for SessionRunner inside Dispatcher's server engine."""

    def __init__(self, behavior=None):
        self.calls: list[dict] = []
        self.behavior = behavior or (lambda prompt, cwd: "```handoff\nTASK: T\nSTATUS: DONE\n```\n")
        self.handles: list[SessionHandle] = []
        self.aborted: list[str] = []

    def run(
        self, prompt, cwd, *, agent=None, model=None, variant=None, timeout=None, on_session=None
    ):
        self.calls.append(
            {"prompt": prompt, "cwd": cwd, "agent": agent, "model": model, "variant": variant}
        )
        handle = SessionHandle(f"ses_{len(self.calls)}", str(cwd), model or "")
        if on_session is not None:
            on_session(handle)
        text = self.behavior(prompt, cwd)
        return type(
            "R",
            (),
            {
                "text": text,
                "session_id": handle.session_id,
                "models_tried": [model or "m/default"],
                "failed_over": False,
            },
        )

    def abort_session(self, handle):
        self.aborted.append(handle.session_id)


def spawn(dispatcher_factory, repo, tmp_path, runner, **kwargs):
    dispatcher = dispatcher_factory(repo, runner)
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


class TestServerEngine:
    def test_spawn_runs_and_finalizes_success(self, repo, tmp_path):
        from orchestrator.runtime.dispatcher import Dispatcher

        runner = FakeRunner()
        d = Dispatcher(repo, runner=runner)
        record = spawn(lambda r, run: Dispatcher(r, runner=run), repo, tmp_path, runner)
        assert record.engine == "server"
        assert record.pid is None
        assert wait_until(lambda: d.poll("TASK-900").exit_code == 0)
        final = d.poll("TASK-900")
        assert final.session_id == "ses_1"
        assert final.model_used == "m/default"
        with open(final.log_path) as f:
            log_text = f.read()
        assert "STATUS: DONE" in log_text

    def test_failure_recorded_with_diagnosis(self, repo, tmp_path):
        def explode(prompt, cwd):
            raise RuntimeError("502 bad gateway from provider")

        runner = FakeRunner(behavior=explode)
        d = Dispatcher(repo, runner=runner)
        spawn(lambda r, run: d, repo, tmp_path, runner)
        assert wait_until(lambda: d.poll("TASK-900").exit_code == 1)
        final = d.poll("TASK-900")
        with open(final.log_path) as f:
            assert "502 bad gateway" in f.read()

    def test_agent_model_variant_forwarded(self, repo, tmp_path):
        runner = FakeRunner()
        spawn(
            lambda r, run: Dispatcher(r, runner=run),
            repo,
            tmp_path,
            runner,
            model="prov/m1",
            agent_name="orchestrator-tester",
            variant="high",
        )
        wait_until(lambda: len(runner.calls) == 1)
        call = runner.calls[0]
        assert call["model"] == "prov/m1"
        assert call["agent"] == "orchestrator-tester"
        assert call["variant"] == "high"

    def test_terminate_aborts_live_session(self, repo, tmp_path):
        gate = threading.Event()

        def blocked(prompt, cwd):
            gate.wait(timeout=5)
            return "late"

        runner = FakeRunner(behavior=blocked)
        d = Dispatcher(repo, runner=runner)
        spawn(lambda r, run: d, repo, tmp_path, runner)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not d._handles.get("TASK-900"):
            time.sleep(0.02)
        assert d.terminate("TASK-900") is True
        assert runner.aborted == ["ses_1"]
        gate.set()

    def test_poll_returns_record_for_unknown_task(self, repo, tmp_path):
        from orchestrator.runtime.dispatcher import Dispatcher

        d = Dispatcher(repo, runner=FakeRunner())
        assert d.poll("NOPE") is None


class TestCliEngineUnchanged:
    def test_registry_roundtrip(self, repo, fake_worker, tmp_path):
        from orchestrator.core.config import Config
        from orchestrator.runtime.dispatcher import Dispatcher

        d = Dispatcher(repo)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        config = Config(opencode_bin=fake_worker("echo ok"), execution_backend="cli")
        record = d.spawn(
            config=config,
            task_id="TASK-901",
            branch="b",
            worktree=worktree,
            prompt="p",
        )
        assert record.engine == "cli"
        assert record.pid is not None
        assert wait_until(lambda: d.poll("TASK-901").exit_code == 0)
