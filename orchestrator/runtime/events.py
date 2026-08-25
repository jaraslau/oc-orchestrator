"""Background event tap: consumes the server SSE stream for observability.

Collects per-session errors (used by the runner for failover decisions) and
logs activity so `--verbose` shows what every worker is doing live.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

import httpx

from orchestrator.logs import get
from orchestrator.runtime.client import OpencodeApiError, OpencodeClient

log = get("events")

_ACTIVITY_LIMIT = 200


class EventTap:
    def __init__(self, client: OpencodeClient) -> None:
        self.client = client
        self.errors: dict[str, dict[str, Any]] = {}
        self.activity: deque[tuple[str, str]] = deque(maxlen=_ACTIVITY_LIMIT)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="opencode-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def pop_error(self, session_id: str) -> dict[str, Any] | None:
        return self.errors.pop(session_id, None)

    def _record(self, session_id: str, kind: str) -> None:
        self.activity.append((session_id, kind))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for event in self.client.events():
                    if self._stop.is_set():
                        return
                    self._handle(event)
            except httpx.HTTPError as exc:
                log.debug("event stream interrupted: %s", exc)
            except OpencodeApiError as exc:
                log.debug("event stream failed: %s", exc)
            except Exception as exc:
                log.warning("event tap error: %s", exc)
            if not self._stop.wait(timeout=1.0):
                continue
            return

    def _handle(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        props = event.get("properties") or {}
        sid = props.get("sessionID") or (props.get("info") or {}).get("sessionID") or ""
        if etype == "session.error":
            error = props.get("error") or props
            name = error.get("name", "UnknownError") if isinstance(error, dict) else str(error)
            message = error.get("message", "") if isinstance(error, dict) else ""
            if sid:
                self.errors[sid] = {"name": name, "message": str(message)}
            log.error("session %s error: %s %s", sid or "?", name, str(message)[:300])
            return
        if etype == "message.part.updated":
            part = props.get("part") or {}
            ptype = part.get("type", "?")
            self._record(sid, ptype)
            if ptype == "tool":
                tool = part.get("tool", "?")
                state = (part.get("state") or {}).get("status", "")
                log.info("[%s] tool %s %s", sid[-8:], tool, state)
            elif ptype == "step-start":
                log.info("[%s] step started", sid[-8:])
            else:
                log.log(5, "[%s] part %s", sid[-8:], ptype)
            return
        if etype == "session.idle":
            self._record(sid, "idle")
            log.debug("session %s idle", sid)
            return
        log.log(5, "event %s", etype)
