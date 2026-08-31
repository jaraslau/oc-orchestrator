import pytest

from orchestrator.runtime.resilience import (
    PROVIDER_SIDED,
    ErrorKind,
    ModelChain,
    OrchestratorError,
    classify,
    parse_model_ref,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Error 401: Unauthorized", ErrorKind.PROVIDER_AUTH),
        ("invalid api key provided", ErrorKind.PROVIDER_AUTH),
        ("402 payment required, insufficient credits", ErrorKind.PROVIDER_QUOTA),
        ("you exceeded your quota", ErrorKind.PROVIDER_QUOTA),
        ("429 Too Many Requests", ErrorKind.RATE_LIMITED),
        ("rate limit exceeded", ErrorKind.RATE_LIMITED),
        ("ProviderModelNotFoundError: Model not found: x/y", ErrorKind.MODEL_NOT_FOUND),
        ("502 Bad Gateway", ErrorKind.PROVIDER_UNAVAILABLE),
        ("provider is overloaded", ErrorKind.PROVIDER_UNAVAILABLE),
        ("ECONNRESET while streaming", ErrorKind.TRANSIENT_NETWORK),
        ("request timed out after 30s", ErrorKind.TRANSIENT_NETWORK),
        ("prompt is too long: maximum context length exceeded", ErrorKind.CONTEXT_OVERFLOW),
        ("content policy violation", ErrorKind.CONTENT_FILTER),
        ("session was aborted", ErrorKind.ABORTED),
        ("something completely weird", ErrorKind.UNKNOWN),
        ("", ErrorKind.UNKNOWN),
    ],
)
def test_classify(text: str, expected: ErrorKind) -> None:
    assert classify(text) is expected


def test_provider_sided_membership() -> None:
    assert ErrorKind.PROVIDER_AUTH in PROVIDER_SIDED
    assert ErrorKind.RATE_LIMITED in PROVIDER_SIDED
    assert ErrorKind.CONTEXT_OVERFLOW not in PROVIDER_SIDED
    assert ErrorKind.UNKNOWN not in PROVIDER_SIDED


class TestModelChain:
    def test_build_dedupes_and_defaults(self) -> None:
        chain = ModelChain.build(None, ["a", "b"], "dflt")
        assert chain.models == ["dflt", "a", "b"]

    def test_build_primary_first_no_duplicates(self) -> None:
        chain = ModelChain.build("a", ["a", "b"], "d")
        assert chain.models == ["a", "b", "d"]

    def test_advance_walks_chain_then_none(self) -> None:
        chain = ModelChain.build("a", ["b", "c"], "z")
        assert chain.current == "a"
        assert chain.advance("auth") == "b"
        assert chain.advance("quota") == "c"
        assert chain.advance("rate") == "z"
        assert chain.advance("down") is None
        assert chain.exhausted


PROVIDERS: dict[str, list[str]] = {
    "anthropic": ["claude-sonnet-4-6", "claude-haiku-4"],
    "opencode": ["big-pickle"],
}


class TestParseModelRef:
    def test_explicit_provider(self) -> None:
        assert parse_model_ref("anthropic/claude-haiku-4", PROVIDERS, "opencode") == (
            "anthropic",
            "claude-haiku-4",
        )

    def test_bare_uses_default_provider(self) -> None:
        assert parse_model_ref("big-pickle", PROVIDERS, "opencode") == ("opencode", "big-pickle")

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="not offered"):
            parse_model_ref("anthropic/nonexistent", PROVIDERS, "opencode")

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="available: none|available"):
            parse_model_ref("ghost/model", PROVIDERS, "opencode")
