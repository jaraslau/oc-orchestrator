"""Server-backed session execution with model failover.

Runs a prompt against the shared opencode server: preflights the model chain,
creates a session, then uses OpenCode's blocking message endpoint to wait for
the completed assistant turn. Provider-sided failures trigger failover to the
next model in the chain; anything else fails fast with full diagnosis.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.logs import get
from orchestrator.runtime.client import OpencodeApiError, OpencodeClient, SessionHandle
from orchestrator.runtime.events import EventTap
from orchestrator.runtime.resilience import (
    PROVIDER_SIDED,
    ErrorKind,
    ModelChain,
    OrchestratorError,
    classify,
    parse_model_ref,
)

log = get("runner")

DEFAULT_POLL_INTERVAL = 0.5


class SessionAbortError(RuntimeError):
    """Do not dispatch another writer when stopping the previous one failed."""


@dataclass
class RunResult:
    text: str
    session_id: str
    models_tried: list[str] = field(default_factory=list)


class SessionRunner:
    def __init__(
        self,
        client: OpencodeClient,
        tap: EventTap,
        fallback_models: list[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        idle_timeout: float = 300.0,
    ) -> None:
        self.client = client
        self.tap = tap
        self.fallbacks = list(fallback_models or [])
        self.poll_interval = poll_interval
        if not math.isfinite(idle_timeout) or idle_timeout <= 0:
            raise ValueError("session_idle_timeout must be finite and positive")
        self.idle_timeout = idle_timeout
        self._default_model: str | None = None

    def default_model(self) -> str:
        if self._default_model is None:
            provider_id, model_id = self.client.default_model()
            self._default_model = f"{provider_id}/{model_id}"
            log.info("server default model: %s", self._default_model)
        return self._default_model

    def resolve_chain(self, requested: str | None) -> ModelChain:
        providers = self.client.providers()
        log.debug("available providers: %s", {k: len(v) for k, v in providers.items()})
        default_provider, default_model_id = self.default_model().split("/", 1)
        providers.setdefault(default_provider, [])
        if default_model_id not in providers[default_provider]:
            providers[default_provider].append(default_model_id)
        if requested is not None:
            try:
                parse_model_ref(requested, providers, default_provider)
            except OrchestratorError:
                log.warning("requested model '%s' unavailable; using fallback chain", requested)
                requested = None
        chain = ModelChain.build(requested, self.fallbacks, self.default_model())
        validated: list[str] = []
        for ref in chain.models:
            try:
                provider_id, model_id = parse_model_ref(ref, providers, default_provider)
                normalized = f"{provider_id}/{model_id}"
                if normalized not in validated:
                    validated.append(normalized)
            except OrchestratorError:
                log.error("chain entry '%s' unavailable on server; dropping", ref)
        chain.models = validated
        if not chain.models:
            raise OrchestratorError("no usable models in chain after validation")
        log.info("model chain: %s", " -> ".join(chain.models))
        if len(chain.models) == 1:
            log.warning(
                "no backup model configured; transient failures can only retry %s", chain.current
            )
        return chain

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        agent: str | None = None,
        model: str | None = None,
        variant: str | None = None,
        timeout: float = 900.0,
        on_session: Callable[[SessionHandle], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> RunResult:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("session timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        chain = self.resolve_chain(model)
        attempts: list[tuple[str, str]] = []
        retried = False
        while not chain.exhausted:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("session cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("session timed out: total model-attempt budget exhausted")
            current = chain.current
            log.info("model attempt %d: %s", len(attempts) + 1, current)
            try:
                result = self._run_single(
                    prompt
                    if not attempts
                    else prompt
                    + (
                        "\n\nA previous session failed. Inspect existing files and commits in this "
                        "worktree and continue from them; preserve partial work."
                    ),
                    cwd,
                    agent=agent,
                    model=current,
                    variant=variant,
                    timeout=remaining,
                    on_session=on_session,
                    cancelled=cancelled,
                )
                result.models_tried = [m for m, _ in attempts] + [current]
                if attempts:
                    log.info(
                        "succeeded on failover model %s after %d failure(s)", current, len(attempts)
                    )
                return result
            except Exception as exc:
                kind = (
                    ErrorKind.TRANSIENT_NETWORK
                    if isinstance(exc, TimeoutError)
                    else classify(str(exc))
                )
                attempts.append((current, f"[{kind.value}] {exc}"))
                log.error("model %s failed (%s): %s", current, kind.value, exc, exc_info=True)
                if (
                    isinstance(exc, SessionAbortError)
                    or kind not in PROVIDER_SIDED
                    or (cancelled is not None and cancelled.is_set())
                ):
                    raise
                if chain.advance(kind.value) is None:
                    if (
                        kind not in {ErrorKind.TRANSIENT_NETWORK, ErrorKind.EMPTY_RESPONSE}
                        or retried
                    ):
                        raise
                    retried = True
                    chain.models.append(current)
                    log.warning("retrying %s once in a fresh session after %s", current, kind.value)
        raise OrchestratorError("model chain exhausted without a result")

    def abort_session(self, handle: SessionHandle) -> None:
        try:
            self.client.abort(handle.session_id, handle.directory)
        except (OpencodeApiError, TimeoutError) as exc:
            raise SessionAbortError(f"could not stop session {handle.session_id}: {exc}") from exc

    def _run_single(
        self,
        prompt: str,
        cwd: Path,
        *,
        agent: str | None,
        model: str,
        variant: str | None,
        timeout: float,
        on_session: Callable[[SessionHandle], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> RunResult:
        directory = str(cwd)
        session_id = self.client.create_session(directory, title=prompt[:60].replace("\n", " "))
        handle = SessionHandle(session_id, directory, model)
        if on_session is not None:
            on_session(handle)
        log.info(
            "session %s created in %s (model=%s%s)",
            session_id,
            directory,
            model,
            f" variant={variant}" if variant else "",
        )
        self.tap.watch(session_id)
        try:
            message = self._prompt(handle, prompt, agent, variant, timeout, cancelled)
        finally:
            self.tap.forget(session_id)
        error = (message.get("info") or {}).get("error")
        if error is not None:
            self.abort_session(handle)
            data = error.get("data") or {}
            msg = f"{error.get('name', 'UnknownError')}: {data.get('statusCode', '')} "
            msg += error.get("message") or data.get("message", "")
            raise RuntimeError(msg) from None
        text = self._assistant_text([message])
        if not text:
            raise RuntimeError(f"empty assistant response from session {session_id}")
        log.info("session %s completed (%d chars)", session_id, len(text))
        return RunResult(text=text, session_id=session_id)

    def _prompt(
        self,
        handle: SessionHandle,
        prompt: str,
        agent: str | None,
        variant: str | None,
        timeout: float,
        cancelled: threading.Event | None,
    ) -> dict[str, Any]:
        """Keep the blocking completion contract; monitor progress independently."""
        started = time.monotonic()
        stopped = threading.Event()
        failures: list[Exception] = []

        def monitor() -> None:
            heartbeat = started
            while not stopped.wait(self.poll_interval):
                now = time.monotonic()
                last, activity = self.tap.progress(handle.session_id)
                error = self.tap.pop_error(handle.session_id)
                if cancelled is not None and cancelled.is_set():
                    failures.append(RuntimeError("session cancelled"))
                elif error:
                    failures.append(RuntimeError(f"{error['name']}: {error['message']}"))
                elif now - started >= timeout or now - last >= self.idle_timeout:
                    failures.append(
                        TimeoutError(
                            f"session {handle.session_id} model={handle.model} timed out: "
                            f"elapsed={now - started:.1f}s idle={now - last:.1f}s "
                            f"last_activity={activity} "
                            f"event_stream_idle={now - self.tap.last_event:.1f}s"
                        )
                    )
                if failures:
                    log.warning("%s; aborting before recovery", failures[0])
                    try:
                        self.abort_session(handle)
                    except SessionAbortError as exc:
                        failures[:] = [exc]
                    return
                if now - heartbeat >= 60:
                    log.info(
                        "session=%s model=%s elapsed=%.1fs idle=%.1fs last_activity=%s "
                        "event_stream_idle=%.1fs",
                        handle.session_id,
                        handle.model,
                        now - started,
                        now - last,
                        activity,
                        now - self.tap.last_event,
                    )
                    heartbeat = now

        watcher = threading.Thread(target=monitor, name=f"monitor-{handle.session_id}", daemon=True)
        watcher.start()
        request_error: Exception | None = None
        try:
            message = self.client.prompt(
                handle.session_id,
                prompt,
                handle.model or "",
                agent,
                variant,
                handle.directory,
                timeout,
            )
        except Exception as exc:
            request_error = exc
        finally:
            stopped.set()
            watcher.join()
        if failures:
            raise failures[0]
        error = self.tap.pop_error(handle.session_id)
        if error:
            self.abort_session(handle)
            raise RuntimeError(f"{error['name']}: {error['message']}")
        if request_error is not None:
            self.abort_session(handle)
            raise request_error
        return message

    @staticmethod
    def _assistant_text(messages: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for message in messages:
            info = message.get("info") or {}
            role = info.get("role") or message.get("role")
            if role != "assistant":
                continue
            for part in message.get("parts", []):
                if part.get("type") == "text":
                    chunks.append(part.get("text", ""))
        return "\n".join(chunks).strip()
