from orchestrator.server import DEFAULT_SERVER_NAME, build_server, run_serve


class FakeMCP:
    instances = []

    def __init__(self, name):
        self.name = name
        self.tools = {}
        self.ran = False
        FakeMCP.instances.append(self)

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def run(self):
        self.ran = True


class TestServe:
    def test_missing_mcp_package_returns_error(self, capsys):
        def boom(_name):
            raise ImportError("No module named 'mcp'")

        rc = run_serve(load_server=boom)
        assert rc == 1
        assert "mcp" in capsys.readouterr().err

    def test_build_server_registers_tools(self, tmp_path):
        fake = FakeMCP(DEFAULT_SERVER_NAME)
        built = build_server(lambda name: fake, root=tmp_path)
        assert built is fake
        expected = {
            "create_task",
            "dispatch_task",
            "task_status",
            "list_tasks",
            "get_task",
            "cancel_task",
        }
        assert expected <= set(fake.tools)

    def test_list_tasks_tool_roundtrip(self, tmp_path, monkeypatch):
        # init state manually via service, then call the MCP tool closure
        from orchestrator import service

        service.create_task(tmp_path, title="Tool check")
        fake = FakeMCP(DEFAULT_SERVER_NAME)
        build_server(lambda name: fake, root=tmp_path)
        rows = fake.tools["list_tasks"]()
        assert rows[0]["title"] == "Tool check"
