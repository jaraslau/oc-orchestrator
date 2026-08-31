from pathlib import Path

import pytest

from orchestrator.core.config import Config, load_config, save_config


class TestConfig:
    def test_defaults(self) -> None:
        config = Config()
        assert config.primary_branch == "main"
        assert config.branch_prefix == "agent/"
        assert config.worker_model is None

    def test_round_trip(self, tmp_path: Path) -> None:
        config = Config(primary_branch="develop", worker_model="opencode/grok-code")
        save_config(tmp_path, config)
        loaded = load_config(tmp_path)
        assert loaded == config

    def test_load_missing_returns_defaults(self, tmp_path: Path) -> None:
        assert load_config(tmp_path) == Config()

    def test_load_ignores_unknown_keys(self, tmp_path: Path) -> None:
        path = tmp_path / ".orchestrator" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"primary_branch": "main", "future_key": true}')
        assert load_config(tmp_path).primary_branch == "main"

    def test_save_is_atomic_shape(self, tmp_path: Path) -> None:
        save_config(tmp_path, Config())
        import json

        raw = json.loads((tmp_path / ".orchestrator" / "config.json").read_text())
        assert isinstance(raw, dict)

    def test_load_rejects_non_object(self, tmp_path: Path) -> None:
        path = tmp_path / ".orchestrator" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="JSON object"):
            load_config(tmp_path)
