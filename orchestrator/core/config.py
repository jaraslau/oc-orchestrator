"""Orchestrator configuration persisted per target repository."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from orchestrator.core.storage import read_json, write_json_atomic

STATE_DIRNAME = ".orchestrator"
CONFIG_FILENAME = "config.json"
LEDGER_FILENAME = "ledger.json"


@dataclass
class Config:
    primary_branch: str = "main"
    branch_prefix: str = "agent/"
    worktree_dirname: str = "worktrees"
    logs_dirname: str = "logs"
    manager_agent: str = "orchestrator-manager"
    worker_agent: str = "orchestrator-worker"
    worker_model: str | None = None
    planner_model: str | None = None
    reviewer_model: str | None = None
    fallback_models: list[str] = field(default_factory=list)
    execution_backend: str = "server"
    worker_timeout: float = 3600.0
    gate_commands: list[str] = field(default_factory=list)
    opencode_bin: str = "opencode"
    server_port: int = 0
    gh_bin: str = "gh"
    merge_method: str = "squash"


def state_dir(root: Path) -> Path:
    return root / STATE_DIRNAME


def config_path(root: Path) -> Path:
    return state_dir(root) / CONFIG_FILENAME


def ledger_path(root: Path) -> Path:
    return state_dir(root) / LEDGER_FILENAME


def load_config(root: Path) -> Config:
    path = config_path(root)
    if not path.exists():
        return Config()
    data: Any = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save_config(root: Path, config: Config) -> None:
    write_json_atomic(config_path(root), asdict(config))
