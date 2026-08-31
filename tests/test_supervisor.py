from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orchestrator.core.config import Config, ledger_path
from orchestrator.core.ledger import Ledger, TaskStatus
from orchestrator.orchestration.supervisor import (
    PlannedTask,
    PlanningError,
    extract_json_block,
    llm_review,
    parse_plan,
    plan_tasks,
    run_goal,
)
from tests.conftest import HANDOFF_OK, configured


class TestExtractJson:
    def test_fenced_block(self) -> None:
        text: str = 'blah\n```json\n{"tasks": []}\n```\nend'
        assert extract_json_block(text) == {"tasks": []}

    def test_bare_object(self) -> None:
        assert extract_json_block('noise {"a": {"b": 1}} noise') == {"a": {"b": 1}}

    def test_no_json_raises(self) -> None:
        with pytest.raises(PlanningError):
            extract_json_block("no objects here")


class TestParsePlan:
    def test_valid_plan(self) -> None:
        plans: list[PlannedTask] = parse_plan(
            json.dumps(
                {
                    "tasks": [
                        {
                            "title": "A",
                            "objective": "do a",
                            "acceptance_criteria": ["x"],
                            "depends_on": [],
                        },
                        {
                            "title": "B",
                            "depends_on": ["A"],
                            "role": "orchestrator-tester",
                            "model": "g",
                            "effort": "high",
                        },
                    ]
                }
            )
        )
        assert [p.title for p in plans] == ["A", "B"]
        assert plans[1].role == "orchestrator-tester"
        assert plans[1].model == "g"
        assert plans[1].effort == "high"

    def test_effort_omitted_is_none(self) -> None:
        plans: list[PlannedTask] = parse_plan(json.dumps({"tasks": [{"title": "C"}]}))
        assert plans[0].effort is None

    def test_empty_or_titleless_rejected(self) -> None:
        with pytest.raises(PlanningError):
            parse_plan(json.dumps({"tasks": []}))
        with pytest.raises(PlanningError):
            parse_plan(json.dumps({"tasks": [{"objective": "no title"}]}))

    @pytest.mark.parametrize(
        "task",
        [None, {"title": "x", "depends_on": "not-a-list"}],
    )
    def test_malformed_task_rejected(self, task: Any) -> None:
        with pytest.raises(PlanningError):
            parse_plan(json.dumps({"tasks": [task]}))


def test_invalid_review_verdict_fails_closed(tmp_path: Path, monkeypatch: Any) -> None:
    def _junk_llm(*args: Any, **kwargs: Any) -> str:
        return "junk"

    monkeypatch.setattr("orchestrator.orchestration.supervisor.call_llm", _junk_llm)
    task: dict[str, Any] = {
        "title": "x",
        "objective": "",
        "acceptance_criteria": [],
        "_root": str(tmp_path),
    }

    with pytest.raises(RuntimeError, match="no valid"):
        llm_review(task, "", True, "")


class TestPlanRetry:
    def test_retries_once_with_feedback(self, monkeypatch: Any) -> None:
        calls: list[str] = []

        def flaky(root: Path, prompt: str, *, model: str | None = None) -> str:
            calls.append(prompt)
            if len(calls) == 1:
                return "total garbage"
            return '```json\n{"tasks": [{"title": "ok"}]}\n```'

        monkeypatch.setattr("orchestrator.orchestration.supervisor.call_llm", flaky)
        plans: list[PlannedTask] = plan_tasks(Path("/tmp/x"), Config(), "goal")
        assert [p.title for p in plans] == ["ok"]
        assert len(calls) == 2
        assert "not valid plan JSON" in calls[1]

    def test_falls_back_to_one_worker_when_planner_is_unavailable(self, monkeypatch: Any) -> None:
        def failing_llm(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("offline")

        monkeypatch.setattr(
            "orchestrator.orchestration.supervisor.call_llm",
            failing_llm,
        )

        plans: list[PlannedTask] = plan_tasks(
            Path("/tmp/x"), Config(worker_model="provider/worker"), "ship feature"
        )

        assert plans == [
            PlannedTask(
                title="ship feature",
                objective="ship feature",
                acceptance_criteria=["The requested goal is complete and relevant checks pass"],
                model="provider/worker",
                effort="high",
            )
        ]


@pytest.fixture()
def fast_factory(repo: Path) -> Path:
    """Repo wired for run_goal with instant fake workers."""
    configured(repo)
    return repo


def _approve_all() -> Callable[[dict[str, Any], str, bool, str], tuple[str, str]]:
    def _reviewer(task: dict[str, Any], diff: str, gate_ok: bool, gate_out: str) -> tuple[str, str]:
        return ("approve", "")

    return _reviewer


class TestRunGoal:
    def test_happy_path_two_independent_tasks(self, fast_factory: Path) -> None:
        plans: list[PlannedTask] = [
            PlannedTask(title="Add foo", objective="implement foo"),
            PlannedTask(title="Add bar", acceptance_criteria=["bar works"]),
        ]
        rc: int = run_goal(
            fast_factory,
            "do foo and bar",
            max_loops=20,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, "gate ok"),
            reviewer=_approve_all(),
            io=print,
        )
        assert rc == 0
        lg: Ledger = Ledger.load(ledger_path(fast_factory))
        assert all(t.status == TaskStatus.MERGED for t in lg.tasks.values())
        wt_root: Path = fast_factory / ".orchestrator" / "worktrees"
        leftovers: list[Path] = [p for p in wt_root.rglob("*") if p.is_dir() and "task-" in p.name]
        assert not leftovers

    def test_changes_then_approve_correction_cycle(self, fast_factory: Path) -> None:
        plans: list[PlannedTask] = [PlannedTask(title="Reworked feature")]
        verdicts: Any = iter([("changes", "handle empty input"), ("approve", "")])
        seen: list[int] = []

        def reviewer(t: dict[str, Any], d: str, ok: bool, go: str) -> tuple[str, str]:
            seen.append(1)
            result: Any = next(verdicts)
            return result  # type: ignore[no-any-return]

        rc: int = run_goal(
            fast_factory,
            "goal",
            max_loops=25,
            max_corrections=2,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=reviewer,
            io=lambda s: None,
        )
        assert rc == 0
        assert len(seen) == 2
        lg: Ledger = Ledger.load(ledger_path(fast_factory))
        assert all(t.status == TaskStatus.MERGED for t in lg.tasks.values())

    def test_correction_budget_exhaustion_gives_up(self, fast_factory: Path) -> None:
        plans: list[PlannedTask] = [PlannedTask(title="Never good enough")]
        rc: int = run_goal(
            fast_factory,
            "impossible",
            max_loops=25,
            max_corrections=1,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=lambda t, d, ok, go: ("changes", "still bad"),
            io=lambda s: None,
        )
        assert rc == 1
        lg: Ledger = Ledger.load(ledger_path(fast_factory))
        (t,) = lg.tasks.values()
        assert t.status == TaskStatus.BLOCKED
        assert "supervisor gave up" in (t.last_result or "")

    def test_merge_failure_is_not_counted_as_success(  # noqa: E501
        self, fast_factory: Path, monkeypatch: Any
    ) -> None:
        def fail_merge(*args: Any, **kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, "git merge", stderr="conflict")

        monkeypatch.setattr("orchestrator.orchestration.supervisor._merge_branch", fail_merge)
        rc: int = run_goal(
            fast_factory,
            "conflicting change",
            max_loops=10,
            poll_seconds=0.05,
            planner=lambda root, config, goal: [PlannedTask(title="Conflict")],
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=_approve_all(),
            io=lambda s: None,
        )

        assert rc == 1
        task = next(iter(Ledger.load(ledger_path(fast_factory)).tasks.values()))
        assert task.status == TaskStatus.BLOCKED

    def test_dry_run_creates_nothing(self, repo: Path) -> None:
        rc: int = run_goal(
            repo,
            "just looking",
            dry_run=True,
            planner=lambda root, config, goal: [PlannedTask(title="X")],
            io=lambda s: None,
        )
        assert rc == 0
        assert not (repo / ".orchestrator").exists()

    def test_planned_model_flows_to_dispatch_command(self, fast_factory: Path) -> None:
        from orchestrator.orchestration import service

        runner = service.get_dispatcher(fast_factory)._runner
        plans: list[PlannedTask] = [
            PlannedTask(title="Heavy lift", model="anthropic/opus", effort="high")
        ]
        run_goal(
            fast_factory,
            "goal",
            max_loops=15,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=_approve_all(),
            io=lambda s: None,
        )
        assert runner.calls[0]["model"] == "anthropic/opus"
        assert runner.calls[0]["variant"] == "high"

    def test_planned_effort_persists_on_task(self, fast_factory: Path) -> None:
        plans: list[PlannedTask] = [PlannedTask(title="Tricky bug", effort="high")]
        run_goal(
            fast_factory,
            "fix it",
            max_loops=15,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=_approve_all(),
            io=lambda s: None,
        )
        lg: Ledger = Ledger.load(ledger_path(fast_factory))
        (t,) = lg.tasks.values()
        assert t.effort == "high"

    def test_dependencies_created_in_order(self, fast_factory: Path) -> None:
        plans: list[PlannedTask] = [
            PlannedTask(title="First", objective="base"),
            PlannedTask(title="Second", depends_on=["First"]),
        ]
        rc: int = run_goal(
            fast_factory,
            "chained",
            max_loops=20,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=_approve_all(),
            io=lambda s: None,
        )
        assert rc == 0
        lg: Ledger = Ledger.load(ledger_path(fast_factory))
        by_id: dict[str, Any] = {t.id: t for t in lg.tasks.values()}
        child = next(t for t in lg.tasks.values() if t.title == "Second")
        assert by_id[child.dependencies[0]].title == "First"

    def test_reviewer_failure_retries_then_fails_closed(self, fast_factory: Path) -> None:
        calls: list[int] = []

        def broken_reviewer(*args: Any) -> tuple[str, str]:
            calls.append(1)
            raise RuntimeError("review service offline")

        rc: int = run_goal(
            fast_factory,
            "goal",
            max_loops=10,
            max_retries=1,
            poll_seconds=0.01,
            planner=lambda root, config, goal: [PlannedTask(title="Review me")],
            gate_runner=lambda wt, cfg: (True, "ok"),
            reviewer=broken_reviewer,
            io=lambda s: None,
        )

        assert rc == 1
        assert len(calls) == 2
        task = next(iter(Ledger.load(ledger_path(fast_factory)).tasks.values()))
        assert task.status == TaskStatus.BLOCKED
        assert "review failed" in (task.last_result or "")

    def test_failed_gate_cannot_be_overridden_by_reviewer(self, fast_factory: Path) -> None:
        rc: int = run_goal(
            fast_factory,
            "goal",
            max_loops=10,
            max_corrections=0,
            poll_seconds=0.01,
            planner=lambda root, config, goal: [PlannedTask(title="Unsafe")],
            gate_runner=lambda wt, cfg: (False, "tests failed"),
            reviewer=_approve_all(),
            io=lambda s: None,
        )

        assert rc == 1
        task = next(iter(Ledger.load(ledger_path(fast_factory)).tasks.values()))
        assert task.status == TaskStatus.BLOCKED

    def test_loop_exhaustion_cancels_and_marks_worker_blocked(self, fast_factory: Path) -> None:
        hold: threading.Event = threading.Event()
        runner = configured(
            fast_factory,
            lambda prompt, cwd: (hold.wait(timeout=2), HANDOFF_OK)[1],
        )

        rc: int = run_goal(
            fast_factory,
            "goal",
            max_loops=1,
            poll_seconds=0.01,
            planner=lambda root, config, goal: [PlannedTask(title="Slow")],
            reviewer=_approve_all(),
            io=lambda s: None,
        )
        hold.set()

        assert rc == 1
        assert runner.aborted
        task = next(iter(Ledger.load(ledger_path(fast_factory)).tasks.values()))
        assert task.status == TaskStatus.BLOCKED
        assert "loop budget exhausted" in (task.last_result or "")

    def test_worker_limit_also_applies_to_correction_dispatches(self, fast_factory: Path) -> None:
        lock: threading.Lock = threading.Lock()
        active: int = 0
        peak: int = 0

        def tracked_worker(prompt: str, cwd: Path) -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                threading.Event().wait(0.2 if "TASK-002" in prompt else 0.05)
                return HANDOFF_OK
            finally:
                with lock:
                    active -= 1

        configured(fast_factory, tracked_worker)
        reviewed: set[str] = set()

        def review_once(  # noqa: E501
            task: dict[str, Any], diff: str, gate_ok: bool, gate_out: str
        ) -> tuple[str, str]:
            if task["id"] == "TASK-001" and task["id"] not in reviewed:
                reviewed.add(task["id"])
                return "changes", "apply the requested correction"
            return "approve", ""

        rc: int = run_goal(
            fast_factory,
            "goal",
            max_loops=100,
            max_workers=1,
            poll_seconds=0.01,
            planner=lambda root, config, goal: [
                PlannedTask(title="First"),
                PlannedTask(title="Second"),
            ],
            gate_runner=lambda wt, cfg: (True, "ok"),
            reviewer=review_once,
            io=lambda s: None,
        )

        assert rc == 0
        assert peak == 1
