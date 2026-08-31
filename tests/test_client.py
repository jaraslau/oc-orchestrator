from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from orchestrator.runtime.client import OpencodeApiError, OpencodeClient


class _EventStream:
    status_code = 200

    def __enter__(self) -> "_EventStream":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_lines(self) -> Iterator[str]:
        yield 'data: {"type":"session.idle","properties":{"sessionID":"ses_1"}}'
        yield ""


def test_events_yields_complete_sse_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orchestrator.runtime.client.httpx.stream", lambda *args, **kwargs: _EventStream()
    )

    events = list(OpencodeClient("http://localhost:4096").events())

    assert events == [{"type": "session.idle", "properties": {"sessionID": "ses_1"}}]


def test_non_json_api_response_is_an_explicit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        status_code=200,
        content=b"not json",
        text="not json",
        json=lambda: (_ for _ in ()).throw(ValueError("bad json")),
    )
    monkeypatch.setattr("orchestrator.runtime.client.httpx.request", lambda *a, **kw: response)

    with pytest.raises(OpencodeApiError, match="invalid JSON response"):
        OpencodeClient("http://localhost:4096").providers()
