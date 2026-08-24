# oc-orchestrator

OpenCode-backed orchestrator for my projects.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM "manager" agent
decomposes a goal into tasks; deterministic tooling owns the task ledger,
worker dispatch (`opencode run` in per-task worktrees), review plumbing,
and dependency-aware integration.

## Status

Iteration 4 — review & integration:

- everything from iterations 1–3 (ledger, dispatch, MCP manager wiring)
- GitHub review layer via `gh`: `open_pr`, `pr_diff`, `request_changes`,
  `merge_task` (dependency-aware, squash default), `list_open_prs`
- changes-requested re-dispatch loop: `dispatch_task(task_id, instructions=...)`
  reuses the same branch/worktree and injects corrections into the prompt
- `oc-orchestrator report` / `project_report` tool: completion summary
- verified end-to-end incl. one full changes-requested cycle

Requires `gh auth login` once for live GitHub operations.

## Install

Requires Python 3.12+ and [Poetry](https://python-poetry.org/).

```sh
poetry install
```

## Usage

```sh
# wire up a target repository
poetry run oc-orchestrator init /path/to/project

# inspect its ledger
poetry run oc-orchestrator status /path/to/project

# run the MCP server (stdio)
poetry run oc-orchestrator serve

# smoke test the CLI
poetry run oc-orchestrator --version
```

## Development

```sh
poetry run pytest        # tests
poetry run ruff check .  # lint
```
