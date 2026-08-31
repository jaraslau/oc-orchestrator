"""Logging setup for the orchestrator.

File log always captures TRACE and above; console mirrors at INFO (or DEBUG with
verbose=True). Everything the orchestrator decides, tries, or fails ends up in
rotated `.orchestrator/logs/orchestrator.log` files.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR_NAME = ".orchestrator"
LOG_FILE_NAME = "orchestrator.log"
TRACE = 5
_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s "
    "pid=%(process)d thread=%(threadName)s %(name)s: %(message)s"
)
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def log_path(root: Path) -> Path:
    return root / LOG_DIR_NAME / "logs" / LOG_FILE_NAME


def setup_logging(root: Path, verbose: bool = False) -> Path:
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.addLevelName(TRACE, "TRACE")
    logger = logging.getLogger("orchestrator")
    logger.setLevel(TRACE)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    file_handler = RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(TRACE)
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    logging.captureWarnings(True)
    logger.debug("logging initialized (root=%s, file=%s, verbose=%s)", root, path, verbose)
    return path


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"orchestrator.{name}")


def traced[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Log boundary timing and full exceptions without changing behavior."""
    logger = get(function.__module__.removeprefix("orchestrator."))

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.monotonic()
        logger.debug("%s started", function.__name__)
        try:
            result = function(*args, **kwargs)
        except Exception:
            logger.exception("%s failed after %.3fs", function.__name__, time.monotonic() - started)
            raise
        logger.debug("%s finished in %.3fs", function.__name__, time.monotonic() - started)
        return result

    return wrapped
