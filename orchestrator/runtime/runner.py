"""Server-backed session execution with model failover.

Runs a prompt against the shared opencode server: preflights the model chain,
creates a session, prompts asynchronously, then polls status while the event
tap captures errors. Provider-sided failures trigger failover to the next
model in the chain; anything else fails fast with full diagnosis.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.logs import get
from orchestrator.runtime.client import OpencodeApiError, OpencodeClient, SessionHandle
from orchestrator.runtime.events import EventTap
from orchestrator.runtime.resilience import (
    PROVIDER_SIDED,
    ErrorKind,
    ModelChain,
    OrchestratorError,
    ProviderExhaustedError,
    classify,
    parse_model_ref,
)

log = get("runner")

DEFAULT_POLL_INTERVAL = 0.5


@dataclass
class RunResult:
    text: str
    session_id: str
    models_tried: list[str] = field(default_factory=list)
    failed_over: bool = False


class SessionRunner:
    def __init__(
        self,
        client: OpencodeClient,
        tap: EventTap,
        fallback_models: list[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.client = client
        self.tap = tap
        self.fallbacks = list(fallback_models or [])
        self.poll_interval = poll_interval
        self._default_model: str | None = None

    def default_model(self) -> str:
        if self._default_model is None:
            provider_id, model_id = self.client.default_model()
            self._default_model = f"{provider_id}/{model_id}"
        return self._default_model

    def resolve_chain(self, requested: str | None) -> ModelChain:
        providers = self.client.providers()
        default_provider, default_model_id = self.default_model().split("/", 1)
        providers.setdefault(default_provider, [])
        if default_model_id not in providers[default_provider]:
            providers[default_provider].append(default_model_id)
        if requested is not None:
            parse_model_ref(requested, providers, default_provider)
        chain = ModelChain.build(requested, self.fallbacks, self.default_model())
        validated: list[str] = []
        for ref in chain.models:
            try:
                parse_model_ref(ref, providers, default_provider)
                validated.append(ref)
            except OrchestratorError:
                log.error("chain entry '%s' unavailable on server; dropping", ref)
        chain.models = validated
        if not chain.models:
            raise OrchestratorError("no usable models in chain after validation")
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
    ) -> RunResult:
        chain = self.resolve_chain(model)
        attempts: list[tuple[str, str]] = []
        while not chain.exhausted:
            current = chain.current
            try:
                result = self._run_single(
                    prompt,
                    cwd,
                    agent=agent,
                    model=current,
                    variant=variant,
                    timeout=timeout,
                    on_session=on_session,
                )
                result.models_tried = [m for m, _ in attempts] + [current]
                result.failed_over = bool(attempts)
                if attempts:
                    log.info(
                        "succeeded on failover model %s after %d failure(s)", current, len(attempts)
                    )
                return result
            except Exception as exc:
                kind = classify(str(exc))
                attempts.append((current, f"[{kind.value}] {exc}"))
                log.error("model %s failed (%s): %s", current, kind.value, exc)
                if kind not in PROVIDER_SIDED or chain.advance(kind.value) is None:
                    raise
        raise ProviderExhaustedError(attempts)

    def abort_session(self, handle: SessionHandle) -> None:
        try:
            self.client.abort(handle.session_id, handle.directory)
        except OpencodeApiError as exc:
            log.warning("abort failed for %s: %s", handle.session_id, exc)

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
        started = time.monotonic()
        baseline = self._message_count(session_id, directory)
        try:
            self.client.prompt_async(session_id, prompt, model, agent, variant, directory)
        except OpencodeApiError:
            self.abort_session(handle)
            raise
        deadline = started + timeout
        session_gone = False
        while True:
            error = self.tap.pop_error(session_id)
            if error is not None:
                self.abort_session(handle)
                msg = f"{error.get('name', 'UnknownError')}: {error.get('message', '')}"
                raise RuntimeError(msg) from None
            if not session_gone and not self.client.session_alive(session_id):
                session_gone = True
            if session_gone:
                messages = self.client.messages(session_id, directory)
                if len(messages) > baseline:
                    text = self._assistant_text(messages)
                    if text:
                        elapsed = time.monotonic() - started
                        log.info(
                            "session %s completed in %.1fs (%d chars)",
                            session_id,
                            elapsed,
                            len(text),
                        )
                        return RunResult(text=text, session_id=session_id)
            if time.monotonic() >= deadline:
                self.abort_session(handle)
                raise TimeoutError(f"session {session_id} exceeded {timeout}s") from None
            time.sleep(self.poll_interval)

    def _message_count(self, session_id: str, directory: str) -> int:
        try:
            return len(self.client.messages(session_id, directory))
        except OpencodeApiError:
            return 0

    @staticmethod
    def _assistant_text(messages: list[dict]) -> str:
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


def classify_kind(text: str) -> ErrorKind:
    return classify(text)


__all__ = ["RunResult", "SessionRunner"]
