from orchestrator.runtime.client import OpencodeClient


class _EventStream:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def iter_lines(self):
        yield 'data: {"type":"session.idle","properties":{"sessionID":"ses_1"}}'
        yield ""


def test_events_yields_complete_sse_frame(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.runtime.client.httpx.stream", lambda *args, **kwargs: _EventStream()
    )

    events = list(OpencodeClient("http://localhost:4096").events())

    assert events == [{"type": "session.idle", "properties": {"sessionID": "ses_1"}}]
