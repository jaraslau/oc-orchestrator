"""Exception hierarchy for oc-orchestrator."""

from __future__ import annotations


class OrchestratorError(RuntimeError):
    """Base class for all oc-orchestrator errors."""


class TaskNotFound(OrchestratorError):
    pass


class InvalidState(OrchestratorError):
    """Operation not valid for the task's current state."""


class DispatchBlocked(OrchestratorError):
    """Task cannot be dispatched yet (e.g. unmet dependencies)."""


class WorktreeError(OrchestratorError):
    """Git worktree/branch plumbing failure."""


class GhError(OrchestratorError):
    """GitHub CLI (gh) invocation failure."""


class GhChecksFailed(GhError):
    """GitHub checks completed unsuccessfully."""
