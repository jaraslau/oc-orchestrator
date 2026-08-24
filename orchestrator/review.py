"""Pull-request plumbing via the GitHub CLI (gh).

All commands run through an injectable runner so tests can substitute a
fake without touching subprocess.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.errors import GhError

Runner = Callable[..., subprocess.CompletedProcess[str]]

PR_NUMBER_RE = re.compile(r"/pull(?:s)?/(\d+)/?$")
LIST_FIELDS = ["number", "url", "headRefName", "title", "state"]


@dataclass
class PrInfo:
    number: int
    url: str
    head: str
    title: str
    state: str


def pr_number_from_url(url: str) -> int | None:
    m = PR_NUMBER_RE.search(url)
    return int(m.group(1)) if m else None


class GhClient:
    def __init__(self, root: Path, gh_bin: str = "gh", runner: Runner | None = None) -> None:
        self.root = root
        self.gh_bin = gh_bin
        if runner is not None:
            self._runner: Runner = runner
        else:
            gh = gh_bin

            def runner(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([gh, *args], capture_output=True, text=True, cwd=self.root)

            self._runner = runner

    def _run(self, *args: str) -> str:
        proc = self._runner(*args)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GhError(f"gh {' '.join(args[:2])} failed: {detail}")
        return proc.stdout

    def check(self) -> None:
        """Fail fast when gh is missing or unauthenticated."""
        self._run("auth", "status")

    def list_prs(self, head: str | None = None) -> list[PrInfo]:
        args = ["pr", "list", "--json", ",".join(LIST_FIELDS)]
        if head is not None:
            args += ["--head", head]
        out = self._run(*args).strip() or "[]"
        return [
            PrInfo(
                number=item["number"],
                url=item["url"],
                head=item.get("headRefName") or "",
                title=item.get("title") or "",
                state=item.get("state") or "",
            )
            for item in _json_array(out)
        ]

    def find_by_head(self, head: str) -> PrInfo | None:
        for pr in self.list_prs(head=head):
            if pr.state.upper() == "OPEN":
                return pr
        return None

    def create_pr(self, base: str, head: str, title: str, body: str) -> PrInfo:
        out = self._run(
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        )
        m = re.search(r"https://\S+", out)
        url = m.group(0).rstrip(".)") if m else ""
        number = pr_number_from_url(url) or 0
        return PrInfo(number=number, url=url, head=head, title=title, state="OPEN")

    def pr_diff(self, number: int) -> str:
        return self._run("pr", "diff", str(number))

    def comment(self, number: int, body: str) -> None:
        self._run("pr", "comment", str(number), "--body", body)

    def merge(self, number: int, method: str = "squash") -> None:
        self._run("pr", "merge", str(number), f"--{method}", "--delete-branch")


def _json_array(text: str) -> list:
    import json

    value = json.loads(text)
    if not isinstance(value, list):
        raise GhError(f"unexpected gh output: expected JSON array, got: {text[:120]}")
    return value


__all__ = ["GhClient", "GhError", "PrInfo", "pr_number_from_url"]
