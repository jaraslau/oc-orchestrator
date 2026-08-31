# oc-orchestrator

OpenCode-backed orchestrator for my projects.
Most of the code was written via said orchestrator with Big Pickle as a primary model.

oc-orchestrator coordinates multiple autonomous coding agents working
concurrently in isolated git branches/worktrees. An LLM plans and reviews;
deterministic tooling owns everything else: the task ledger, worker dispatch
(`opencode run` in per-task worktrees), gates, correction loops, and
dependency-aware integration.

## Concepts

| Piece | What it is |
| --- | --- |
| **Manager** | LLM agent that decomposes a goal into tasks (playbook shipped with `init`) |
| **Worker** | Headless `opencode run --auto` session in an isolated git worktree |
| **Role** | Worker persona: `orchestrator-worker`, `orchestrator-tester`, `orchestrator-reviewer`, or any custom `.md` you drop in |
| **Ledger** | `.orchestrator/ledger.json` — atomic, restart-safe task state |
| **Handoff** | Structured block every worker must end its log with; parsed to drive status transitions |
| **Supervisor** | The `run` command: plan → dispatch → poll → gate → review → merge/rework loop |

## Install

Requires Python 3.12+, [Poetry](https://python-poetry.org/), and
[opencode](https://opencode.ai) authenticated on your PATH. Optional:
[`gh`](https://cli.github.com/) authenticated for PR-based review/merge.

```sh
# daily driver across your repos (recommended)
poetry build && pipx install dist/*.whl

# or editable dev install (command available under poetry run)
poetry install
```

## Quick start

```sh
cd your-project

# ONE TIME: state dir + agent definitions + MCP registration
oc-orchestrator init

# REQUIRED: definitions must be tracked so worker worktrees can see them
git add .opencode && git commit -m "chore: self-host orchestrator"

# preview what the planner would do
oc-orchestrator run --dry-run "refactor auth into a service layer"

# full autonomy
oc-orchestrator run "refactor auth into a service layer"
```

Exit code is `0` only when **every** task merged.

## The dark factory: `run`

```sh
oc-orchestrator run "<goal>" [--path REPO] [--dry-run]
    [--max-workers N] [--max-loops N] [--max-retries N]
    [--max-corrections N] [--push]
```

Pipeline:

1. **Plan** — LLM chooses the smallest useful worker count and emits JSON tasks
   with objective, criteria, dependencies, role, configured model, and effort.
   Invalid plans retry once, then degrade to one general worker.
2. **Create** — ledger entries on branch names `agent/task-NNN-slug`;
   dependents cannot dispatch until their parents MERGE.
3. **Dispatch** — ready tasks get isolated worktrees and background sessions,
   capped by `max_parallel_tasks` / `--max-workers`.
4. **Work** — worker commits to its branch and ends its log with a
   fenced ```handoff``` block.
5. **Reconcile** — exit code + parsed handoff → `REVIEWING`; failures retry up
   to `--max-retries`, then give-up (`BLOCKED`).
6. **Gate** — `gate_commands` from config run inside the worktree.
7. **Review** — reviewer LLM judges diff vs criteria + gate output. Malformed or
   unavailable reviews retry and fail closed; a failed gate cannot be overridden.
8. **Integrate** — approved: `--no-ff` merge into primary, worktree removed,
   dependents auto-unblock. Changes: same-branch re-dispatch within
   `--max-corrections`.
9. **Report** — completion summary; `--push` optionally publishes primary.

The LLM touches exactly two decision points (plan, review verdict); sequencing,
isolation, gating, integration, and state are deterministic code.

## Conversational mode (MCP)

Skip `run` and steer manually: launch `opencode` inside an initialized repo.
The manager playbook and MCP tools are already registered by `init`
(`.opencode/opencode.json`). Ask it to decompose and dispatch; you decide when
to open PRs, request changes, or merge.

## CLI reference

```sh
oc-orchestrator init [PATH]              # wire up a repository
oc-orchestrator run GOAL [--path PATH]   # dark factory (see above)
oc-orchestrator serve [--root PATH]      # MCP stdio server
oc-orchestrator status [PATH]            # dump the ledger
oc-orchestrator report [PATH]            # completion summary
oc-orchestrator -v ...                   # verbose logging (any command)
oc-orchestrator --version
```

`serve` resolves the target repo from cwd; `OC_ORCHESTRATOR_ROOT` overrides.

## MCP tools

`create_task(title, objective?, acceptance_criteria?, dependencies?,
role?, model?, effort?)` · `dispatch_task(task_id, model?, instructions?,
role?, effort?)` ·
`task_status(task_id, timeout_seconds?)` · `list_tasks` · `get_task(task_id)` ·
`cancel_task(task_id)` · `open_pr(task_id)` · `pr_diff(task_id)` ·
`request_changes(task_id, comment)` · `merge_task(task_id)` ·
`list_open_prs()` · `project_report()`

`dispatch_task(instructions=...)` re-dispatches on the same branch — that is
the changes-requested loop.

## Configuration

`.orchestrator/config.json` (created by `init`; all keys optional):

| Key | Default | Purpose |
| --- | --- | --- |
| `primary_branch` | `"main"` | integration target |
| `branch_prefix` | `"agent/"` | task branch namespace |
| `worker_agent` | `orchestrator-worker` | default persona |
| `worker_model` / `planner_model` / `reviewer_model` | `null` | model pins per role; unset = opencode default |
| `fallback_models` | `[]` | ordered chain of backup models; on provider failure the next model is tried automatically |
| `max_parallel_tasks` | `4` | maximum workers live at once; `--max-workers` overrides it |
| `worker_timeout` | `3600` | seconds before a running worker is aborted |
| `gate_commands` | `[]` | shell commands run in the worktree before review, e.g. `["poetry run pytest -q", "ruff check ."]` |
| `gh_bin` | `"gh"` | GitHub CLI binary |
| `merge_method` | `"squash"` | used by gh PR merges |

Per-task overrides beat config: `create_task(role=..., model=..., effort=...)` /
`dispatch_task(role=..., model=..., effort=...)`. `effort` is the reasoning
variant passed to opencode (`--variant`, e.g. `high`/`medium`/`low`);
the planner picks it per task — high for architecture/debugging, low for
mechanical chores.

## Resilience & observability

Errors are classified into recoverable (auth, quota, rate limit,
model-not-found, provider-unavailable, transient network) and hard failures
(context overflow, content filters, bugs in our code). Recoverable errors trigger
**automatic failover** through `fallback_models`; an unavailable task-selected
model degrades to that chain and the server default.

All decisions, model switches, HTTP/subprocess timing, state transitions, server output,
and full error tracebacks are written to `.orchestrator/logs/orchestrator.log` (TRACE
level, rotated at 10 MiB with five backups). Run with `-v` / `--verbose`
for live stderr output including per-tool call status, failover switches, and
session lifecycle events.

The `worker_status` worker dict (from `task_status`) includes `session_id` and
`model_used` — the actual model that completed the work.

Workers use a shared `opencode serve` instance.

## Roles & custom agents

`init` installs four definitions into `.opencode/agent/`: manager playbook,
default worker contract, tester, reviewer. Custom role = drop any `my-role.md`
there, commit, then use `role="my-role"`. Resolution order: dispatch override >
task's assigned role > config default; missing definitions fail fast at
create/dispatch time.

## Task statuses

`PLANNED → DISPATCHED → WORKING → REVIEWING → MERGED`
with detours: `BLOCKED` (unmet dependencies or supervisor give-up),
`CHANGES_REQUESTED` (re-dispatch loop), `FAILED`, `CANCELLED`.
`REVIEWING` means "work finished, awaiting review" — it is the success state,
not an error.

## Observability

- `oc-orchestrator status` / `report`
- per-worker logs: `.orchestrator/logs/task-NNN.log` (all retry/correction sessions appended)
- dispatch registry: `.orchestrator/dispatches.json`

Ledger and dispatch writes are atomic. A supervised run also cancels and marks
unfinished work `BLOCKED` when its loop budget expires. A hard process kill may
still require cancelling stale sessions before starting a new run.

## Architecture

```
orchestrator/
  cli.py                    argparse entry points
  server.py                 MCP tool registration (FastMCP)
  core/
    config.py               per-repo Config (+ paths)
    ledger.py               Task dataclass, statuses, atomic JSON persistence
    storage.py              read/write_json_atomic
    errors.py               typed exceptions
  runtime/
    dispatcher.py           background spawn, registry, log capture, handoff parse
    worktrees.py            ensure/remove per-task worktrees
    github.py               GhClient over the gh CLI
  orchestration/
    prompts.py              delegation prompt rendering
    service.py              operations shared by CLI/MCP/supervisor
    supervisor.py           run_goal control loop, planning, gates, integration
```

Worker isolation model: one branch + one worktree per task under
`.orchestrator/worktrees/<branch>`; re-dispatch reuses the branch.

## Caveats

- Workers burn real tokens; expect minutes per task.
- `.opencode/agent/*` must be committed or dispatch breaks in fresh worktrees.
- Without `gh auth login`, PR tools are unavailable; the supervisor merges
  locally instead.
- Gates are only as good as `gate_commands` — set them for real repos.
- One ledger per repo; don't run two supervisors against the same root.

## Development

```sh
poetry run pytest        # tests
poetry run ruff check .  # lint
```

Layout follows the architecture table above; tests live in `tests/` with fake
workers/planners so the suite runs without network or opencode.
