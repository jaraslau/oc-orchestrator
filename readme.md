# oc-orchestrator

OpenCode-backed orchestrator for my projects.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM "manager" agent
decomposes a goal into tasks; deterministic tooling owns the task ledger,
worker dispatch (`opencode run` in per-task worktrees), review plumbing,
and dependency-aware integration.

## Status

Iteration 2 — worker dispatch:

- `init` wires a target repository: state dir, agent definitions, MCP registration
- task ledger with JSON persistence and atomic writes
- MCP tools: `create_task`, `dispatch_task`, `task_status`, `list_tasks`, `get_task`, `cancel_task`
- per-task git worktrees on `agent/task-NNN-slug` branches (branch reuse on re-dispatch)
- background `opencode run` workers with `--dir` isolation, log capture, and
  structured handoff parsing; dependency-gated dispatch

Manager agent wiring and PR review/integration land in later iterations.

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
