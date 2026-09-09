from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest

from orchestrator.runtime.client import OpencodeApiError, OpencodeClient, SessionHandle
from orchestrator.runtime.events import EventTap
from orchestrator.runtime.runner import SessionRunner


class FakeClient(OpencodeClient):
    def __init__(self, *, default: tuple[str, str] = ("opencode", "big-pickle")) -> None:
        self.default: tuple[str, str] = default
        self.providers_map: dict[str, list[str]] = {
            "opencode": ["big-pickle", "backup-model"],
            "anthropic": ["claude-sonnet-4-6"],
        }
        self.sessions: list[tuple[str, str]] = []
        self.prompts: list[dict[str, Any]] = []
        self.aborted: list[str] = []
        self.fail_prompt_for: dict[str, Exception] = {}
        self.base_url: str = "http://fake.test"
        self._timeout: float = 30.0

    def default_model(self) -> tuple[str, str]:
        return self.default

    def providers(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.providers_map.items()}

    def create_session(self, directory: str, title: str) -> str:
        sid = f"ses_{len(self.sessions)}"
        self.sessions.append((sid, directory))
        return sid

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
        self.prompts.append(
            {
                "session": session_id,
                "model": model,
                "agent": agent,
                "variant": variant,
                "timeout": timeout,
            }
        )
        exc = self.fail_prompt_for.get(session_id)
        if exc is not None:
            raise exc
        return {
            "info": {"role": "assistant"},
            "parts": [
                {
                    "type": "text",
                    "text": "```handoff\nTASK: X\nSTATUS: DONE\n```\n",
                }
            ],
        }

    def abort(self, session_id: str, directory: str) -> None:
        self.aborted.append(session_id)


class StubTap(EventTap):
    def __init__(self) -> None:
        super().__init__(cast(OpencodeClient, None))


def make_runner(
    client: OpencodeClient,
    fallbacks: list[str] | None = None,
    poll: float = 0.01,
) -> SessionRunner:
    return SessionRunner(client, StubTap(), fallback_models=fallbacks, poll_interval=poll)


class TestResolveChain:
    def test_requested_plus_fallbacks_validated(self) -> None:
        client = FakeClient()
        runner = make_runner(client, fallbacks=["opencode/backup-model"])
        chain = runner.resolve_chain(None)
        assert chain.models == ["opencode/big-pickle", "opencode/backup-model"]

    def test_unavailable_fallback_dropped_not_fatal(self) -> None:
        client = FakeClient()
        runner = make_runner(client, fallbacks=["ghost/nope", "opencode/backup-model"])
        chain = runner.resolve_chain(None)
        assert chain.models == ["opencode/big-pickle", "opencode/backup-model"]

    def test_explicit_unknown_model_falls_back_to_default(self) -> None:
        client = FakeClient()
        runner = make_runner(client)
        assert runner.resolve_chain("anthropic/nonexistent").models == ["opencode/big-pickle"]

    def test_unqualified_model_is_normalized_to_default_provider(self) -> None:
        client = FakeClient()
        runner = make_runner(client)
        assert runner.resolve_chain("backup-model").models == [
            "opencode/backup-model",
            "opencode/big-pickle",
        ]


class TestRun:
    def test_happy_path_returns_text_and_model(self) -> None:
        client = FakeClient()
        result = make_runner(client).run("do it", Path("/tmp/wt"))
        assert "STATUS: DONE" in result.text
        assert result.models_tried == ["opencode/big-pickle"]

    def test_agent_and_variant_forwarded(self) -> None:
        client = FakeClient()
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

    def test_provider_error_via_tap_triggers_failover(self) -> None:
        tap = StubTap()

        class FlakyClient(FakeClient):
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
                if len(self.sessions) == 1:
                    tap.errors[session_id] = {
                        "name": "ProviderAuthError",
                        "message": "401 invalid api key",
                    }
                return super().prompt(session_id, text, model, agent, variant, directory, timeout)

        client = FlakyClient()
        runner = SessionRunner(
            client, tap, fallback_models=["opencode/backup-model"], poll_interval=0.01
        )
        result = runner.run("goal", Path("/wt"))
        assert result.models_tried[-1] == "opencode/backup-model"
        assert "ses_0" in client.aborted

    def test_non_provider_error_raises_instantly(self) -> None:
        tap = StubTap()

        class OverflowClient(FakeClient):
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
                tap.errors[session_id] = {
                    "name": "ContextOverflowError",
                    "message": "maximum context length exceeded",
                }
                return super().prompt(session_id, text, model, agent, variant, directory, timeout)

        client = OverflowClient()
        runner = SessionRunner(
            client, tap, fallback_models=["opencode/backup-model"], poll_interval=0.01
        )
        with pytest.raises(RuntimeError, match="ContextOverflowError"):
            runner.run("goal", Path("/wt"))
        assert len(client.sessions) == 1

    def test_prompt_api_error_classified(self) -> None:
        client = FakeClient()
        client.fail_prompt_for["ses_0"] = OpencodeApiError(500, "upstream exploded")
        runner = make_runner(client)
        with pytest.raises(OpencodeApiError):
            runner.run("goal", Path("/wt"))

    def test_timeout_aborts_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeClient()
        runner = make_runner(client)
        client.fail_prompt_for["ses_0"] = TimeoutError("request timed out")
        client.fail_prompt_for["ses_1"] = TimeoutError("request timed out")
        with pytest.raises(TimeoutError):
            runner.run("goal", Path("/wt"))
        assert client.aborted == ["ses_0", "ses_1"]

    def test_on_session_hook_receives_handle(self) -> None:
        client = FakeClient()
        seen: list[SessionHandle] = []
        make_runner(client).run("x", Path("/w"), on_session=seen.append)
        assert isinstance(seen[0], SessionHandle)
        assert seen[0].session_id == "ses_0"


class TestEventTapHandling:
    def test_worktree_activity_and_nested_error(self) -> None:
        tap = EventTap(FakeClient())
        tap.watch("worker")
        tap._handle(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "sessionID": "worker",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "running"},
                    },
                },
            }
        )
        last = tap.progress("worker")
        assert last[1] == "tool bash running"
        tap._handle({"type": "server.heartbeat", "properties": {}})
        tap._handle(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "worker",
                    "status": {"type": "busy"},
                },
            }
        )
        assert tap.progress("worker") == last
        tap._handle(
            {
                "type": "session.error",
                "properties": {
                    "sessionID": "worker",
                    "error": {
                        "name": "APIError",
                        "data": {"statusCode": 429, "message": "Too many requests"},
                    },
                },
            }
        )
        assert tap.pop_error("worker") == {"name": "APIError", "message": "429: Too many requests"}

    def test_session_error_recorded(self) -> None:
        tap = EventTap(client=cast(OpencodeClient, FakeClient()))
        tap._handle(
            {
                "type": "session.error",
                "properties": {"sessionID": "s1", "error": {"name": "APIError", "message": "502"}},
            }
        )
        assert tap.pop_error("s1") == {"name": "APIError", "message": "502"}
        assert tap.pop_error("s1") is None

    def test_other_events_ignored_safely(self) -> None:
        tap = EventTap(client=cast(OpencodeClient, FakeClient()))
        tap._handle({"type": "storage.write", "properties": {}})
        tap._handle({})
        assert not tap.errors


@pytest.mark.parametrize("active", [False, True])
def test_stall_aborts_before_failover_but_progress_keeps_session_alive(active: bool) -> None:
    class SlowClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.stopped = threading.Event()

        def prompt(self, session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if session_id == "ses_0":
                if active:
                    for _ in range(12):
                        tap._handle(
                            {
                                "type": "message.part.delta",
                                "properties": {
                                    "sessionID": session_id,
                                    "delta": "x",
                                },
                            }
                        )
                        assert not self.stopped.wait(0.01)
                else:
                    assert self.stopped.wait(2), "watchdog did not abort the stalled session"
            else:
                assert self.aborted == ["ses_0"]
            return super().prompt(session_id, *args, **kwargs)

        def abort(self, session_id: str, directory: str) -> None:
            super().abort(session_id, directory)
            self.stopped.set()

    client = SlowClient()
    tap = EventTap(client)
    runner = SessionRunner(client, tap, ["opencode/backup-model"], 0.005, idle_timeout=0.06)
    result = runner.run("goal", Path("/wt"), timeout=2)
    assert result.models_tried == (
        ["opencode/big-pickle"] if active else ["opencode/big-pickle", "opencode/backup-model"]
    )
    assert not tap.activity


def test_empty_response_retry_is_bounded_and_cancellation_does_not_retry() -> None:
    cancelled = threading.Event()

    class EmptyClient(FakeClient):
        def prompt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"info": {"role": "assistant"}, "parts": []}

    client = EmptyClient()
    with pytest.raises(RuntimeError, match="empty assistant response"):
        make_runner(client).run("goal", Path("/wt"))
    assert len(client.sessions) == 2
    cancelled.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        make_runner(client).run("goal", Path("/wt"), cancelled=cancelled)
    assert len(client.sessions) == 2


def test_abort_failure_prevents_failover() -> None:
    class AbortFailure(FakeClient):
        def abort(self, session_id: str, directory: str) -> None:
            raise OpencodeApiError(500, "server unavailable")

    client = AbortFailure()
    client.fail_prompt_for["ses_0"] = TimeoutError("request timed out")
    with pytest.raises(RuntimeError, match="could not stop session"):
        make_runner(client, ["opencode/backup-model"]).run("goal", Path("/wt"))
    assert len(client.sessions) == 1


def test_response_body_error_triggers_failover_without_sse() -> None:
    class BodyError(FakeClient):
        def prompt(self, session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if session_id == "ses_0":
                return {
                    "info": {
                        "role": "assistant",
                        "error": {
                            "name": "APIError",
                            "data": {"statusCode": 429, "message": "slow down"},
                        },
                    },
                    "parts": [],
                }
            return super().prompt(session_id, *args, **kwargs)

    client = BodyError()
    result = make_runner(client, ["opencode/backup-model"]).run("goal", Path("/wt"))
    assert result.models_tried == ["opencode/big-pickle", "opencode/backup-model"]


@pytest.mark.parametrize("cancel", [False, True])
def test_total_budget_and_live_cancellation_stop_without_new_attempts(cancel: bool) -> None:
    cancelled = threading.Event()
    stopped = threading.Event()

    class BusyClient(FakeClient):
        def prompt(self, session_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if cancel:
                cancelled.set()
            for _ in range(200):
                tap._handle(
                    {
                        "type": "message.part.delta",
                        "properties": {
                            "sessionID": session_id,
                            "delta": "x",
                        },
                    }
                )
                if stopped.wait(0.005):
                    return super().prompt(session_id, *args, **kwargs)
            pytest.fail("monitor did not stop the request")

        def abort(self, session_id: str, directory: str) -> None:
            super().abort(session_id, directory)
            stopped.set()

    client = BusyClient()
    tap = EventTap(client)
    runner = SessionRunner(client, tap, ["opencode/backup-model"], 0.005, idle_timeout=1)
    with pytest.raises(RuntimeError if cancel else TimeoutError):
        runner.run("goal", Path("/wt"), timeout=0.08, cancelled=cancelled)
    assert len(client.sessions) == 1
    assert client.aborted == ["ses_0"]
