from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestrator.server import DEFAULT_SERVER_NAME, build_server, run_serve


class FakeMCP:
    instances: list[FakeMCP] = []

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.tools: dict[str, Callable[..., Any]] = {}
        self.ran: bool = False
        FakeMCP.instances.append(self)

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def run(self) -> None:
        self.ran = True


class TestServe:
    def test_missing_mcp_package_returns_error(self, capsys: Any) -> None:
        def boom(_name: str) -> FakeMCP:
            raise ImportError("No module named 'mcp'")

        rc: int = run_serve(load_server=boom)
        assert rc == 1
        assert "mcp" in capsys.readouterr().err

    def test_build_server_registers_tools(self, tmp_path: Path) -> None:
        fake = FakeMCP(DEFAULT_SERVER_NAME)
        built = build_server(lambda name: fake, root=tmp_path)
        assert built is fake
        expected: set[str] = {
            "create_task",
            "dispatch_task",
            "task_status",
            "list_tasks",
            "get_task",
            "cancel_task",
        }
        assert expected <= set(fake.tools)

    def test_list_tasks_tool_roundtrip(self, tmp_path: Path, monkeypatch: Any) -> None:
        # init state manually via service, then call the MCP tool closure
        from orchestrator.orchestration import service

        service.create_task(tmp_path, title="Tool check")
        fake = FakeMCP(DEFAULT_SERVER_NAME)
        build_server(lambda name: fake, root=tmp_path)
        rows: list[dict[str, Any]] = fake.tools["list_tasks"]()
        assert rows[0]["title"] == "Tool check"

    def test_all_tools_delegate_to_service_no_shadowing(  # noqa: E501
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # regression for TASK-003 incident: closures must call _service.*, not
        # themselves (create_task recursed into itself until fixed).
        from orchestrator.orchestration import service as service_module

        calls: list[str] = []

        def make_tracked(name: str, ret: Any) -> Callable[..., Any]:
            def _fn(*a: Any, **kw: Any) -> Any:
                calls.append(name)
                return ret

            return _fn

        fakes: dict[str, Callable[..., Any]] = {
            "create_task": make_tracked("create_task", {"id": "TASK-001"}),
            "dispatch_task": make_tracked("dispatch_task", {}),
            "task_status": make_tracked("task_status", {}),
            "list_tasks": make_tracked("list_tasks", []),
            "get_task": make_tracked("get_task", {}),
            "cancel_task": make_tracked("cancel_task", {}),
        }
        for name, fn in fakes.items():
            monkeypatch.setattr(service_module, name, fn, raising=False)

        fake = FakeMCP(DEFAULT_SERVER_NAME)
        build_server(lambda n: fake, root=tmp_path)

        fake.tools["create_task"](title="x")
        fake.tools["dispatch_task"]("TASK-001")
        fake.tools["task_status"]("TASK-001")
        fake.tools["list_tasks"]()
        fake.tools["get_task"]("TASK-001")
        fake.tools["cancel_task"]("TASK-001")

        assert sorted(set(calls)) == sorted(fakes)
