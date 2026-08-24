"""Background worker process management.

Workers run as detached `opencode run` subprocesses inside per-task
worktrees. State survives orchestrator restarts via a registry file;
exit codes are only recoverable in-process, so post-restart completion
is inferred from liveness plus the handoff block in the log.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.config import Config, state_dir
from orchestrator.storage import read_json, write_json_atomic

DISPATCHES_FILENAME = "dispatches.json"
HANDOFF_RE = re.compile(r"```handoff\s*\n(.*?)```", re.DOTALL)
LOG_TAIL_BYTES = 65536


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class DispatchRecord:
    task_id: str
    pid: int | None
    branch: str
    worktree: str
    log_path: str
    started_at: str
    exit_code: int | None = None
    ended_at: str | None = None


def parse_handoff(text: str) -> dict[str, str] | None:
    """Parse the last ```handoff fenced block into a dict, or return None."""
    matches = HANDOFF_RE.findall(text)
    if not matches:
        return None
    result: dict[str, str] = {}
    current: str | None = None
    for line in matches[-1].splitlines():
        m = re.match(r"^([A-Z][A-Z ]*?):\s?(.*)$", line)
        if m:
            current = m.group(1).strip()
            result[current] = m.group(2).strip()
        elif line.strip() and current:
            result[current] += "\n" + line.strip()
    return result or None


class Dispatcher:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._popens: dict[str, subprocess.Popen[bytes]] = {}

    @property
    def registry_path(self) -> Path:
        return state_dir(self.root) / DISPATCHES_FILENAME

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        if not self.registry_path.exists():
            return {}
        data = read_json(self.registry_path)
        return data.get("dispatches", {})

    def _save_registry(self, dispatches: dict[str, dict[str, Any]]) -> None:
        write_json_atomic(self.registry_path, {"dispatches": dispatches})

    @staticmethod
    def build_command(
        config: Config,
        worktree: Path,
        prompt: str,
        model: str | None = None,
    ) -> list[str]:
        cmd = [config.opencode_bin, "run", "--auto", "--dir", str(worktree)]
        if model:
            cmd += ["-m", model]
        cmd += ["--agent", config.worker_agent, prompt]
        return cmd

    def spawn(
        self,
        *,
        config: Config,
        task_id: str,
        branch: str,
        worktree: Path,
        prompt: str,
        model: str | None = None,
    ) -> DispatchRecord:
        logs_dir = state_dir(self.root) / config.logs_dirname
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"{task_id.lower()}.log"

        cmd = self.build_command(config, worktree, prompt, model)

        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=worktree,
                start_new_session=True,
            )

        record = DispatchRecord(
            task_id=task_id,
            pid=proc.pid,
            branch=branch,
            worktree=str(worktree),
            log_path=str(log_path),
            started_at=_now(),
        )
        self._popens[task_id] = proc
        registry = self._load_registry()
        registry[task_id] = asdict(record)
        self._save_registry(registry)
        return record

    def poll(self, task_id: str) -> DispatchRecord | None:
        """Return the current record, updating exit status when discoverable."""
        registry = self._load_registry()
        raw = registry.get(task_id)
        if raw is None:
            return None
        record = DispatchRecord(**raw)
        if record.exit_code is not None:
            return record

        proc = self._popens.get(task_id)
        if proc is not None:
            code = proc.poll()
            if code is not None:
                self._finalize(record, code, registry)
            return record

        # Post-restart: no Popen handle; infer from liveness.
        try:
            os.kill(record.pid or 0, 0)
        except ProcessLookupError:
            self._finalize(record, None, registry)  # ended while we were away
        except PermissionError:
            pass  # owned by another user but running
        return record

    def _finalize(
        self,
        record: DispatchRecord,
        exit_code: int | None,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        record.exit_code = exit_code
        record.ended_at = _now()
        registry[record.task_id] = asdict(record)
        self._save_registry(registry)

    def terminate(self, task_id: str) -> bool:
        """Terminate a running worker (SIGTERM to its process group)."""
        proc = self._popens.get(task_id)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            return True
        record = self.poll(task_id)
        if record is None or record.exit_code is not None or record.pid is None:
            return False
        try:
            os.killpg(record.pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def read_log(record: DispatchRecord, tail_bytes: int = LOG_TAIL_BYTES) -> str:
        path = Path(record.log_path)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read()


def wait_for_completion(
    dispatcher: Dispatcher, task_id: str, timeout: float
) -> DispatchRecord | None:
    """Poll until the worker finishes or the timeout elapses. Test helper."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = dispatcher.poll(task_id)
        if record is not None and (record.exit_code is not None or record.ended_at):
            return record
        time.sleep(0.05)
    return dispatcher.poll(task_id)


__all__ = [
    "DISPATCHES_FILENAME",
    "DispatchRecord",
    "Dispatcher",
    "parse_handoff",
    "wait_for_completion",
]
