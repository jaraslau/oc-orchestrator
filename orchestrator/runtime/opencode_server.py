"""Lifecycle for an opencode server owned by the orchestrator.

Spawns `opencode serve`, waits for health, parses the listening URL (supports
ephemeral --port 0), and tears down on close.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
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
        self._output: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self.base_url: str | None = None

    def start(self) -> str:
        if self.base_url:
            return self.base_url
        log.info("starting %s serve (port=%d, cwd=%s)", self.binary, self.port, self.cwd)
        self._process = subprocess.Popen(
            [self.binary, "serve", "--port", str(self.port)],
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._start_output_reader()
        deadline = time.monotonic() + _START_TIMEOUT
        fixed_url = f"http://127.0.0.1:{self.port}" if self.port else None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise ServerStartError(
                    f"{self.binary} serve exited with code {self._process.returncode}"
                )
            url = self._read_listening_line() or fixed_url
            if url and self._healthy(url):
                self.base_url = url.rstrip("/")
                log.info("opencode server ready at %s", self.base_url)
                return self.base_url
            time.sleep(0.1)
        self.stop()
        raise ServerStartError(f"opencode server did not become healthy within {_START_TIMEOUT}s")

    def _read_listening_line(self) -> str | None:
        """Drain currently available output without blocking the startup deadline."""
        while True:
            try:
                line = self._output.get_nowait()
            except queue.Empty:
                return None
            match = _LISTEN_RE.search(line)
            if match:
                return match.group("url")

    def _start_output_reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self._output.put(line)

        self._reader = threading.Thread(
            target=read_output,
            name="opencode-server-output",
            daemon=True,
        )
        self._reader.start()

    def _healthy(self, url: str) -> bool:
        try:
            response = httpx.get(f"{url.rstrip('/')}/global/health", timeout=1.0, trust_env=False)
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
        if self._reader is not None:
            self._reader.join(timeout=1)
            self._reader = None

    def __enter__(self) -> OpencodeServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
