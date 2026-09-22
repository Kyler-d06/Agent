import json

import start_platform
from platform_contracts import safe_env


def test_first_run_creates_private_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_DASHBOARD_PASSWORD", "correct-horse-battery")
    unavailable_dsh = {"available": False, "version": None, "model": None, "context_window": None,
                       "runtime_patch": None}
    monkeypatch.setattr(start_platform, "prepare_dsh_runtime_patch", lambda *_args, **_kwargs: unavailable_dsh)
    monkeypatch.setattr(start_platform, "discover_dsh", lambda: unavailable_dsh)
    monkeypatch.setattr(start_platform, "port_is_free", lambda *_args: True)
    settings = start_platform.load_or_create_settings(tmp_path)
    secret_records = start_platform.load_or_create_secrets(tmp_path)
    providers = start_platform.ensure_provider_config(tmp_path, settings)
    env = start_platform.apply_environment(tmp_path, settings, secret_records, providers)
    monkeypatch.setattr(start_platform, "_ollama_inventory", lambda: [])
    config = start_platform.build_maintenance_config(tmp_path, settings)

    assert env["CORE_BIND"] == "127.0.0.1"
    assert env["CORE_ROOT"] != str(start_platform.SOURCE_DIR)
    settings["workspace"] = str(tmp_path / "selected-repo")
    settings["obsidian_vault"] = str(tmp_path / "my-vault")
    settings["research_repo"] = str(tmp_path / "selected-repo")
    custom_env = start_platform.apply_environment(tmp_path, settings, secret_records, providers)
    assert custom_env["CORE_ROOT"].endswith("selected-repo")
    assert custom_env["OBSIDIAN_VAULT"].endswith("my-vault")
    assert custom_env["RESEARCH_REPO"] == custom_env["CORE_ROOT"]
    chosen_repo = tmp_path / "chosen-repo"
    chosen_vault = tmp_path / "chosen-vault"
    (chosen_repo / ".git").mkdir(parents=True)
    chosen_vault.mkdir()
    answers = iter([str(chosen_repo), str(chosen_vault)])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    configured = start_platform.configure_paths(tmp_path, settings)
    assert configured["workspace"] == str(chosen_repo.resolve())
    assert configured["obsidian_vault"] == str(chosen_vault.resolve())
    assert json.loads((tmp_path / "deployment.json").read_text())["research_repo"] == str(chosen_repo.resolve())
    assert env["CORE_PASSWORD"] == "correct-horse-battery"
    assert env["MODEL_MAX_CONCURRENCY"] == "1"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "8192"
    assert env["DSH_TELEMETRY_MODE"] == "DISABLED"
    assert env["PYTHONIOENCODING"] == "utf-8:replace"
    assert env["TELEGRAM_API_KEY"] != env["CORE_API_KEY"]
    assert json.loads(providers.read_text(encoding="utf-8"))["routing"]["allow_paid"] is False
    assert {service["name"] for service in config["services"]} >= {
        "core", "assistant-worker", "mcp-http", "system-monitor", "memory-consolidation"
    }
    assert set(config["readiness"]) >= {"ollama_installed", "docker_daemon_ready"}
    assert set(config["readiness"]) >= {"dsh_installed", "free_web_retrieval_ready", "web_search_backend"}
    assert "CORE_SECRET" in next(service for service in config["services"] if service["name"] == "core")["env_allowlist"]
    assert all("PYTHONIOENCODING" in service["env_allowlist"] for service in config["services"]
               if service["command"][0].lower().endswith("python.exe"))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "123456")
    pilot = start_platform.build_maintenance_config(tmp_path, settings, pilot=True)
    pilot_names = {service["name"] for service in pilot["services"] if service.get("enabled", True)}
    expected_pilot = {"core", "assistant-worker", "mcp-http", "overnight-report", "telegram"}
    if start_platform.os.name == "nt":
        expected_pilot.add("approval-notifier")
    if start_platform._ollama_executable():
        expected_pilot.add("local-model")
    assert pilot_names == expected_pilot
    telegram = next(service for service in pilot["services"] if service["name"] == "telegram")
    assert "TELEGRAM_API_KEY" in telegram["env_allowlist"]
    assert "CORE_API_KEY" not in telegram["env_allowlist"]
    settings["loops"] = {
        "assistant_poll_seconds": 7, "approval_notification_seconds": 9,
        "system_monitor_seconds": 20, "memory_consolidation_seconds": 1800,
        "discovery_seconds": 600, "overnight_report_seconds": 120,
    }
    overnight = start_platform.build_maintenance_config(tmp_path, settings, overnight=True)
    discovery = next(service for service in overnight["services"] if service["name"] == "discovery")
    assert discovery["enabled"] is True
    assert discovery["command"][1:4] == ["discovery_worker.py", "--interval", "600"]
    report = next(service for service in overnight["services"] if service["name"] == "overnight-report")
    assert report["enabled"] is True and report["command"][1:] == ["overnight_report_worker.py", "--interval", "120"]
    assert next(service for service in overnight["services"] if service["name"] == "assistant-worker")["command"][-1] == "7"
    assert next(service for service in overnight["services"] if service["name"] == "approval-notifier")["command"][-1] == "9"
    assert next(service for service in overnight["services"] if service["name"] == "system-monitor")["command"][-1] == "20"
    assert next(service for service in overnight["services"] if service["name"] == "memory-consolidation")["command"][-1] == "1800"
    mcp_http = next(service for service in overnight["services"] if service["name"] == "mcp-http")
    assert mcp_http["enabled"] is True and mcp_http["command"][-2:] == ["--port", "5078"]
    assert overnight["readiness"]["mcp_http_url"] == "http://127.0.0.1:5078/mcp"
    assert overnight["readiness"]["overnight_mode"] is True
    trading = start_platform.build_maintenance_config(tmp_path, settings, trading=True)
    assert trading["readiness"]["trading_research_mode"] is True
    assert trading["readiness"]["executor_only"] is True
    assert trading["readiness"]["local_reasoning_enabled"] is False
    assert next(service for service in trading["services"] if service["name"] == "assistant-worker")["enabled"] is False
    assert next(service for service in trading["services"] if service["name"] == "discovery")["enabled"] is False
    assert next(service for service in trading["services"] if service["name"] == "memory-consolidation")["enabled"] is False
    assert not any(service["name"] == "local-model" and service["enabled"] for service in trading["services"])
    assert not any(service["name"] == "dsh-web" and service["enabled"] for service in trading["services"])
    core_service = next(service for service in trading["services"] if service["name"] == "core")
    assert {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "BRAVE_API_KEY", "OBSIDIAN_VAULT", "EXECUTOR_ONLY"} <= set(core_service["env_allowlist"])
    detected = start_platform._pilot_provider_config([
        {"name": "completion-small", "size": 1, "capabilities": ["completion"]},
        {"name": "tool-small", "size": 2, "capabilities": ["completion", "tools"]},
    ])
    assert detected["providers"][0]["model"] == "tool-small"
    assert detected["providers"][0]["supports_tools"] is True
    assert detected["providers"][0]["structured_text"] is False

    runtime_patch = tmp_path / "dsh" / "runtime.cordis.yml"
    runtime_patch.parent.mkdir(parents=True, exist_ok=True)
    runtime_patch.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DSH_RUNTIME_PATCH", str(runtime_patch))
    monkeypatch.setattr(start_platform, "discover_dsh", lambda: {
        "available": True, "version": "test", "model": "qwen3.5:4b", "context_window": 8192,
        "runtime_patch": str(runtime_patch),
    })
    monkeypatch.setattr(start_platform, "dsh_web_command",
                        lambda **kwargs: ["node", "dsh", "web", str(kwargs["port"])])
    monkeypatch.setattr(start_platform, "port_is_free", lambda *_args: True)
    integrated = start_platform.build_maintenance_config(tmp_path, settings, pilot=True)
    dsh_service = next(service for service in integrated["services"] if service["name"] == "dsh-web")
    assert dsh_service["command"] == ["node", "dsh", "web", "3080"]
    assert "DSH_TELEMETRY_MODE" in dsh_service["env_allowlist"]
    assert integrated["readiness"]["dsh_model"] == "qwen3.5:4b"
    assert integrated["readiness"]["dsh_context_window"] == 8192


def test_existing_secrets_are_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_DASHBOARD_PASSWORD", "first-password-value")
    first = start_platform.load_or_create_secrets(tmp_path)
    monkeypatch.setenv("ASSISTANT_DASHBOARD_PASSWORD", "different-password")
    second = start_platform.load_or_create_secrets(tmp_path)
    assert second == first


def test_existing_local_ollama_provider_gets_nonthinking_migration(tmp_path):
    target = tmp_path / "providers.json"
    target.write_text(json.dumps({"routing": {}, "providers": [{
        "name": "old-local", "type": "api", "cost_class": "local",
        "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3.5:4b",
        "task_types": ["general"],
    }]}), encoding="utf-8")
    start_platform.ensure_provider_config(tmp_path, {"providers_file": str(target)})
    provider = json.loads(target.read_text(encoding="utf-8"))["providers"][0]
    assert provider["reasoning_effort"] == "none"
    assert {"critic", "capability_build", "evaluation", "improvement", "memory_consolidation"} <= set(provider["task_types"])


def test_existing_playwright_provider_is_removed_from_active_config(tmp_path):
    target = tmp_path / "providers.json"
    target.write_text(json.dumps({"routing": {}, "providers": [
        {"name": "local", "type": "api", "cost_class": "local", "base_url": "http://127.0.0.1:11434/v1"},
        {"name": "browser-seat", "type": "playwright", "url": "https://example.com/chat"},
    ]}), encoding="utf-8")

    start_platform.ensure_provider_config(tmp_path, {"providers_file": str(target)})

    providers = json.loads(target.read_text(encoding="utf-8"))["providers"]
    assert [provider["name"] for provider in providers] == ["local"]


def test_preflight_labels_overnight_mode(tmp_path, capsys):
    config = {"readiness": {
        "pilot_mode": False, "overnight_mode": True, "ollama_api_ready": True,
        "ollama_installed": True, "ollama_context_length": 8192,
        "dsh_configured": False, "dsh_config_error": "not configured",
        "free_web_retrieval_ready": True, "web_search_backend": "ddg",
        "docker_daemon_ready": True, "telegram_configured": False,
    }}
    start_platform.print_preflight(tmp_path, config)
    assert "mode:    overnight" in capsys.readouterr().out


def test_sanitized_windows_environment_preserves_profile_location(monkeypatch):
    monkeypatch.setenv("USERPROFILE", "C:/Users/example")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/example/AppData/Local")
    child = safe_env([])
    assert child["USERPROFILE"] == "C:/Users/example"
    assert child["LOCALAPPDATA"].endswith("AppData/Local")
