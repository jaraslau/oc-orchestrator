import subprocess
from types import SimpleNamespace

import pytest

from orchestrator.core.config import Config, save_config
from orchestrator.orchestration import service
from orchestrator.runtime.client import SessionHandle
from orchestrator.runtime.dispatcher import Dispatcher


@pytest.fixture()
def repo(tmp_path):
    """A scratch git repo with one commit on main."""
    r = tmp_path / "repo"
    r.mkdir()
    env_args = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "readme.md").write_text("scratch\n")
    subprocess.run(["git", "-C", str(r), *env_args, "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), *env_args, "commit", "-qm", "init"], check=True)
    agents = r / ".opencode" / "agent"
    agents.mkdir(parents=True)
    (agents / "orchestrator-worker.md").write_text(
        "---\ndescription: default worker\nmode: primary\n---\nworker stub\n"
    )
    return r


HANDOFF_OK = """```handoff
TASK: TASK-001
STATUS: DONE
BRANCH: agent/task-001-demo
COMMIT: abc1234
SUMMARY: did the thing
FILES CHANGED: src/x.py
TESTS RUN: pytest
TEST RESULTS: 3 passed
KNOWN ISSUES: none
NOTES FOR MANAGER: none
```"""

HANDOFF_FAIL = """```handoff
TASK: TASK-001
STATUS: FAILED
SUMMARY: could not do the thing
```"""


class FakeRunner:
    def __init__(self, behavior=HANDOFF_OK):
        self.behavior = behavior
        self.calls = []
        self.aborted = []

    def run(
        self, prompt, cwd, *, agent=None, model=None, variant=None, timeout=None, on_session=None
    ):
        self.calls.append(
            {"prompt": prompt, "cwd": cwd, "agent": agent, "model": model, "variant": variant}
        )
        handle = SessionHandle(f"ses_{len(self.calls)}", str(cwd))
        if on_session is not None:
            on_session(handle)
        if isinstance(self.behavior, Exception):
            raise self.behavior
        text = self.behavior(prompt, cwd) if callable(self.behavior) else self.behavior
        return SimpleNamespace(
            text=text,
            session_id=handle.session_id,
            models_tried=[model or "m/default"],
        )

    def abort_session(self, handle):
        self.aborted.append(handle.session_id)


def configured(repo, behavior=HANDOFF_OK) -> FakeRunner:
    config = Config()
    save_config(repo, config)
    runner = FakeRunner(behavior)
    service._dispatchers[str(repo.resolve())] = Dispatcher(repo, runner)
    return runner


def wait_until(fn, timeout: float = 8.0, interval: float = 0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()
