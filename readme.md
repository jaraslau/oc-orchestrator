# oc-orchestrator

OpenCode-backed orchestrator for my projects.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM "manager" agent
decomposes a goal into tasks; deterministic tooling owns the task ledger,
worker dispatch (`opencode run` in per-task worktrees), review plumbing,
and dependency-aware integration.

## Status

Iteration 5 — role assignment:

- everything from iterations 1–4
- per-task worker roles: `create_task(role=...)`, `dispatch_task(role=...)`
- resolution order: dispatch override > task role > default worker; missing
  agent definitions fail fast at create/dispatch time
- built-in role templates installed by `init`: `orchestrator-tester`,
  `orchestrator-reviewer` (+ default `orchestrator-worker`)
- custom roles: drop any `.md` into `.opencode/agent/` and reference its stem
- verified: 92 tests green, MCP surface smoke-checked

Requires `gh auth login` once for live GitHub operations.

Earlier iterations: foundations (ledger, CLI), worker dispatch (worktrees,
background workers), manager wiring (playbook agents, MCP tools), review &
integration (gh PR layer, changes-requested loop, reports).

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
