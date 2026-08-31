"""Delegation prompt rendering for worker dispatch."""

from __future__ import annotations

from pathlib import Path

from orchestrator.core.config import Config
from orchestrator.core.ledger import Task

HANDOFF_TEMPLATE = """\
```handoff
TASK: <task id>
STATUS: DONE | BLOCKED | FAILED
BRANCH: <your branch>
COMMIT: <latest commit sha on your branch>
SUMMARY: <what you did>
FILES CHANGED: <comma-separated paths>
TESTS RUN: <commands you ran>
TEST RESULTS: <pass/fail summary>
KNOWN ISSUES: <or none>
NOTES FOR MANAGER: <or none>
```"""


def render_delegation(
    config: Config,
    task: Task,
    extra_instructions: str | None = None,
    worker_agent: str | None = None,
    worktree: Path | None = None,
) -> str:
    criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria) or "- (none specified)"
    deps = ", ".join(task.dependencies) if task.dependencies else "(none)"
    extra = f"\nAdditional instructions:\n{extra_instructions}\n" if extra_instructions else ""
    return f"""You are Worker Agent {worker_agent or config.worker_agent}.

Task:
{task.id} - {task.title}

Objective:
{task.objective or "(none provided)"}

Repository:
{worktree.resolve() if worktree else "The current working directory"}
This isolated worktree is the only repository checkout you may access. Use paths inside it only.

Base branch:
{config.primary_branch}

Your branch:
{task.branch} (you are already checked out on it, in an isolated worktree)

Dependencies:
{deps}

Acceptance criteria:
{criteria}
{extra}
Instructions:

1. Work only on your assigned task.
2. Inspect existing code before modifying it.
3. Follow repository conventions.
4. Do not make unrelated refactors.
5. Run relevant tests and checks.
6. Add tests for new behavior where appropriate.
7. Commit your changes with a clear commit message.
8. Do not push your task branch; the manager owns integration and pushing.
9. Do not merge anything yourself.
10. Never commit to {config.primary_branch}.
11. Finish by printing exactly one fenced handoff block, and nothing after it:

{HANDOFF_TEMPLATE}
"""
