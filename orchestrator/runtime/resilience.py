"""Error taxonomy, classification and model failover.

Provider-side failures are classified from HTTP status codes, server error
names and stderr text. Provider-sided errors trigger failover to the next
model in the chain; everything else fails fast with full context.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from orchestrator.logs import get

log = get("resilience")


class ErrorKind(StrEnum):
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_QUOTA = "provider_quota"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MODEL_NOT_FOUND = "model_not_found"
    TRANSIENT_NETWORK = "transient_network"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTENT_FILTER = "content_filter"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


PROVIDER_SIDED: frozenset[ErrorKind] = frozenset(
    {
        ErrorKind.PROVIDER_AUTH,
        ErrorKind.PROVIDER_QUOTA,
        ErrorKind.RATE_LIMITED,
        ErrorKind.PROVIDER_UNAVAILABLE,
        ErrorKind.MODEL_NOT_FOUND,
    }
)

_PATTERNS: list[tuple[ErrorKind, re.Pattern[str]]] = [
    (kind, re.compile(pat, re.IGNORECASE))
    for kind, pat in (
        (ErrorKind.PROVIDER_AUTH, r"\b401\b|\b403\b"),
        (
            ErrorKind.PROVIDER_AUTH,
            r"unauthori[sz]ed|invalid api key|invalid_api_key|authentication",
        ),
        (ErrorKind.PROVIDER_QUOTA, r"\b402\b"),
        (ErrorKind.PROVIDER_QUOTA, r"quota|credit|billing|insufficient|exceeded your"),
        (ErrorKind.RATE_LIMITED, r"\b429\b"),
        (ErrorKind.RATE_LIMITED, r"rate.?limit|too many requests"),
        (
            ErrorKind.MODEL_NOT_FOUND,
            r"model not found|provider.*not found|does not exist|unsupported model",
        ),
        (
            ErrorKind.PROVIDER_UNAVAILABLE,
            r"5\d\d|bad gateway|service unavailable|overloaded|capacity|provider.*(down|error)",
        ),
        (
            ErrorKind.TRANSIENT_NETWORK,
            r"econn.?reset|econn.?refused|connection (reset|refused|error)|"
            r"timed? ?out|temporary failure|getaddrinfo",
        ),
        (
            ErrorKind.CONTEXT_OVERFLOW,
            r"context (length|window)|too long|maximum context|token limit",
        ),
        (ErrorKind.CONTENT_FILTER, r"content (filter|policy)|safety"),
        (ErrorKind.ABORTED, r"abort|cancel"),
    )
]


def classify(text: str) -> ErrorKind:
    target = text or ""
    for kind, pattern in _PATTERNS:
        if pattern.search(target):
            return kind
    return ErrorKind.UNKNOWN


class OrchestratorError(RuntimeError):
    pass


class ProviderExhaustedError(OrchestratorError):
    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        detail = "; ".join(f"{m}: {e}" for m, e in attempts)
        super().__init__(f"all models failed -> {detail}")


@dataclass
class ModelChain:
    models: list[str]
    _index: int = 0

    @classmethod
    def build(cls, primary: str | None, fallbacks: list[str], default: str) -> ModelChain:
        seen: list[str] = []
        for model in [primary, *fallbacks]:
            candidate = model or default
            if candidate not in seen:
                seen.append(candidate)
        return cls(models=seen)

    @property
    def current(self) -> str:
        return self.models[self._index]

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self.models)

    def advance(self, reason: str) -> str | None:
        failed = self.current
        log.warning("model %s failed (%s); failing over", failed, reason)
        self._index += 1
        if self.exhausted:
            return None
        nxt = self.current
        log.info("switching model: %s -> %s", failed, nxt)
        return nxt


T = TypeVar("T")


@dataclass
class AttemptOutcome:
    value: T | None = None
    error: Exception | None = None


def run_with_failover[T](
    chain: ModelChain,
    attempt: Callable[[str], T],
    classify_error: Callable[[Exception], ErrorKind] | None = None,
) -> T:
    classifier = classify_error or (lambda err: classify(str(err)))
    attempts: list[tuple[str, str]] = []
    while not chain.exhausted:
        model = chain.current
        try:
            result = attempt(model)
            if attempts:
                log.info("succeeded on failover model %s after %d failure(s)", model, len(attempts))
            return result
        except Exception as exc:
            kind = classifier(exc)
            attempts.append((model, f"[{kind.value}] {exc}"))
            log.error("attempt on %s raised %s: %s", model, kind.value, exc)
            if kind not in PROVIDER_SIDED:
                raise
            if chain.advance(kind.value) is None:
                break
    raise ProviderExhaustedError(attempts)


def parse_model_ref(
    ref: str, providers: dict[str, list[str]], default_provider: str
) -> tuple[str, str]:
    if "/" in ref:
        provider_id, model_id = ref.split("/", 1)
    else:
        provider_id, model_id = default_provider, ref
    available = providers.get(provider_id, [])
    if model_id not in available:
        raise OrchestratorError(
            f"model '{ref}' not offered by provider '{provider_id}' "
            f"(available: {', '.join(sorted(available)) or 'none'})"
        )
    return provider_id, model_id
