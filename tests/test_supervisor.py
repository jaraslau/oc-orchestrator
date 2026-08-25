import json

import pytest

from orchestrator.core.config import Config, ledger_path
from orchestrator.core.ledger import Ledger, TaskStatus
from orchestrator.orchestration.supervisor import (
    PlannedTask,
    PlanningError,
    extract_json_block,
    parse_plan,
    plan_tasks,
    run_goal,
)
from tests.conftest import HANDOFF_OK, configured


class TestExtractJson:
    def test_fenced_block(self):
        text = 'blah\n```json\n{"tasks": []}\n```\nend'
        assert extract_json_block(text) == {"tasks": []}

    def test_bare_object(self):
        assert extract_json_block('noise {"a": {"b": 1}} noise') == {"a": {"b": 1}}

    def test_no_json_raises(self):
        with pytest.raises(PlanningError):
            extract_json_block("no objects here")


class TestParsePlan:
    def test_valid_plan(self):
        plans = parse_plan(
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

    def test_effort_omitted_is_none(self):
        plans = parse_plan(json.dumps({"tasks": [{"title": "C"}]}))
        assert plans[0].effort is None

    def test_empty_or_titleless_rejected(self):
        with pytest.raises(PlanningError):
            parse_plan(json.dumps({"tasks": []}))
        with pytest.raises(PlanningError):
            parse_plan(json.dumps({"tasks": [{"objective": "no title"}]}))


class TestPlanRetry:
    def test_retries_once_with_feedback(self):
        calls = []

        def flaky(root, config, prompt, model=None):
            calls.append(prompt)
            if len(calls) == 1:
                return "total garbage"
            return '```json\n{"tasks": [{"title": "ok"}]}\n```'

        plans = plan_tasks("/tmp/x", Config(), "goal", runner=flaky)
        assert [p.title for p in plans] == ["ok"]
        assert len(calls) == 2
        assert "not valid plan JSON" in calls[1]


@pytest.fixture()
def fast_factory(repo, fake_worker):
    """Repo wired for run_goal with instant fake workers."""
    configured(repo, fake_worker(HANDOFF_OK))
    return repo


def _approve_all():
    return lambda task, diff, gate_ok, gate_out: ("approve", "")


class TestRunGoal:
    def test_happy_path_two_independent_tasks(self, fast_factory):
        plans = [
            PlannedTask(title="Add foo", objective="implement foo"),
            PlannedTask(title="Add bar", acceptance_criteria=["bar works"]),
        ]
        rc = run_goal(
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
        lg = Ledger.load(ledger_path(fast_factory))
        assert all(t.status == TaskStatus.MERGED for t in lg.tasks.values())
        wt_root = fast_factory / ".orchestrator" / "worktrees"
        leftovers = [p for p in wt_root.rglob("*") if p.is_dir() and "task-" in p.name]
        assert not leftovers

    def test_changes_then_approve_correction_cycle(self, fast_factory):
        plans = [PlannedTask(title="Reworked feature")]
        verdicts = iter([("changes", "handle empty input"), ("approve", "")])
        seen = []
        rc = run_goal(
            fast_factory,
            "goal",
            max_loops=25,
            max_corrections=2,
            poll_seconds=0.05,
            planner=lambda root, config, goal: plans,
            gate_runner=lambda wt, cfg: (True, ""),
            reviewer=lambda t, d, ok, go: (seen.append(1), next(verdicts))[1],
            io=lambda s: None,
        )
        assert rc == 0
        assert len(seen) == 2
        lg = Ledger.load(ledger_path(fast_factory))
        assert all(t.status == TaskStatus.MERGED for t in lg.tasks.values())

    def test_correction_budget_exhaustion_gives_up(self, fast_factory):
        plans = [PlannedTask(title="Never good enough")]
        rc = run_goal(
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
        lg = Ledger.load(ledger_path(fast_factory))
        (t,) = lg.tasks.values()
        assert t.status == TaskStatus.BLOCKED
        assert "supervisor gave up" in (t.last_result or "")

    def test_dry_run_creates_nothing(self, repo):
        rc = run_goal(
            repo,
            "just looking",
            dry_run=True,
            planner=lambda root, config, goal: [PlannedTask(title="X")],
            io=lambda s: None,
        )
        assert rc == 0
        assert not (repo / ".orchestrator").exists()

    def test_planned_model_flows_to_dispatch_command(self, fast_factory):
        from unittest.mock import patch

        import orchestrator.runtime.dispatcher as dispatcher_mod

        recorded = {}
        original = dispatcher_mod.Dispatcher.__dict__["build_command"]

        def spy(config, worktree, prompt, model=None, agent_name=None, variant=None):
            recorded["model"] = model
            recorded["variant"] = variant
            return original.__func__(config, worktree, prompt, model, agent_name, variant)

        with patch.object(dispatcher_mod.Dispatcher, "build_command", staticmethod(spy)):
            plans = [PlannedTask(title="Heavy lift", model="anthropic/opus", effort="high")]
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
        assert recorded["model"] == "anthropic/opus"
        assert recorded["variant"] == "high"

    def test_planned_effort_persists_on_task(self, fast_factory):
        plans = [PlannedTask(title="Tricky bug", effort="high")]
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
        lg = Ledger.load(ledger_path(fast_factory))
        (t,) = lg.tasks.values()
        assert t.effort == "high"

    def test_dependencies_created_in_order(self, fast_factory):
        plans = [
            PlannedTask(title="First", objective="base"),
            PlannedTask(title="Second", depends_on=["First"]),
        ]
        rc = run_goal(
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
        lg = Ledger.load(ledger_path(fast_factory))
        by_id = {t.id: t for t in lg.tasks.values()}
        child = next(t for t in lg.tasks.values() if t.title == "Second")
        assert by_id[child.dependencies[0]].title == "First"
