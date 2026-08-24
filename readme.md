# oc-orchestrator

OpenCode-backed orchestrator for my projects.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM "manager" agent
decomposes a goal into tasks; deterministic tooling owns the task ledger,
worker dispatch (`opencode run` in per-task worktrees), review plumbing,
and dependency-aware integration.

## Status

Iterations 1–5 complete:

- foundations: task ledger and CLI
- worker dispatch: isolated per-task git worktrees, background workers
- manager wiring: playbook agents, MCP tools
- review & integration: `gh` PR layer, changes-requested loop, reports
- role assignment: built-in roles `orchestrator-tester` and
  `orchestrator-reviewer`; custom roles via `.opencode/agent/`

The package lives at `orchestrator/` at the repo root. CI runs tests and
lint via a GitHub Actions workflow. Requires a one-time `gh auth login`
for live GitHub operations.

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
