"""Background event tap: consumes the server SSE stream for observability.

Collects per-session errors (used by the runner for failover decisions) and
logs activity so `--verbose` shows what every worker is doing live.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from orchestrator.logs import get
from orchestrator.runtime.client import OpencodeApiError, OpencodeClient

log = get("events")


class EventTap:
    def __init__(self, client: OpencodeClient) -> None:
        self.client = client
        self.errors: dict[str, dict[str, Any]] = {}
        self.activity: dict[str, tuple[float, str]] = {}
        self.last_event = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            log.debug("event tap already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="opencode-events", daemon=True)
        self._thread.start()
        log.info("event tap started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("event tap stopped")

    def pop_error(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.errors.pop(session_id, None)

    def watch(self, session_id: str) -> None:
        with self._lock:
            self.activity[session_id] = (time.monotonic(), "awaiting first event")

    def forget(self, session_id: str) -> None:
        with self._lock:
            self.activity.pop(session_id, None)
            self.errors.pop(session_id, None)

    def progress(self, session_id: str) -> tuple[float, str]:
        with self._lock:
            return self.activity[session_id]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for event in self.client.events():
                    if self._stop.is_set():
                        return
                    self._handle(event)
            except httpx.HTTPError as exc:
                log.warning("event stream interrupted; reconnecting: %s", exc)
            except OpencodeApiError as exc:
                log.warning("event stream failed; reconnecting: %s", exc)
            except Exception as exc:
                log.warning("event tap error: %s", exc)
            if not self._stop.wait(timeout=1.0):
                continue
            return

    def _handle(self, event: dict[str, Any]) -> None:
        self.last_event = time.monotonic()
        etype = event.get("type", "")
        props = event.get("properties") or {}
        part = props.get("part") or {}
        sid = (
            props.get("sessionID")
            or (props.get("info") or {}).get("sessionID")
            or part.get("sessionID")
            or ""
        )
        description = etype
        if etype == "message.part.updated":
            description = f"{part.get('type', '?')} {part.get('tool', '')} "
            description += (part.get("state") or {}).get("status", "")
        # Server heartbeats and repeated busy statuses are not worker progress.
        if sid and etype in {"message.part.updated", "message.part.delta", "message.updated"}:
            with self._lock:
                if sid in self.activity:
                    self.activity[sid] = (self.last_event, description.strip())
        if etype == "session.error":
            error = props.get("error") or props
            name = error.get("name", "UnknownError") if isinstance(error, dict) else str(error)
            data = error.get("data") or {} if isinstance(error, dict) else {}
            message = (
                error.get("message") or data.get("message", "") if isinstance(error, dict) else ""
            )
            if data.get("statusCode"):
                message = f"{data['statusCode']}: {message}"
            if sid:
                with self._lock:
                    self.errors[sid] = {"name": name, "message": str(message)}
            log.error("session %s error: %s %s", sid or "?", name, str(message)[:300])
            return
        if etype == "message.part.updated":
            part = props.get("part") or {}
            ptype = part.get("type", "?")
            if ptype == "tool":
                tool = part.get("tool", "?")
                state = (part.get("state") or {}).get("status", "")
                log.info(
                    "session=%s tool=%s status=%s call=%s", sid, tool, state, part.get("callID")
                )
            elif ptype == "step-start":
                log.info("[%s] step started", sid[-8:])
            else:
                log.log(5, "[%s] part %s", sid[-8:], ptype)
            return
        if etype == "session.idle":
            log.debug("session %s idle", sid)
            return
        if etype in {"permission.asked", "question.asked"}:
            log.warning(
                "session=%s waiting for operator: %s request=%s", sid, etype, props.get("id")
            )
            with self._lock:
                if sid in self.activity:
                    last, _ = self.activity[sid]
                    self.activity[sid] = (last, etype)
        log.log(5, "event %s", etype)
