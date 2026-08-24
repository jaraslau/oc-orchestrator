import pytest

from orchestrator.core.config import Config
from orchestrator.core.errors import WorktreeError
from orchestrator.runtime.worktrees import (
    branch_exists,
    branch_name,
    ensure_worktree,
    remove_worktree,
    resolve_base,
    slugify,
)


class TestSlugify:
    def test_basic(self):
        assert slugify("Add Auth API!") == "add-auth-api"

    def test_truncates(self):
        assert len(slugify("a" * 100)) <= 32

    def test_empty_falls_back(self):
        assert slugify("!!!") == "task"


class TestBranchName:
    def test_format(self):
        config = Config()
        assert branch_name(config, "TASK-007", "Implement Login") == (
            "agent/task-007-implement-login"
        )


class TestWorktrees:
    def test_resolve_base_local(self, repo):
        assert resolve_base(repo, Config()) == "main"

    def test_resolve_base_missing_raises(self, tmp_path):
        (tmp_path / ".git").mkdir()  # bare-ish fake; no commits
        with pytest.raises(WorktreeError, match="no base branch"):
            resolve_base(tmp_path, Config())

    def test_ensure_creates_branch_and_dir(self, repo):
        config = Config()
        path, branch = ensure_worktree(repo, config, "TASK-001", "Do Thing")
        assert path.is_dir()
        assert branch == "agent/task-001-do-thing"
        assert branch_exists(repo, branch)
        remove_worktree(repo, config, branch)

    def test_reuse_existing_branch(self, repo):
        config = Config()
        _, branch = ensure_worktree(repo, config, "TASK-001", "Do Thing")
        # simulate a commit on the task branch, then re-dispatch
        import subprocess

        wt_path = repo / ".orchestrator" / "worktrees" / branch
        (wt_path / "work.txt").write_text("wip")
        subprocess.run(["git", "-C", str(wt_path), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(wt_path),
                "-c",
                "user.email=t@e.com",
                "-c",
                "user.name=T",
                "commit",
                "-qm",
                "wip",
            ],
            check=True,
        )
        _, branch2 = ensure_worktree(repo, config, "TASK-001", "Do Thing", existing_branch=branch)
        assert branch2 == branch
        assert branch_exists(repo, branch)  # same branch reused, not recreated from base
        remove_worktree(repo, config, branch)

    def test_remove_missing_is_noop(self, repo):
        assert remove_worktree(repo, Config(), "agent/task-999-nope") is False
