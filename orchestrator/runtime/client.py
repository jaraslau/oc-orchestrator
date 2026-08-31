"""Typed HTTP client for a running opencode server.

Thin wrapper over the endpoints this orchestrator needs; raises
OpencodeApiError with full status+body so resilience can classify.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from orchestrator.logs import get

log = get("client")


class OpencodeApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"opencode api {status}: {body[:400]}")


@dataclass
class SessionHandle:
    session_id: str
    directory: str


class OpencodeClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _request(
        self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        started = time.monotonic()
        log.debug("HTTP %s %s", method, path)
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                timeout=self._timeout if timeout is None else timeout,
                trust_env=False,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            log.warning(
                "HTTP %s %s timed out after %.3fs",
                method,
                path,
                time.monotonic() - started,
            )
            raise TimeoutError(f"opencode request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            log.warning(
                "HTTP %s %s failed after %.3fs: %s",
                method,
                path,
                time.monotonic() - started,
                exc,
            )
            raise OpencodeApiError(0, f"connection failure: {exc}") from exc
        elapsed = time.monotonic() - started
        log.debug("HTTP %s %s -> %d in %.3fs", method, path, response.status_code, elapsed)
        if response.status_code >= 400:
            log.warning("HTTP %s %s returned %d", method, path, response.status_code)
            raise OpencodeApiError(response.status_code, response.text)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OpencodeApiError(
                response.status_code, f"invalid JSON response: {response.text[:400]}"
            ) from exc

    def providers(self) -> dict[str, list[str]]:
        data = self._request("GET", "/config/providers") or {}
        result: dict[str, list[str]] = {}
        for provider in data.get("providers", []):
            result[provider.get("id", "")] = sorted(provider.get("models", {}).keys())
        return result

    def default_model(self) -> tuple[str, str]:
        data = self._request("GET", "/config/providers") or {}
        default = data.get("default", {}) or {}
        if isinstance(default, dict):
            for provider_id, model_id in default.items():
                if isinstance(model_id, str):
                    return str(provider_id), model_id
        raise OpencodeApiError(0, "server reported no default model")

    def create_session(self, directory: str, title: str) -> str:
        payload = {"title": title}
        session = self._request("POST", f"/session?directory={_q(directory)}", json=payload)
        return str(session["id"])

    def prompt(
        self,
        session_id: str,
        text: str,
        model: str,
        agent: str | None,
        variant: str | None,
        directory: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Send a prompt and wait for OpenCode's final assistant message.

        OpenCode's synchronous message endpoint owns turn completion. Using it
        avoids reconstructing completion from the eventually-consistent status
        map or from an SSE stream that may reconnect.
        """
        parts = [{"type": "text", "text": text}]
        call = dict(parts=parts, model=_model_param(model))
        if agent:
            call["agent"] = agent
        if variant:
            call["variant"] = variant
        response = self._request(
            "POST",
            f"/session/{session_id}/message?directory={_q(directory)}",
            timeout=timeout,
            json=call,
        )
        if not isinstance(response, dict):
            raise OpencodeApiError(0, "server returned no assistant message")
        return response

    def messages(self, session_id: str, directory: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/session/{session_id}/message?directory={_q(directory)}")
        if isinstance(data, list):
            return data
        return list((data or {}).get("rows", []))

    def abort(self, session_id: str, directory: str) -> None:
        try:
            self._request("POST", f"/session/{session_id}/abort?directory={_q(directory)}")
        except OpencodeApiError as exc:
            if "not found" not in exc.body.lower():
                raise

    def events(self) -> Iterator[dict[str, Any]]:
        log.debug("opening event stream: %s/event", self.base_url)
        with httpx.stream(
            "GET", f"{self.base_url}/event", timeout=None, trust_env=False
        ) as response:
            if response.status_code >= 400:
                raise OpencodeApiError(response.status_code, "event stream failed")
            buffer: list[str] = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    buffer.append(line[5:].strip())
                    continue
                if line.strip() or not buffer:
                    continue
                payload = "\n".join(buffer)
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("discarding malformed event frame: %r", payload[:400])
                buffer.clear()


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _model_param(model: str) -> dict[str, str]:
    if "/" in model:
        provider_id, model_id = model.split("/", 1)
    else:
        provider_id, model_id = "", model
    return {"providerID": provider_id, "modelID": model_id}
