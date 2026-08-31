"""Background worker management for sessions on a shared opencode server."""

from __future__ import annotations

import re
import threading
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from orchestrator.core.config import Config, state_dir
from orchestrator.core.storage import read_json, write_json_atomic
from orchestrator.logs import get

log = get("dispatcher")

DISPATCHES_FILENAME = "dispatches.json"
HANDOFF_RE = re.compile(r"```handoff\s*\n(.*?)```", re.DOTALL)
LOG_TAIL_BYTES = 65536


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class DispatchRecord:
    task_id: str
    branch: str
    worktree: str
    log_path: str
    started_at: str
    exit_code: int | None = None
    ended_at: str | None = None
    session_id: str | None = None
    model_used: str | None = None


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
    def __init__(self, root: Path, runner: Any) -> None:
        self.root = root
        self._runner = runner
        self._handles: dict[str, Any] = {}
        self._registry_lock = threading.RLock()

    @property
    def registry_path(self) -> Path:
        return state_dir(self.root) / DISPATCHES_FILENAME

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        with self._registry_lock:
            if not self.registry_path.exists():
                return {}
            data = read_json(self.registry_path)
            return cast(dict[str, dict[str, Any]], data.get("dispatches", {}))

    def _save_registry(self, dispatches: dict[str, dict[str, Any]]) -> None:
        with self._registry_lock:
            write_json_atomic(self.registry_path, {"dispatches": dispatches})

    def _store_record(self, record: DispatchRecord) -> None:
        """Atomically merge one record into the shared dispatch registry."""
        with self._registry_lock:
            registry = self._load_registry()
            registry[record.task_id] = asdict(record)
            self._save_registry(registry)
        log.debug(
            "dispatch record stored: task=%s exit=%s session=%s",
            record.task_id,
            record.exit_code,
            record.session_id,
        )

    def spawn(
        self,
        *,
        config: Config,
        task_id: str,
        branch: str,
        worktree: Path,
        prompt: str,
        model: str | None = None,
        agent_name: str | None = None,
        variant: str | None = None,
    ) -> DispatchRecord:
        logs_dir = state_dir(self.root) / config.logs_dirname
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"{task_id.lower()}.log"

        record = DispatchRecord(
            task_id=task_id,
            branch=branch,
            worktree=str(worktree),
            log_path=str(log_path),
            started_at=_now(),
        )
        self._store_record(record)
        log.info(
            "worker thread starting: task=%s branch=%s agent=%s model=%s variant=%s",
            task_id,
            branch,
            agent_name or config.worker_agent,
            model or "default",
            variant or "default",
        )

        def work() -> None:
            self._handles[task_id] = None

            def capture_session(handle: Any) -> None:
                self._handles[task_id] = handle
                record.session_id = handle.session_id
                self._store_record(record)

            try:
                result = self._runner.run(
                    prompt,
                    worktree,
                    agent=agent_name or config.worker_agent,
                    model=model,
                    variant=variant,
                    timeout=config.worker_timeout,
                    on_session=capture_session,
                )
                record.session_id = result.session_id
                record.model_used = result.models_tried[-1] if result.models_tried else model
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"\n[session: {result.session_id}; "
                        f"model used: {record.model_used or 'server-default'}]\n\n"
                        f"{result.text}\n"
                    )
                self._finalize(record, 0)
                log.info("task %s completed (session %s)", task_id, result.session_id)
            except Exception as exc:
                diagnosis = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                log.exception("task %s failed: %s", task_id, diagnosis)
                try:
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(f"\n[orchestrator] worker failed: {diagnosis}\n")
                except OSError:
                    log.exception("could not append failure to worker log: %s", log_path)
                if record.model_used is None:
                    record.model_used = model or ""
                self._finalize(record, 1)
            finally:
                self._handles.pop(task_id, None)

        thread = threading.Thread(target=work, name=f"worker-{task_id}", daemon=True)
        thread.start()
        log.debug("worker thread dispatched: task=%s thread=%s", task_id, thread.name)
        return record

    def poll(self, task_id: str) -> DispatchRecord | None:
        """Return the current record, updating exit status when discoverable."""
        with self._registry_lock:
            raw = self._load_registry().get(task_id)
        if raw is None:
            log.log(5, "poll: no dispatch record for %s", task_id)
            return None
        record = DispatchRecord(**raw)
        log.log(5, "poll: task=%s exit=%s ended=%s", task_id, record.exit_code, record.ended_at)
        if record.exit_code is not None:
            return record

        return record

    def _finalize(
        self,
        record: DispatchRecord,
        exit_code: int | None,
    ) -> None:
        record.exit_code = exit_code
        record.ended_at = _now()
        self._store_record(record)
        log.info("worker finalized: task=%s exit=%s", record.task_id, exit_code)

    def terminate(self, task_id: str) -> bool:
        """Abort a running worker session."""
        handle = self._handles.get(task_id)
        if handle is not None:
            self._runner.abort_session(handle)
            log.info("aborted session %s for task %s", handle.session_id, task_id)
            return True
        log.debug("no live session to abort for task %s", task_id)
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
