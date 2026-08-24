import pytest

from orchestrator.core.ledger import Ledger, Task, TaskStatus


@pytest.fixture()
def ledger_path(tmp_path):
    return tmp_path / "ledger.json"


class TestCreateTask:
    def test_assigns_sequential_ids(self, ledger_path):
        ledger = Ledger(ledger_path)
        t1 = ledger.create_task("First")
        t2 = ledger.create_task("Second")
        assert (t1.id, t2.id) == ("TASK-001", "TASK-002")

    def test_defaults(self, ledger_path):
        task = Ledger(ledger_path).create_task("Do a thing")
        assert task.status is TaskStatus.PLANNED
        assert task.acceptance_criteria == []
        assert task.dependencies == []
        assert task.branch is None

    def test_unknown_dependency_rejected(self, ledger_path):
        ledger = Ledger(ledger_path)
        with pytest.raises(ValueError, match="TASK-009"):
            ledger.create_task("Blocked", dependencies=["TASK-009"])

    def test_known_dependency_accepted(self, ledger_path):
        ledger = Ledger(ledger_path)
        first = ledger.create_task("First")
        second = ledger.create_task("Second", dependencies=[first.id])
        assert second.dependencies == ["TASK-001"]


class TestPersistence:
    def test_round_trip(self, ledger_path):
        ledger = Ledger(ledger_path)
        created = ledger.create_task(
            "Implement thing",
            objective="make it work",
            acceptance_criteria=["tests pass"],
            agent="agent-a",
            branch="agent/task-001-thing",
            risks=["none known"],
        )
        ledger.update_status(created.id, TaskStatus.DISPATCHED)
        ledger.save()

        loaded = Ledger.load(ledger_path)
        task = loaded.get("TASK-001")
        assert task.title == "Implement thing"
        assert task.status is TaskStatus.DISPATCHED
        assert task.acceptance_criteria == ["tests pass"]
        assert task.branch == "agent/task-001-thing"

    def test_load_missing_file_returns_empty(self, ledger_path):
        ledger = Ledger.load(ledger_path)
        assert ledger.filter() == []

    def test_load_rejects_bad_status(self, ledger_path):
        ledger_path.write_text('{"tasks": [{"id": "TASK-001", "title": "x", "status": "NOPE"}]}')
        with pytest.raises(ValueError, match="NOPE"):
            Ledger.load(ledger_path)


class TestQueries:
    def test_get_missing_raises_keyerror_with_id(self, ledger_path):
        with pytest.raises(KeyError, match="TASK-404"):
            Ledger(ledger_path).get("TASK-404")

    def test_filter_by_status_string(self, ledger_path):
        ledger = Ledger(ledger_path)
        a = ledger.create_task("A")
        b = ledger.create_task("B")
        ledger.update_status(b.id, "FAILED")
        ids = [t.id for t in ledger.filter(status=TaskStatus.PLANNED)]
        assert ids == [a.id]

    def test_ids_survive_three_digits(self, ledger_path):
        ledger = Ledger(ledger_path)
        ledger.tasks = {
            f"TASK-{i:03d}": Task(id=f"TASK-{i:03d}", title="stub") for i in range(1, 1000)
        }
        assert ledger.next_task_id() == "TASK-1000"


class TestUpdateStatus:
    def test_accepts_plain_string(self, ledger_path):
        ledger = Ledger(ledger_path)
        task = ledger.create_task("A")
        updated = ledger.update_status(task.id, "WORKING")
        assert updated.status is TaskStatus.WORKING

    def test_rejects_unknown_status(self, ledger_path):
        ledger = Ledger(ledger_path)
        task = ledger.create_task("A")
        with pytest.raises(ValueError, match="unknown task status"):
            ledger.update_status(task.id, "WAT")

    def test_updates_timestamp(self, ledger_path):
        ledger = Ledger(ledger_path)
        task = ledger.create_task("A")
        before = task.updated_at
        updated = ledger.update_status(task.id, TaskStatus.MERGED)
        assert updated.updated_at >= before
