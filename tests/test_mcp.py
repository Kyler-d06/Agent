import asyncio
import json
import sys
from pathlib import Path

from integrations import mcp_request
import mcp_bridge
import mcp_http_bridge


def test_real_mcp_stdio_discovery_and_invocation():
    config = {"transport": "stdio", "command": sys.executable, "args": [str(Path(__file__).parent / "fixtures" / "mcp_echo.py")]}
    tools = asyncio.run(mcp_request(config, "list"))
    assert [t["name"] for t in tools] == ["echo"]
    result = asyncio.run(mcp_request(config, "echo", {"text": "connected"}))
    assert result["content"][0]["text"] == "connected"


def test_actual_export_bridge_speaks_mcp_protocol():
    config = {"transport": "stdio", "command": sys.executable, "args": [str(Path(__file__).parent / "fixtures" / "mcp_export.py")]}
    tools = asyncio.run(mcp_request(config, "list"))
    assert tools[0]["name"] == "echo"
    result = asyncio.run(mcp_request(config, "echo", {"text": "roundtrip"}))
    assert json.loads(result["content"][0]["text"])["result"] == "roundtrip"


def test_mcp_bridge_loads_managed_key_without_client_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path))
    (tmp_path / "managed-secrets.json").write_text(
        json.dumps({"CORE_API_KEY": {"value": "managed-owner-key"}}), encoding="utf-8"
    )
    assert mcp_bridge._core_api_key() == "managed-owner-key"


def test_mcp_bridge_loads_vault_from_deployment_without_owner_state(tmp_path, monkeypatch):
    vault = tmp_path / "notes"
    monkeypatch.delenv("OBSIDIAN_VAULT", raising=False)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path))
    (tmp_path / "deployment.json").write_text(
        json.dumps({"obsidian_vault": str(vault)}), encoding="utf-8"
    )
    assert mcp_bridge._obsidian_vault() == vault.resolve()


def test_handoff_journal_exists_immediately_and_updates_without_tool_arguments(tmp_path):
    journal = mcp_bridge.HandoffJournal(tmp_path)
    assert journal.target.is_file()
    journal.record("workspace_read", {"ok": True, "result": {"content": "private tool output"}})
    journal.checkpoint({
        "objective": "Develop trailing-stop research",
        "current_status": "Backtester patched",
        "completed": ["Added ATR stop"],
        "decisions": ["Use chronological holdout"],
        "files_changed": ["trading_tools.py"],
        "tests": ["12 tests passed"],
        "next_steps": ["Run walk-forward test"],
        "blockers": [],
        "notes_for_next_model": "Do not inspect holdout while tuning.",
    })
    content = journal.target.read_text(encoding="utf-8")
    assert "Develop trailing-stop research" in content
    assert "workspace_read" in content
    assert "private tool output" not in content
    assert journal.calls_since_summary == 0


def test_http_bridge_uses_separate_derived_transport_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path))
    (tmp_path / "managed-secrets.json").write_text(
        json.dumps({"CORE_API_KEY": {"value": "managed-owner-key"}}), encoding="utf-8"
    )
    token = mcp_http_bridge.transport_token()
    assert token and token != "managed-owner-key"


def test_http_bridge_rejects_missing_bearer_token(monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setenv("CORE_API_KEY", "test-owner-key")
    monkeypatch.setattr(mcp_http_bridge, "build_server", lambda: mcp_bridge.build_server())
    with TestClient(mcp_http_bridge.create_app(token="expected")) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response.status_code == 401
