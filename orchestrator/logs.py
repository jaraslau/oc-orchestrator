"""Logging setup for the orchestrator.

File log always captures DEBUG; console mirrors at INFO (or DEBUG with
verbose=True). Everything the orchestrator decides, tries, or fails ends up in
`.orchestrator/logs/orchestrator.log`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_DIR_NAME = ".orchestrator"
LOG_FILE_NAME = "orchestrator.log"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def log_path(root: Path) -> Path:
    return root / LOG_DIR_NAME / "logs" / LOG_FILE_NAME


def setup_logging(root: Path, verbose: bool = False) -> Path:
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    return path


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"orchestrator.{name}")
