# oc-orchestrator

OpenCode-backed orchestrator for my projects.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM "manager" agent
decomposes a goal into tasks; deterministic tooling owns the task ledger,
worker dispatch (`opencode run` in per-task worktrees), review plumbing,
and dependency-aware integration.

## Status

Iteration 3 — manager wiring:

- `init` wires a target repository: state dir, agent definitions (full
  repository-manager playbook + worker contract), MCP registration with a
  baked `--root`
- task ledger with JSON persistence and atomic writes
- MCP tools over stdio (`mcp>=1.2,<2`): `create_task`, `dispatch_task`,
  `task_status`, `list_tasks`, `get_task`, `cancel_task`
- per-task git worktrees on `agent/task-NNN-slug` branches (branch reuse on
  re-dispatch)
- background `opencode run --auto --dir <worktree>` workers with log capture,
  structured handoff parsing, dependency-gated dispatch, restart-safe registry
- verified end-to-end: headless manager session decomposes a goal into two
  concurrently dispatched workers and reconciles their results

PR review/integration automation lands in the next iteration.

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
