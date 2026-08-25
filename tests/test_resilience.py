import pytest

from orchestrator.runtime.resilience import (
    PROVIDER_SIDED,
    ErrorKind,
    ModelChain,
    OrchestratorError,
    ProviderExhaustedError,
    classify,
    parse_model_ref,
    run_with_failover,
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
def test_classify(text, expected):
    assert classify(text) is expected


def test_provider_sided_membership():
    assert ErrorKind.PROVIDER_AUTH in PROVIDER_SIDED
    assert ErrorKind.RATE_LIMITED in PROVIDER_SIDED
    assert ErrorKind.CONTEXT_OVERFLOW not in PROVIDER_SIDED
    assert ErrorKind.UNKNOWN not in PROVIDER_SIDED


class TestModelChain:
    def test_build_dedupes_and_defaults(self):
        chain = ModelChain.build(None, ["a", "b"], "dflt")
        assert chain.models == ["dflt", "a", "b"]

    def test_build_primary_first_no_duplicates(self):
        chain = ModelChain.build("a", ["a", "b"], "d")
        assert chain.models == ["a", "b"]

    def test_advance_walks_chain_then_none(self):
        chain = ModelChain.build("a", ["b", "c"], "z")
        assert chain.current == "a"
        assert chain.advance("auth") == "b"
        assert chain.advance("quota") == "c"
        assert chain.advance("rate") is None
        assert chain.exhausted


class TestRunWithFailover:
    def test_success_on_first_model(self):
        chain = ModelChain.build("a", ["b"], "c")
        calls = []

        def attempt(model):
            calls.append(model)
            return f"ok-{model}"

        assert run_with_failover(chain, attempt) == "ok-a"
        assert calls == ["a"]

    def test_failover_to_backup(self):
        chain = ModelChain.build("a", ["b"], "c")

        def attempt(model):
            if model == "a":
                raise RuntimeError("429 rate limit exceeded")
            return "ok"

        assert run_with_failover(chain, attempt) == "ok"

    def test_non_provider_error_raises_immediately(self):
        chain = ModelChain.build("a", ["b"], "c")
        calls = []

        def attempt(model):
            calls.append(model)
            raise ValueError("bug in our code")

        with pytest.raises(ValueError):
            run_with_failover(chain, attempt)
        assert calls == ["a"]

    def test_exhaustion_raises_with_diagnosis(self):
        chain = ModelChain.build("a", ["b"], "c")

        def attempt(model):
            if model == "a":
                raise RuntimeError("401 unauthorized")
            raise RuntimeError("502 bad gateway")

        with pytest.raises(ProviderExhaustedError) as excinfo:
            run_with_failover(chain, attempt)
        models = [m for m, _ in excinfo.value.attempts]
        assert models == ["a", "b"]
        assert "[provider_auth]" in excinfo.value.attempts[0][1]
        assert "[provider_unavailable]" in excinfo.value.attempts[1][1]

    def test_custom_classifier(self):
        class Weird(Exception):
            pass

        chain = ModelChain.build("a", ["b"], "c")

        def classifier(err):
            return ErrorKind.PROVIDER_UNAVAILABLE

        def attempt(model):
            raise Weird()

        with pytest.raises(ProviderExhaustedError):
            run_with_failover(chain, attempt, classifier)


PROVIDERS = {"anthropic": ["claude-sonnet-4-6", "claude-haiku-4"], "opencode": ["big-pickle"]}


class TestParseModelRef:
    def test_explicit_provider(self):
        assert parse_model_ref("anthropic/claude-haiku-4", PROVIDERS, "opencode") == (
            "anthropic",
            "claude-haiku-4",
        )

    def test_bare_uses_default_provider(self):
        assert parse_model_ref("big-pickle", PROVIDERS, "opencode") == ("opencode", "big-pickle")

    def test_unknown_model_raises(self):
        with pytest.raises(OrchestratorError, match="not offered"):
            parse_model_ref("anthropic/nonexistent", PROVIDERS, "opencode")

    def test_unknown_provider_raises(self):
        with pytest.raises(OrchestratorError, match="available: none|available"):
            parse_model_ref("ghost/model", PROVIDERS, "opencode")
