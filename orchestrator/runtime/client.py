"""Typed HTTP client for a running opencode server.

Thin wrapper over the endpoints this orchestrator needs; raises
OpencodeApiError with full status+body so resilience can classify.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx


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
        try:
            response = httpx.request(
                method, f"{self.base_url}{path}", timeout=timeout or self._timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise OpencodeApiError(0, f"connection failure: {exc}") from exc
        if response.status_code >= 400:
            raise OpencodeApiError(response.status_code, response.text)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

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

    def prompt_async(
        self,
        session_id: str,
        text: str,
        model: str,
        agent: str | None,
        variant: str | None,
        directory: str,
    ) -> None:
        parts = [{"type": "text", "text": text}]
        call = dict(parts=parts, model=_model_param(model))
        if agent:
            call["agent"] = agent
        if variant:
            call["variant"] = variant
        self._request(
            "POST", f"/session/{session_id}/prompt_async?directory={_q(directory)}", json=call
        )

    def status(self) -> dict[str, dict[str, Any]]:
        return self._request("GET", "/session/status") or {}

    def messages(self, session_id: str, directory: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/session/{session_id}/message?directory={_q(directory)}")
        if isinstance(data, list):
            return data
        return list((data or {}).get("rows", []))

    def session_alive(self, session_id: str) -> bool:
        """Check if a session is still in the server's status map."""
        status = self.status()
        return session_id in status

    def abort(self, session_id: str, directory: str) -> None:
        try:
            self._request("POST", f"/session/{session_id}/abort?directory={_q(directory)}")
        except OpencodeApiError as exc:
            if "not found" not in exc.body.lower():
                raise

    def events(self) -> Iterator[dict[str, Any]]:
        with httpx.stream("GET", f"{self.base_url}/event", timeout=None) as response:
            if response.status_code >= 400:
                raise OpencodeApiError(response.status_code, "event stream failed")
            buffer: list[str] = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    buffer.append(line[5:].strip())
                    continue
                if line.strip() or not buffer:
                    continue
                    with contextlib.suppress(json.JSONDecodeError):
                        yield json.loads("\n".join(buffer))
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
