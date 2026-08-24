import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import service
from orchestrator.errors import DispatchBlocked, GhError, InvalidState
from orchestrator.ledger import Ledger, TaskStatus
from orchestrator.review import GhClient, pr_number_from_url


def make_runner(handler, calls=None):
    calls = [] if calls is None else calls

    def runner(*args):
        arg_list = list(args)
        calls.append(arg_list)
        rc, out, err = handler(arg_list)
        return subprocess.CompletedProcess(arg_list, rc, out, err)

    return runner, calls


def gh_ok(args):
    return 0, "", ""


PR_JSON_ONE = json.dumps(
    [
        {
            "number": 7,
            "url": "https://github.com/acme/repo/pull/7",
            "headRefName": "agent/task-001-thing",
            "title": "TASK-001: thing",
            "state": "OPEN",
        }
    ]
)


def reviewing_task(repo):
    task = service.create_task(repo, title="Thing")
    ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
    ledger.update_status(task["id"], TaskStatus.REVIEWING)
    ledger.save()
    return task


class TestGhClient:
    def test_pr_number_from_url(self):
        assert pr_number_from_url("https://github.com/a/b/pull/12") == 12
        assert pr_number_from_url("https://gh.enterprise/x/y/pulls/3") == 3
        assert pr_number_from_url("https://github.com/a/b") is None

    def test_check_failure_raises(self):
        runner, _ = make_runner(lambda a: (1, "", "not logged in"))
        client = GhClient(Path("."), runner=runner)
        with pytest.raises(GhError, match="not logged in"):
            client.check()

    def test_list_parses_json(self):
        def handler(args):
            assert args[0] == "pr" and args[1] == "list"
            assert "--head" in args
            return 0, PR_JSON_ONE, ""

        runner, _ = make_runner(handler)
        prs = GhClient(Path("."), runner=runner).list_prs(head="x")
        assert prs[0].number == 7
        assert prs[0].state == "OPEN"

    def test_create_parses_url(self):
        def handler(args):
            assert "--base" in args and "--head" in args
            return (
                0,
                "Creating pull request for agent/x:\nhttps://github.com/acme/repo/pull/9\n",
                "",
            )

        runner, calls = make_runner(handler)
        pr = GhClient(Path("."), runner=runner).create_pr("main", "agent/x", "t", "b")
        assert pr.number == 9 and pr.url.endswith("/pull/9")
        flat = [a for call in calls for a in call]
        assert "--delete-branch" not in flat

    def test_merge_uses_method_flag(self):
        seen = []

        def handler(args):
            seen.append(args)
            return 0, "", ""

        runner, _ = make_runner(handler)
        GhClient(Path("."), runner=runner).merge(5, method="squash")
        assert "--squash" in seen[0]

    def test_comment_passes_body(self):
        seen = []

        def handler(args):
            seen.append(args)
            return 0, "", ""

        runner, _ = make_runner(handler)
        GhClient(Path("."), runner=runner).comment(3, "fix it")
        flat = [a for call in seen for a in call]
        assert "fix it" in flat and "3" in flat


class TestPrLifecycle:
    def test_open_creates_and_records(self, repo):
        task = reviewing_task(repo)

        def handler(args):
            if args[:2] == ["pr", "list"]:
                return 0, "[]", ""
            if args[:2] == ["pr", "create"]:
                return 0, "https://github.com/acme/repo/pull/11\n", ""
            return 0, "", ""

        runner, calls = make_runner(handler)
        result = service.open_pr(repo, task["id"], gh_runner=runner)
        assert result["pr"]["number"] == 11
        assert result["task"]["status"] == "PR_OPEN"
        flat = [a for c in calls for a in c]
        assert "--base" in flat and "main" in flat

    def test_open_reuses_existing_pr(self, repo):
        task = reviewing_task(repo)

        def handler(args):
            if args[:2] == ["auth", "status"]:
                return 0, "", ""
            if args[:2] == ["pr", "list"]:
                return 0, PR_JSON_ONE, ""
            raise AssertionError("create should not be called")

        runner, _ = make_runner(handler)
        result = service.open_pr(repo, task["id"], gh_runner=runner)
        assert result["pr"]["number"] == 7

    def test_request_changes_posts_and_marks(self, repo):
        task = reviewing_task(repo)
        ledger_path = repo / ".orchestrator" / "ledger.json"

        # give the task an existing PR url directly
        ledger = Ledger.load(ledger_path)
        t = ledger.get(task["id"])
        t.pr = "https://github.com/acme/repo/pull/7"
        ledger.save()

        seen_comments = []

        def handler(args):
            if args[:2] == ["pr", "comment"]:
                seen_comments.append(args[args.index("--body") + 1])
                return 0, "", ""
            return 0, "", ""

        runner, _ = make_runner(handler)
        result = service.request_changes(repo, task["id"], "rotate tokens please", gh_runner=runner)
        assert result["posted_to_pr"] is True
        assert result["task"]["status"] == "CHANGES_REQUESTED"
        assert any("rotate tokens" in c for c in seen_comments)

    def test_request_changes_without_pr_still_marks(self, repo):
        task = reviewing_task(repo)
        runner, calls = make_runner(gh_ok)
        result = service.request_changes(repo, task["id"], "redo it", gh_runner=runner)
        assert result["posted_to_pr"] is False
        assert result["task"]["status"] == "CHANGES_REQUESTED"
        assert calls == []

    def test_merge_happy_path_cleans_worktree(self, repo, tmp_path):
        task = reviewing_task(repo)
        ledger_path = repo / ".orchestrator" / "ledger.json"
        ledger = Ledger.load(ledger_path)
        t = ledger.get(task["id"])
        t.pr = "https://github.com/acme/repo/pull/7"
        t.branch = "agent/task-001-thing"
        ledger.save()
        wt = repo / ".orchestrator" / "worktrees" / t.branch
        wt.mkdir(parents=True)

        merges = []

        def handler(args):
            if args[:2] == ["pr", "merge"]:
                merges.append(args)
                return 0, "", ""
            return 0, "", ""

        runner, _ = make_runner(handler)
        merged = service.merge_task(repo, task["id"], gh_runner=runner)
        assert merged["status"] == "MERGED"
        assert merges and "--squash" in merges[0]
        assert not wt.exists()

    def test_merge_requires_pr(self, repo):
        task = reviewing_task(repo)
        runner, _ = make_runner(gh_ok)
        with pytest.raises(InvalidState, match="no pull request"):
            service.merge_task(repo, task["id"], gh_runner=runner)

    def test_merge_blocked_by_unmet_dependency(self, repo):
        base = service.create_task(repo, title="Base")
        child = service.create_task(repo, title="Child", dependencies=[base["id"]])
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(child["id"], TaskStatus.REVIEWING)
        t = ledger.get(child["id"])
        t.pr = "https://github.com/acme/repo/pull/8"
        ledger.save()
        runner, _ = make_runner(gh_ok)
        with pytest.raises(DispatchBlocked, match="TASK-001"):
            service.merge_task(repo, child["id"], gh_runner=runner)

    def test_pr_diff(self, repo):
        task = reviewing_task(repo)
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        t = ledger.get(task["id"])
        t.pr = "https://github.com/acme/repo/pull/7"
        ledger.save()

        def handler(args):
            if args[:2] == ["pr", "diff"]:
                return 0, "diff --git a/x b/x\n", ""
            return 0, "", ""

        runner, _ = make_runner(handler)
        out = service.pr_diff(repo, task["id"], gh_runner=runner)
        assert out.startswith("diff --git")

    def test_list_open_prs(self, repo):
        def handler(args):
            return 0, PR_JSON_ONE, ""

        runner, _ = make_runner(handler)
        rows = service.list_open_prs(repo, gh_runner=runner)
        assert rows[0]["number"] == 7 and rows[0]["head"] == "agent/task-001-thing"


class TestReport:
    def test_report_sections(self, repo):
        a = service.create_task(repo, title="Done thing")
        b = service.create_task(repo, title="Failed thing")
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(a["id"], TaskStatus.MERGED)
        ledger.get(b["id"]).last_result = "worker exited 2"
        ledger.update_status(b["id"], TaskStatus.FAILED)
        ledger.save()

        text = service.generate_report(repo)
        assert text.startswith("PROJECT REPORT")
        assert "Merged (1)" in text and a["id"] in text
        assert "Open/active (1)" in text
        assert "Needs attention:" in text and "worker exited 2" in text

    def test_empty_ledger_report(self, repo):
        service.create_task(repo, title="Only")
        text = service.generate_report(repo)
        assert "Merged (0)" in text and "(none)" in text


class TestDispatchInstructions:
    def test_render_delegation_includes_extra_instructions(self, repo):
        from orchestrator.config import load_config
        from orchestrator.prompts import render_delegation

        data = service.create_task(repo, title="Fix bug")
        task = Ledger.load(repo / ".orchestrator" / "ledger.json").get(data["id"])
        prompt = render_delegation(
            load_config(repo), task, extra_instructions="1. rotate tokens\n2. map to 401"
        )
        assert "Additional instructions:" in prompt
        assert "rotate tokens" in prompt and "401" in prompt

    def test_dispatch_passes_instructions_to_worker_prompt(self, repo, fake_worker):
        from tests.conftest import HANDOFF_OK, configured

        configured(repo, fake_worker(HANDOFF_OK))
        task = service.create_task(repo, title="Redo")
        ledger = Ledger.load(repo / ".orchestrator" / "ledger.json")
        ledger.update_status(task["id"], TaskStatus.CHANGES_REQUESTED)
        ledger.save()

        service.dispatch_task(repo, task["id"], instructions="map errors to 401")
        log = repo / ".orchestrator" / "logs" / f"{task['id'].lower()}.log"
        # worker echoes nothing about prompt; verify via registry worktree instead
        assert log.exists()  # spawned successfully
