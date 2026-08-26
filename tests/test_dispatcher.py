from orchestrator.runtime.dispatcher import Dispatcher, DispatchRecord, parse_handoff

SAMPLE_HANDOFF = """some log noise
```handoff
TASK: TASK-001
STATUS: DONE
SUMMARY: first line
  continued detail
```
trailing noise"""


class TestParseHandoff:
    def test_parses_last_block(self):
        text = SAMPLE_HANDOFF + "\n```handoff\nSTATUS: SUPERSEDED\nTASK: X\n```"
        result = parse_handoff(text)
        assert result is not None
        assert result["STATUS"] == "SUPERSEDED"

    def test_multiline_value(self):
        result = parse_handoff(SAMPLE_HANDOFF)
        assert result["SUMMARY"] == "first line\ncontinued detail"
        assert result["STATUS"] == "DONE"

    def test_no_block(self):
        assert parse_handoff("no fences here") is None

    def test_empty_block(self):
        assert parse_handoff("```handoff\n```") is None


def _record(tmp_path, **kw) -> DispatchRecord:
    defaults = dict(
        task_id="TASK-001",
        branch="agent/task-001-x",
        worktree=str(tmp_path),
        log_path=str(tmp_path / "task-001.log"),
        started_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(kw)
    return DispatchRecord(**defaults)


class TestReadLog:
    def test_missing_log(self, tmp_path):
        assert Dispatcher.read_log(_record(tmp_path)) == ""

    def test_reads_content(self, tmp_path):
        (tmp_path / "task-001.log").write_text("hello world")
        assert "hello" in Dispatcher.read_log(_record(tmp_path))
