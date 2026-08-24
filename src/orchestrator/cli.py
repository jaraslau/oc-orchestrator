"""Command line interface for oc-orchestrator."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from orchestrator import __version__
from orchestrator.config import ledger_path, load_config, save_config
from orchestrator.ledger import Ledger
from orchestrator.server import run_serve
from orchestrator.storage import read_json, write_json_atomic

OPENCODE_DIRNAME = ".opencode"
OPENCODE_CONFIG_FILENAME = "opencode.json"
AGENT_SUBDIR = "agent"
MANAGER_AGENT_FILENAME = "orchestrator-manager.md"
WORKER_AGENT_FILENAME = "orchestrator-worker.md"
MCP_SERVER_NAME = "oc-orchestrator"
GITIGNORE_ENTRY = ".orchestrator/"
AGENTS_PACKAGE = "orchestrator.agents"
ROLES_PACKAGE = "orchestrator.agents.roles"
ROOT_ENV_VAR = "OC_ORCHESTRATOR_ROOT"


def _mcp_server_entry(root: Path) -> dict:
    return {
        "type": "local",
        "command": ["oc-orchestrator", "serve", "--root", str(root)],
        "enabled": True,
    }


def _load_agent_definition(filename: str) -> str:
    return resources.files(AGENTS_PACKAGE).joinpath(filename).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oc-orchestrator",
        description="OpenCode-backed multi-agent repository orchestrator",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize a repository for orchestration")
    p_init.add_argument("path", nargs="?", default=".", type=Path)

    p_serve = sub.add_parser("serve", help="run the MCP server (stdio)")
    p_serve.add_argument("--root", type=Path, default=None)

    p_status = sub.add_parser("status", help="show the task ledger")
    p_status.add_argument("path", nargs="?", default=".", type=Path)

    p_report = sub.add_parser("report", help="project completion report from the ledger")
    p_report.add_argument("path", nargs="?", default=".", type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init,
        "serve": cmd_serve,
        "status": cmd_status,
        "report": cmd_report,
    }
    return handlers[args.command](args)


def cmd_init(args: argparse.Namespace) -> int:
    root = args.path.resolve()
    if not (root / ".git").exists():
        print(f"warning: {root} does not look like a git repository", file=sys.stderr)

    save_config(root, load_config(root))  # create-or-preserve, filling defaults
    Ledger(ledger_path(root)).save()

    agents_dir = root / OPENCODE_DIRNAME / AGENT_SUBDIR
    agents_dir.mkdir(parents=True, exist_ok=True)
    installed_agents = [MANAGER_AGENT_FILENAME, WORKER_AGENT_FILENAME]
    (agents_dir / MANAGER_AGENT_FILENAME).write_text(
        _load_agent_definition(MANAGER_AGENT_FILENAME), encoding="utf-8"
    )
    (agents_dir / WORKER_AGENT_FILENAME).write_text(
        _load_agent_definition(WORKER_AGENT_FILENAME), encoding="utf-8"
    )
    for role_file in resources.files(ROLES_PACKAGE).iterdir():
        if role_file.name.endswith(".md") and role_file.is_file():
            (agents_dir / role_file.name).write_text(role_file.read_text(encoding="utf-8"))
            installed_agents.append(role_file.name)

    ok = _merge_opencode_config(root)
    ignored = _ensure_gitignore_entry(root)

    print(f"initialized orchestration state in {root}")
    print(f"  state dir:   {root / '.orchestrator'}")
    agents_list = ", ".join(sorted(installed_agents))
    print(f"  agents:      {OPENCODE_DIRNAME}/{AGENT_SUBDIR}/ ({agents_list})")
    print(f"  mcp config:  {OPENCODE_DIRNAME}/{OPENCODE_CONFIG_FILENAME}")
    if not ok:
        return 1
    if ignored:
        print(f"  added {GITIGNORE_ENTRY} to .gitignore")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    return run_serve(root=root)


def _resolve_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    env = os.environ.get(ROOT_ENV_VAR)
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def cmd_status(args: argparse.Namespace) -> int:
    root = args.path.resolve()
    path = root / ".orchestrator" / "ledger.json"
    if not path.exists():
        print(f"no ledger found at {path}; run 'oc-orchestrator init' first", file=sys.stderr)
        return 1
    ledger = Ledger.load(path)
    rows = ledger.filter()
    if not rows:
        print("(ledger is empty)")
        return 0
    width = max(len(t.id) for t in rows)
    print(f"{'ID':<{width}}  {'STATUS':<17} BRANCH")
    for t in rows:
        print(f"{t.id:<{width}}  {t.status.value:<17} {t.branch or '-'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from orchestrator.service import generate_report

    root = args.path.resolve()
    path = root / ".orchestrator" / "ledger.json"
    if not path.exists():
        print(f"no ledger found at {path}; run 'oc-orchestrator init' first", file=sys.stderr)
        return 1
    print(generate_report(root))
    return 0


def _merge_opencode_config(root: Path) -> bool:
    cfg_path = root / OPENCODE_DIRNAME / OPENCODE_CONFIG_FILENAME
    if cfg_path.exists():
        try:
            data = read_json(cfg_path)
        except ValueError as exc:
            print(
                f"error: {cfg_path} is not valid JSON ({exc}); "
                "fix it manually and re-run 'oc-orchestrator init'",
                file=sys.stderr,
            )
            return False
        if not isinstance(data, dict):
            print(f"error: {cfg_path} must contain a JSON object", file=sys.stderr)
            return False
    else:
        data = {"$schema": "https://opencode.ai/config.json"}

    servers = data.setdefault("mcp", {})
    servers[MCP_SERVER_NAME] = _mcp_server_entry(root)
    write_json_atomic(cfg_path, data)
    return True


def _ensure_gitignore_entry(root: Path, entry: str = GITIGNORE_ENTRY) -> bool:
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if entry in (line.strip() for line in lines):
        return False
    with gi.open("a", encoding="utf-8") as f:
        if lines and lines[-1].strip():
            f.write("\n")
        f.write(f"{entry}\n")
    return True
