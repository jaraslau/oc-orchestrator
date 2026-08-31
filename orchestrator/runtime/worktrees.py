"""Git worktree management for isolated per-task workspaces."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from orchestrator.core.config import Config, state_dir
from orchestrator.core.errors import WorktreeError
from orchestrator.logs import get

SLUG_MAX_LEN = 32
log = get("worktrees")


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:SLUG_MAX_LEN].rstrip("-")
    return slug or "task"


def branch_name(config: Config, task_id: str, title: str) -> str:
    number = task_id.removeprefix("TASK-").lower()
    return f"{config.branch_prefix}task-{number}-{slugify(title)}"


def worktree_path(root: Path, config: Config, branch: str) -> Path:
    return state_dir(root) / config.worktree_dirname / branch


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    log.debug("git -C %s %s", root, " ".join(args))
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        log.error("git failed (exit=%d): %s", proc.returncode, proc.stderr.strip())
        raise WorktreeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    log.debug("git exit=%d", proc.returncode)
    return proc


def resolve_base(root: Path, config: Config) -> str:
    """Return a resolvable base ref: local primary branch, else its remote tracking."""
    candidates = [config.primary_branch, f"origin/{config.primary_branch}"]
    for candidate in candidates:
        probe = _run_git(
            root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False
        )
        if probe.returncode == 0:
            return candidate
    raise WorktreeError(
        f"no base branch found; expected local '{config.primary_branch}' "
        f"or 'origin/{config.primary_branch}'"
    )


def branch_exists(root: Path, branch: str) -> bool:
    probe = _run_git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return probe.returncode == 0


def ensure_worktree(
    root: Path,
    config: Config,
    task_id: str,
    title: str,
    existing_branch: str | None = None,
) -> tuple[Path, str]:
    """Create (or recreate) the worktree for a task.

    Reuses an existing task branch when present so follow-up dispatches
    continue on the same branch. Returns (worktree_path, branch).
    """
    branch = existing_branch or branch_name(config, task_id, title)
    path = worktree_path(root, config, branch)
    if path.exists():
        _run_git(root, "worktree", "remove", "--force", str(path))
    base = resolve_base(root, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        _run_git(root, "worktree", "add", str(path), branch)
    else:
        _run_git(root, "worktree", "add", "-b", branch, str(path), base)
    log.info("worktree ready: task=%s branch=%s path=%s", task_id, branch, path)
    return path, branch


def remove_worktree(root: Path, config: Config, branch: str) -> bool:
    path = worktree_path(root, config, branch)
    if not path.exists():
        log.debug("worktree already absent: %s", path)
        return False
    proc = _run_git(root, "worktree", "remove", "--force", str(path), check=False)
    if proc.returncode != 0:
        # Stale or corrupted registration: fall back to plain directory removal.
        shutil.rmtree(path)
    log.info("worktree removed: branch=%s path=%s", branch, path)
    return True
