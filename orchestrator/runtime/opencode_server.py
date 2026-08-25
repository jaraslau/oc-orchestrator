"""Lifecycle for an opencode server owned by the orchestrator.

Spawns `opencode serve`, waits for health, parses the listening URL (supports
ephemeral --port 0), and tears down on close.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import httpx

from orchestrator.logs import get

log = get("server")

_LISTEN_RE = re.compile(r"listening on (?P<url>https?://\S+)")
_START_TIMEOUT = 20.0


class ServerStartError(RuntimeError):
    pass


class OpencodeServer:
    def __init__(self, binary: str, port: int, cwd: Path) -> None:
        self.binary = binary
        self.port = port
        self.cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self.base_url: str | None = None

    def start(self) -> str:
        if self.base_url:
            return self.base_url
        log.info("starting %s serve (port=%d, cwd=%s)", self.binary, self.port, self.cwd)
        self._process = subprocess.Popen(
            [self.binary, "serve", "--port", str(self.port)],
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise ServerStartError(
                    f"{self.binary} serve exited with code {self._process.returncode}"
                )
            url = self._read_listening_line()
            if url and self._healthy(url):
                self.base_url = url.rstrip("/")
                log.info("opencode server ready at %s", self.base_url)
                return self.base_url
            time.sleep(0.2)
        self.stop()
        raise ServerStartError(f"opencode server did not become healthy within {_START_TIMEOUT}s")

    def _read_listening_line(self) -> str | None:
        process = self._process
        if not process or not process.stdout:
            return None
        line = process.stdout.readline()
        match = _LISTEN_RE.search(line)
        return match.group("url") if match else None

    def _healthy(self, url: str) -> bool:
        try:
            response = httpx.get(f"{url.rstrip('/')}/global/health", timeout=1.0)
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    def stop(self) -> None:
        process = self._process
        self._process = None
        if not process or process.poll() is not None:
            return
        log.info("stopping opencode server")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def __enter__(self) -> OpencodeServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
