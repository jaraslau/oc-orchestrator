"""MCP server exposing orchestration tools to the manager agent."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from orchestrator import errors
from orchestrator import service as _service

DEFAULT_SERVER_NAME = "oc-orchestrator"


def build_server(mcp_cls: Callable[[str], object], root: Path | None = None) -> object:
    base = root if root is not None else Path.cwd()

    mcp = mcp_cls(DEFAULT_SERVER_NAME)

    @mcp.tool()
    def create_task(
        title: str,
        objective: str = "",
        acceptance_criteria: list[str] | None = None,
        dependencies: list[str] | None = None,
        risks: list[str] | None = None,
    ) -> dict:
        """Create a task in the ledger with an assigned branch name."""
        return _service.create_task(
            base,
            title=title,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies,
            risks=risks,
        )

    @mcp.tool()
    def dispatch_task(
        task_id: str, model: str | None = None, instructions: str | None = None
    ) -> dict:
        """Dispatch a worker agent for a task (non-blocking). Use instructions to
        pass correction details when re-dispatching after changes were requested."""
        return _service.dispatch_task(base, task_id, model=model, instructions=instructions)

    @mcp.tool()
    def task_status(task_id: str, timeout_seconds: float = 0.0) -> dict:
        """Get current task state; optionally wait up to timeout_seconds for worker completion."""
        return _service.task_status(base, task_id, timeout=timeout_seconds)

    @mcp.tool()
    def list_tasks(status: str | None = None) -> list[dict]:
        """List tasks in the ledger, optionally filtered by status."""
        return _service.list_tasks(base, status=status)

    @mcp.tool()
    def get_task(task_id: str) -> dict:
        """Fetch full details of a single task including parsed handoff."""
        return _service.get_task(base, task_id)

    @mcp.tool()
    def cancel_task(task_id: str) -> dict:
        """Cancel a task and terminate its worker process if running."""
        return _service.cancel_task(base, task_id)

    @mcp.tool()
    def open_pr(task_id: str, title: str | None = None, body: str | None = None) -> dict:
        """Open (or reuse) a GitHub pull request for a REVIEWING task."""
        return _service.open_pr(base, task_id, title=title, body=body)

    @mcp.tool()
    def request_changes(task_id: str, comment: str) -> dict:
        """Request corrections on a task's PR; re-dispatch with instructions afterwards."""
        return _service.request_changes(base, task_id, comment)

    @mcp.tool()
    def merge_task(task_id: str) -> dict:
        """Merge a task's PR (dependency-aware) and clean up its worktree."""
        return _service.merge_task(base, task_id)

    @mcp.tool()
    def pr_diff(task_id: str) -> str:
        """Return the diff of a task's pull request."""
        return _service.pr_diff(base, task_id)

    @mcp.tool()
    def list_open_prs() -> list[dict]:
        """List open pull requests in this repository."""
        return _service.list_open_prs(base)

    @mcp.tool()
    def project_report() -> str:
        """Generate a project-level completion report from the ledger."""
        return _service.generate_report(base)

    return mcp


def run_serve(
    load_server: Callable[[str], object] | None = None,
    root: Path | None = None,
) -> int:
    """Start the stdio MCP server. Returns 0 on clean shutdown, 1 on setup failure."""
    if load_server is None:
        try:
            from mcp.server.fastmcp import FastMCP

            load_server = FastMCP
        except ImportError:
            print(
                "error: the 'mcp' package is not installed; run 'poetry install'",
                file=sys.stderr,
            )
            return 1

    try:
        mcp = build_server(load_server, root=root)
    except ImportError:
        print(
            "error: the 'mcp' package is not installed; run 'poetry install'",
            file=sys.stderr,
        )
        return 1
    except errors.OrchestratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mcp.run()
    return 0
