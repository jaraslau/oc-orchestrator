"""MCP server exposing orchestration tools to the manager agent.

Iteration 1 ships only a health-check tool over the FastMCP plumbing.
The full tool set (task CRUD, dispatch, review) lands in later iterations;
the 'mcp' package is intentionally optional until then.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

DEFAULT_SERVER_NAME = "oc-orchestrator"


def run_serve(
    load_server: Callable[[str], object] | None = None,
) -> int:
    """Start the stdio MCP server. Returns 0 on clean shutdown, 1 on setup failure."""
    if load_server is None:
        try:
            from mcp.server.fastmcp import FastMCP

            load_server = FastMCP
        except ImportError:
            print(
                "error: the 'mcp' package is not installed; MCP support lands in a later milestone",
                file=sys.stderr,
            )
            return 1

    try:
        mcp = load_server(DEFAULT_SERVER_NAME)
    except ImportError:
        print(
            "error: the 'mcp' package is not installed; MCP support lands in a later milestone",
            file=sys.stderr,
        )
        return 1

    @mcp.tool()
    def ping() -> str:
        """Health check for the oc-orchestrator MCP server."""
        return "pong"

    mcp.run()
    return 0
