"""Synchronous LLM calls via headless opencode sessions (planner, reviewer)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from orchestrator.core.config import Config

LlmRunner = Callable[[Path, Config, str, str | None, str | None], str]

DEFAULT_TIMEOUT_SECONDS = 900.0


def run_llm(
    root: Path,
    config: Config,
    prompt: str,
    model: str | None = None,
    agent: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    effort: str | None = None,
) -> str:
    cmd = [config.opencode_bin, "run", "--auto", "--dir", str(root)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["--variant", effort]
    if agent:
        cmd += ["--agent", agent]
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"llm call failed (exit {proc.returncode}): {proc.stderr.strip()[-400:]}"
        )
    return proc.stdout
