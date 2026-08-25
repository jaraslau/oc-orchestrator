from pathlib import Path

import pytest

from orchestrator.runtime.client import OpencodeApiError, SessionHandle
from orchestrator.runtime.events import EventTap
from orchestrator.runtime.runner import SessionRunner


class FakeClient:
    def __init__(self, *, default=("opencode", "big-pickle")):
        self.default = default
        self.providers_map = {
            "opencode": ["big-pickle", "backup-model"],
            "anthropic": ["claude-sonnet-4-6"],
        }
        self.sessions: list[tuple[str, str]] = []
        self.prompts: list[dict] = []
        self.status_map: dict[str, str] = {}
        self.messages_map: dict[str, list] = {}
        self.aborted: list[str] = []
        self.fail_prompt_for: dict[str, Exception] = {}

    def default_model(self):
        return self.default

    def providers(self):
        return {k: list(v) for k, v in self.providers_map.items()}

    def create_session(self, directory, title):
        sid = f"ses_{len(self.sessions)}"
        self.sessions.append((sid, directory))
        return sid

    def prompt_async(self, session_id, text, model, agent, variant, directory):
        self.prompts.append(
            {"session": session_id, "model": model, "agent": agent, "variant": variant}
        )
        exc = self.fail_prompt_for.get(session_id)
        if exc is not None:
            raise exc
        self.status_map[session_id] = "busy"

    def status(self):
        return {sid: {"type": state} for sid, state in self.status_map.items()}

    def messages(self, session_id, directory):
        return self.messages_map.get(session_id, [])

    def session_alive(self, session_id):
        return session_id in self.status_map

    def abort(self, session_id, directory):
        self.aborted.append(session_id)

    def complete(self, session_id, text="```handoff\nTASK: X\nSTATUS: DONE\n```\n"):
        assistant = {"role": "assistant", "parts": [{"type": "text", "text": text}]}
        existing = self.messages_map.setdefault(session_id, [])
        existing.append(assistant)
        self.status_map.pop(session_id, None)


class StubTap:
    def __init__(self):
        self.errors: dict[str, dict] = {}
        self.activity: list = []

    def pop_error(self, session_id):
        return self.errors.pop(session_id, None)


def make_runner(client, fallbacks=None, poll=0.01):
    return SessionRunner(client, StubTap(), fallback_models=fallbacks, poll_interval=poll)


def autocomplete(client):
    original_prompt = client.prompt_async

    def prompt_then_complete(session_id, text, model, agent, variant, directory):
        original_prompt(session_id, text, model, agent, variant, directory)
        client.complete(session_id)

    client.prompt_async = prompt_then_complete


class TestResolveChain:
    def test_requested_plus_fallbacks_validated(self):
        client = FakeClient()
        runner = make_runner(client, fallbacks=["opencode/backup-model"])
        chain = runner.resolve_chain(None)
        assert chain.models == ["opencode/big-pickle", "opencode/backup-model"]

    def test_unavailable_fallback_dropped_not_fatal(self):
        client = FakeClient()
        runner = make_runner(client, fallbacks=["ghost/nope", "opencode/backup-model"])
        chain = runner.resolve_chain(None)
        assert chain.models == ["opencode/big-pickle", "opencode/backup-model"]

    def test_explicit_unknown_model_raises(self):
        client = FakeClient()
        runner = make_runner(client)
        with pytest.raises(Exception, match="not offered"):
            runner.resolve_chain("anthropic/nonexistent")


class TestRun:
    def test_happy_path_returns_text_and_model(self):
        client = FakeClient()
        autocomplete(client)
        result = make_runner(client).run("do it", Path("/tmp/wt"))
        assert "STATUS: DONE" in result.text
        assert result.models_tried == ["opencode/big-pickle"]
        assert result.failed_over is False

    def test_agent_and_variant_forwarded(self):
        client = FakeClient()
        autocomplete(client)
        make_runner(client).run(
            "p",
            Path("/w"),
            agent="orchestrator-worker",
            model="opencode/backup-model",
            variant="high",
        )
        assert client.prompts[0]["agent"] == "orchestrator-worker"
        assert client.prompts[0]["variant"] == "high"
        assert client.prompts[0]["model"] == "opencode/backup-model"

    def test_provider_error_via_tap_triggers_failover(self):
        client = FakeClient()
        tap = StubTap()
        runner = SessionRunner(
            client, tap, fallback_models=["opencode/backup-model"], poll_interval=0.01
        )
        original_prompt = client.prompt_async

        def flaky_prompt(session_id, text, model, agent, variant, directory):
            if len(client.sessions) == 1 and not client.messages_map.get(session_id):
                tap.errors[session_id] = {
                    "name": "ProviderAuthError",
                    "message": "401 invalid api key",
                }
                return
            original_prompt(session_id, text, model, agent, variant, directory)
            client.complete(session_id)

        client.prompt_async = flaky_prompt
        result = runner.run("goal", Path("/wt"))
        assert result.failed_over is True
        assert result.models_tried[-1] == "opencode/backup-model"
        assert "ses_0" in client.aborted

    def test_non_provider_error_raises_instantly(self):
        client = FakeClient()
        tap = StubTap()
        runner = SessionRunner(
            client, tap, fallback_models=["opencode/backup-model"], poll_interval=0.01
        )

        def context_overflow(session_id, text, model, agent, variant, directory):
            tap.errors[session_id] = {
                "name": "ContextOverflowError",
                "message": "maximum context length exceeded",
            }

        client.prompt_async = context_overflow
        with pytest.raises(RuntimeError, match="ContextOverflowError"):
            runner.run("goal", Path("/wt"))
        assert len(client.sessions) == 1

    def test_prompt_api_error_classified(self):
        client = FakeClient()
        client.fail_prompt_for["ses_0"] = OpencodeApiError(500, "upstream exploded")
        runner = make_runner(client)
        with pytest.raises(OpencodeApiError):
            runner.run("goal", Path("/wt"))

    def test_timeout_aborts_session(self, monkeypatch):
        client = FakeClient()
        runner = make_runner(client)

        monkeypatch.setattr(client, "session_alive", lambda sid: True)
        with pytest.raises(TimeoutError):
            runner.run("goal", Path("/wt"), timeout=0.01)
        assert client.aborted == ["ses_0"]

    def test_on_session_hook_receives_handle(self):
        client = FakeClient()
        autocomplete(client)
        seen = []
        make_runner(client).run("x", Path("/w"), on_session=seen.append)
        assert isinstance(seen[0], SessionHandle)
        assert seen[0].session_id == "ses_0"


class TestEventTapHandling:
    def test_session_error_recorded(self):
        tap = EventTap(client=None)
        tap._handle(
            {
                "type": "session.error",
                "properties": {"sessionID": "s1", "error": {"name": "APIError", "message": "502"}},
            }
        )
        assert tap.pop_error("s1") == {"name": "APIError", "message": "502"}
        assert tap.pop_error("s1") is None

    def test_part_updates_recorded(self):
        tap = EventTap(client=None)
        tap._handle(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "s2",
                    "part": {"type": "tool", "tool": "bash", "state": {"status": "running"}},
                },
            }
        )
        assert ("s2", "tool") in tap.activity

    def test_other_events_ignored_safely(self):
        tap = EventTap(client=None)
        tap._handle({"type": "storage.write", "properties": {}})
        tap._handle({})
        assert len(tap.activity) == 0
        assert not tap.errors
