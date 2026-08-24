from orchestrator.server import run_serve


class FakeMCP:
    def __init__(self, name):
        self.name = name
        self.tools = {}
        self.ran = False

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
        assert "'mcp'" in capsys.readouterr().err

    def test_registers_ping_and_runs(self):
        fake = FakeMCP("expected-name")
        rc = run_serve(load_server=lambda _name: fake)
        assert rc == 0
        assert fake.ran is True
        assert fake.tools["ping"]() == "pong"
