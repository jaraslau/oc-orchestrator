import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from orchestrator.runtime.opencode_server import OpencodeServer, ServerStartError


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def health_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make_fake_binary(tmp_path: Path, body: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "opencode"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    return str(exe)


def test_start_parses_listening_url_and_stop_terminates(tmp_path: Path, health_server: str) -> None:
    binary = make_fake_binary(
        tmp_path,
        f'echo "opencode server listening on {health_server}" >&2\nsleep 30',
    )
    server = OpencodeServer(binary, port=0, cwd=tmp_path)
    url = server.start()
    assert url == health_server
    server.stop()
    assert server._process is None or server._process.poll() is not None
    server.stop()


def test_start_raises_when_binary_exits(tmp_path: Path) -> None:
    binary = make_fake_binary(tmp_path, "echo boom >&2\nexit 3")
    server = OpencodeServer(binary, port=4998, cwd=tmp_path)
    with pytest.raises(ServerStartError, match="exited with code 3"):
        server.start()


def test_start_times_out_without_listening_line(tmp_path: Path) -> None:
    binary = make_fake_binary(tmp_path, "sleep 60")
    server = OpencodeServer(binary, port=4997, cwd=tmp_path)
    import orchestrator.runtime.opencode_server as mod

    original = mod._START_TIMEOUT
    mod._START_TIMEOUT = 0.5
    try:
        with pytest.raises(ServerStartError, match="did not become healthy"):
            server.start()
    finally:
        mod._START_TIMEOUT = original
