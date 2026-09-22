import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_ROOT", str(tmp_path))
    monkeypatch.setenv("CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("CORE_API_KEY", "test-owner-key")
    monkeypatch.setenv("CORE_PASSWORD", "test-owner-password")
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "notes"))
    monkeypatch.setenv("RESEARCH_REPO", str(tmp_path / "research"))
    monkeypatch.setenv("EXECUTOR_ONLY", "0")
    monkeypatch.delenv("MODEL_PROVIDERS_FILE", raising=False)
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("CAPABILITY_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("test_core", Path(__file__).resolve().parents[1] / "core_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def owner():
    return {"X-API-Key": "test-owner-key"}
