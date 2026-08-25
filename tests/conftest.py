import subprocess

import pytest

from orchestrator.core.config import Config, save_config


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


@pytest.fixture()
def fake_worker(tmp_path):
    """Factory for fake `opencode` binaries with scriptable behavior."""

    def make(body: str) -> str:
        bindir = tmp_path / "fakebin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "opencode"
        exe.write_text(f"#!/bin/sh\n{body}\n")
        exe.chmod(0o755)
        return str(exe)

    return make


HANDOFF_OK = (
    'echo "working..."\n'
    "cat <<'BLOCK'\n"
    "```handoff\n"
    "TASK: TASK-001\n"
    "STATUS: DONE\n"
    "BRANCH: agent/task-001-demo\n"
    "COMMIT: abc1234\n"
    "SUMMARY: did the thing\n"
    "FILES CHANGED: src/x.py\n"
    "TESTS RUN: pytest\n"
    "TEST RESULTS: 3 passed\n"
    "KNOWN ISSUES: none\n"
    "NOTES FOR MANAGER: none\n"
    "```\n"
    "BLOCK\n"
)

HANDOFF_FAIL = (
    "cat <<'BLOCK'\n"
    "```handoff\n"
    "TASK: TASK-001\n"
    "STATUS: FAILED\n"
    "SUMMARY: could not do the thing\n"
    "```\n"
    "BLOCK\n"
    "exit 0\n"
)


def configured(repo, worker_bin: str) -> Config:
    config = Config(opencode_bin=worker_bin, execution_backend="cli")
    save_config(repo, config)
    return config


def wait_until(fn, timeout: float = 8.0, interval: float = 0.05):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()
