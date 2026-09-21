"""One-click launcher and supervisor for the local assistant deployment."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from maintenance_daemon import MaintenanceDaemon, atomic_json
from platform_contracts import actor_key
from dsh_integration import discover_dsh, prepare_runtime_patch as prepare_dsh_runtime_patch, web_command as dsh_web_command


SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 5077
LOCAL_TEXT_TASK_TYPES = [
    "general", "operations", "office", "coding", "research", "forecast", "impact",
    "critic", "capability_build", "evaluation", "improvement", "memory_consolidation",
]


def private_data_dir() -> Path:
    configured = os.environ.get("ASSISTANT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "UniversalAssistant").resolve()
    return (Path.home() / ".local" / "share" / "universal-assistant").resolve()


def load_or_create_settings(data_dir: Path) -> dict:
    path = data_dir / "deployment.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain a JSON object")
        return value
    value = {
        "bind_host": "127.0.0.1",
        "port": DEFAULT_PORT,
        "open_browser": True,
        "workspace": str(data_dir / "workspace"),
        "providers_file": str(data_dir / "providers.json"),
        "services": {
            "assistant_worker": True,
            "approval_notifications": True,
            "system_monitor": True,
            "memory_consolidation": True,
            "local_model": True,
            "dsh_web": True,
        },
        "compute": {
            "model_max_concurrency": 1,
            "ollama_num_parallel": 1,
            "ollama_max_loaded_models": 1,
            "ollama_keep_alive": "2m",
            "ollama_context_length": 8192,
        },
        "loops": {
            "assistant_poll_seconds": 5,
            "approval_notification_seconds": 5,
            "system_monitor_seconds": 15,
            "memory_consolidation_seconds": 21600,
            "discovery_seconds": 1800,
            "overnight_report_seconds": 300,
        },
        "dsh": {"model": "", "context_window": 8192, "reasoning_effort": "default",
                "web_port": 3080, "open_browser_on_start": True},
    }
    atomic_json(path, value)
    return value


def configure_paths(data_dir: Path, settings: dict) -> dict:
    """Interactively select persistent, existing owner-controlled work paths."""
    current_workspace = Path(settings.get("workspace") or data_dir / "workspace").expanduser().resolve()
    current_vault = Path(settings.get("obsidian_vault") or current_workspace / "obsidian").expanduser().resolve()
    print("\nChoose the folders this deployment may use. Press Enter to keep the current value.")
    print(f"  repository/workspace: {current_workspace}")
    repo_text = input("Repository to edit: ").strip().strip('"')
    print(f"  Obsidian vault:       {current_vault}")
    vault_text = input("Obsidian vault folder: ").strip().strip('"')
    repository = Path(repo_text).expanduser().resolve() if repo_text else current_workspace
    vault = Path(vault_text).expanduser().resolve() if vault_text else current_vault
    if repo_text and (not repository.is_dir() or not (repository / ".git").exists()):
        raise ValueError(f"Repository must be an existing Git checkout containing .git: {repository}")
    if vault_text and not vault.is_dir():
        raise ValueError(f"Obsidian vault must be an existing folder: {vault}")
    settings = dict(settings)
    settings.update({"workspace": str(repository), "research_repo": str(repository), "obsidian_vault": str(vault)})
    atomic_json(data_dir / "deployment.json", settings)
    print("  saved: paths will be used for this and future launches")
    return settings


def _new_password() -> str:
    supplied = os.environ.get("ASSISTANT_DASHBOARD_PASSWORD")
    if supplied:
        return supplied
    if not sys.stdin.isatty():
        raise RuntimeError("first launch needs an interactive console to choose the dashboard password")
    print("\nFirst launch: choose a dashboard password (12+ characters).")
    while True:
        first = getpass.getpass("Dashboard password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            print("Passwords did not match.")
        elif len(first) < 12 or first.lower() == "changeme":
            print("Use at least 12 characters and do not use 'changeme'.")
        else:
            return first


def load_or_create_secrets(data_dir: Path) -> dict:
    path = data_dir / "managed-secrets.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        values = {name: record.get("value") for name, record in raw.items() if isinstance(record, dict)}
        required = {"CORE_PASSWORD", "CORE_API_KEY", "CORE_SECRET"}
        if not required.issubset(values) or not all(isinstance(values[name], str) and values[name] for name in required):
            raise ValueError(f"{path} is missing a required secret")
        return raw
    now = time.time()
    value = {
        "CORE_PASSWORD": {"value": _new_password(), "rotated_at": now},
        "CORE_API_KEY": {"value": secrets.token_urlsafe(48), "rotated_at": now},
        "CORE_SECRET": {"value": secrets.token_urlsafe(48), "rotated_at": now},
    }
    atomic_json(path, value, private=True)
    return value


def _ollama_inventory() -> list[dict]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            models = json.loads(response.read().decode("utf-8")).get("models", [])
    except Exception:
        return []
    result = []
    for model in models:
        name = str(model.get("name") or model.get("model") or "").strip()
        if not name:
            continue
        capabilities = []
        try:
            payload = json.dumps({"name": name}).encode("utf-8")
            request = urllib.request.Request("http://127.0.0.1:11434/api/show", data=payload,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                capabilities = json.loads(response.read().decode("utf-8")).get("capabilities", []) or []
        except Exception:
            pass
        result.append({"name": name, "size": int(model.get("size") or 0),
                       "capabilities": [str(item) for item in capabilities]})
    return sorted(result, key=lambda item: ("tools" not in item["capabilities"], item["size"], item["name"]))


def _pilot_provider_config(models: list[dict]) -> dict | None:
    if not models:
        return None
    primary = sorted(models, key=lambda item: (
        "tools" not in item.get("capabilities", []), int(item.get("size") or 0), str(item.get("name") or "")
    ))[0]
    supports_tools = "tools" in primary["capabilities"]
    provider = {
        "name": "ollama-pilot",
        "type": "api",
        "cost_class": "local",
        "open_source": True,
        "base_url": "http://127.0.0.1:11434/v1",
        "model": primary["name"],
        "api_key_env": "LOCAL_MODEL_API_KEY",
        "priority": 0,
        "max_tokens": 2048,
        "supports_tools": supports_tools,
        # Reserve the small output budget for an answer/tool call. DSH keeps
        # its separate reasoning setting in the generated Cordis layer.
        "reasoning_effort": "none",
        # Prefer Ollama's native tool-call envelope when the model advertises it.
        # The JSON compatibility prompt remains available for models without it.
        "structured_text": not supports_tools,
        "task_types": LOCAL_TEXT_TASK_TYPES,
    }
    return {
        "routing": {"free_first": True, "allow_paid": False, "max_concurrency": 1,
                    "daily_paid_budget_usd": 0, "monthly_paid_budget_usd": 0,
                    "estimated_output_tokens": 2048},
        "providers": [provider],
    }


def ensure_provider_config(data_dir: Path, settings: dict, *, pilot=False) -> Path:
    target = Path(settings.get("providers_file") or data_dir / "providers.json").expanduser().resolve()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        detected = _pilot_provider_config(_ollama_inventory()) if pilot else None
        if detected:
            atomic_json(target, detected, private=True)
        else:
            shutil.copyfile(SOURCE_DIR / "examples" / "providers-free-first.json", target)
    # Migrate older locally generated Ollama routes. Thinking is useful in DSH,
    # but the Universal worker's 2K response budget must reach content/tool_calls.
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        changed = False
        providers = document.get("providers", [])
        browser_free = [provider for provider in providers if provider.get("type", "api") != "playwright"]
        if len(browser_free) != len(providers):
            document["providers"] = browser_free
            changed = True
        for provider in browser_free:
            base_url = str(provider.get("base_url") or "").lower()
            if (provider.get("type", "api") == "api" and provider.get("cost_class") == "local"
                    and ("127.0.0.1:11434" in base_url or "localhost:11434" in base_url)):
                if "reasoning_effort" not in provider:
                    provider["reasoning_effort"] = "none"
                    changed = True
                task_types = provider.get("task_types")
                if isinstance(task_types, list):
                    expanded = list(dict.fromkeys([*task_types, *LOCAL_TEXT_TASK_TYPES]))
                    if expanded != task_types:
                        provider["task_types"] = expanded
                        changed = True
        if changed:
            atomic_json(target, document, private=True)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return target


def _telegram_candidates(token: str) -> list[dict]:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    found = {}
    for update in body.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        ident = str(chat.get("id") or "")
        if ident:
            label = chat.get("username") or " ".join(
                part for part in (str(chat.get("first_name") or ""), str(chat.get("last_name") or "")) if part
            ) or str(chat.get("type") or "chat")
            found[ident] = {"id": ident, "label": label}
    return list(found.values())


def _telegram_token_valid(token: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def configure_telegram(data_dir: Path, records: dict) -> dict:
    if records.get("TELEGRAM_BOT_TOKEN", {}).get("value") and records.get("ALLOWED_CHAT_IDS", {}).get("value"):
        return records
    if not sys.stdin.isatty():
        print("  Telegram: skipped; interactive setup is required")
        return records
    print("\nPilot Telegram setup (optional). The token is stored privately and is not echoed.")
    token = getpass.getpass("Telegram bot token (leave blank to skip): ").strip()
    if not token:
        return records
    if not _telegram_token_valid(token):
        print("  Telegram: token validation failed; nothing was stored")
        return records
    input("Send /start to the bot from your private Telegram account, then press Enter here. ")
    candidates = _telegram_candidates(token)
    if candidates:
        print("Recent Telegram chats:")
        for candidate in candidates:
            print(f"  {candidate['id']}  {candidate['label']}")
    allowed = input("Exact private chat ID to allow (leave blank to skip Telegram): ").strip()
    if not re.fullmatch(r"-?\d+", allowed):
        print("  Telegram: not enabled; an exact numeric chat ID is required")
        return records
    now = time.time()
    records = dict(records)
    records["TELEGRAM_BOT_TOKEN"] = {"value": token, "rotated_at": now}
    records["ALLOWED_CHAT_IDS"] = {"value": allowed, "rotated_at": now}
    atomic_json(data_dir / "managed-secrets.json", records, private=True)
    print("  Telegram: configured for one allowlisted chat")
    return records


def configure_alpaca(data_dir: Path, records: dict) -> dict:
    """Store Alpaca data credentials without echoing or placing them in source."""
    print("\nAlpaca market-data setup. Use newly generated keys if any key was ever committed to source.")
    key = getpass.getpass("Alpaca API key ID (leave blank to keep current): ").strip()
    if not key:
        configured = bool((records.get("APCA_API_KEY_ID") or {}).get("value") and
                          (records.get("APCA_API_SECRET_KEY") or {}).get("value"))
        print("  Alpaca: existing credentials kept" if configured else "  Alpaca: not configured")
        return records
    secret = getpass.getpass("Alpaca API secret key: ").strip()
    if not secret:
        raise ValueError("an Alpaca secret is required when changing the key ID")
    now = time.time()
    records = dict(records)
    records["APCA_API_KEY_ID"] = {"value": key, "rotated_at": now}
    records["APCA_API_SECRET_KEY"] = {"value": secret, "rotated_at": now}
    atomic_json(data_dir / "managed-secrets.json", records, private=True)
    print("  Alpaca: credentials stored privately; restart required")
    return records


def apply_environment(data_dir: Path, settings: dict, secret_records: dict, providers: Path,
                      *, executor_only: bool = False) -> dict:
    workspace = Path(settings.get("workspace") or data_dir / "workspace").expanduser().resolve()
    obsidian_vault = Path(settings.get("obsidian_vault") or workspace / "obsidian").expanduser().resolve()
    research_repo = Path(settings.get("research_repo") or workspace / "research_repo").expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    obsidian_vault.mkdir(parents=True, exist_ok=True)
    research_repo.mkdir(parents=True, exist_ok=True)
    port = int(settings.get("port", DEFAULT_PORT))
    bind_host = str(settings.get("bind_host") or "127.0.0.1")
    public_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    compute = settings.get("compute") or {}
    model_concurrency = max(1, min(int(compute.get("model_max_concurrency", 1)), 32))
    ollama_parallel = max(1, min(int(compute.get("ollama_num_parallel", 1)), 32))
    ollama_loaded = max(1, min(int(compute.get("ollama_max_loaded_models", 1)), 32))
    dsh_options = settings.get("dsh") or {}
    ollama_context = max(2048, min(int(compute.get("ollama_context_length", dsh_options.get("context_window", 8192))), 262144))
    dsh_runtime = None
    dsh_error = None
    try:
        dsh_runtime = prepare_dsh_runtime_patch(data_dir, providers, settings)
    except Exception as exc:
        dsh_error = f"{type(exc).__name__}: {exc}"[:2000]
    values = {
        "CORE_ROOT": str(workspace),
        "CORE_DB": str(data_dir / "core.db"),
        "CORE_BIND": bind_host,
        "CORE_PORT": str(port),
        "CORE_URL": f"http://{public_host}:{port}",
        "MODEL_PROVIDERS_FILE": str(providers),
        "MAINTENANCE_STATUS_FILE": str(data_dir / "maintenance" / "status.json"),
        "APPROVAL_NOTIFIER_STATE_FILE": str(data_dir / "maintenance" / "approval-notifications.json"),
        "OBSIDIAN_VAULT": str(obsidian_vault),
        "RESEARCH_REPO": str(research_repo),
        "EXECUTOR_ONLY": "1" if executor_only else "0",
        "EMBEDDING_BASE_URL": os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1"),
        "EMBEDDING_MODEL": os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
        "EMBEDDING_API_KEY": os.environ.get("EMBEDDING_API_KEY", "local"),
        "MODEL_MAX_CONCURRENCY": str(model_concurrency),
        "OLLAMA_NUM_PARALLEL": str(ollama_parallel),
        "OLLAMA_MAX_LOADED_MODELS": str(ollama_loaded),
        "OLLAMA_KEEP_ALIVE": str(compute.get("ollama_keep_alive", "2m")),
        "OLLAMA_CONTEXT_LENGTH": str(ollama_context),
        "LOCAL_MODEL_API_KEY": os.environ.get("LOCAL_MODEL_API_KEY", "local"),
        "OLLAMA_LAUNCH_DSH_API_KEY": os.environ.get("OLLAMA_LAUNCH_DSH_API_KEY", "local"),
        "DSH_TELEMETRY_MODE": "DISABLED",
        # Windows otherwise gives pipe-backed Python workers the active ANSI
        # code page. Model reports regularly contain Unicode punctuation, so
        # make the supervised logging contract explicitly UTF-8.
        "PYTHONIOENCODING": "utf-8:replace",
        "TELEGRAM_API_KEY": actor_key(secret_records["CORE_API_KEY"]["value"], "telegram"),
    }
    if dsh_runtime and dsh_runtime.get("configured"):
        values["DSH_RUNTIME_PATCH"] = str(dsh_runtime["runtime_patch"])
        values["DSH_RUNTIME_SETTINGS"] = str(dsh_runtime["settings"])
    if dsh_error:
        values["DSH_CONFIG_ERROR"] = dsh_error
    values.update({name: record["value"] for name, record in secret_records.items()})
    os.environ.update(values)
    return values


def _ollama_executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _free_web_readiness() -> dict:
    """Report the browser-free, no-key web research path used by the core."""
    return {
        "free_web_retrieval_ready": True,
        "web_search_backend": os.environ.get("SEARCH_BACKEND", "ddg"),
        "web_javascript_execution": False,
    }


def build_maintenance_config(data_dir: Path, settings: dict, *, pilot=False, overnight=False, trading=False) -> dict:
    python = str(Path(sys.executable).resolve())
    source = str(SOURCE_DIR)
    common = [
        "CORE_ROOT", "CORE_DB", "CORE_BIND", "CORE_PORT", "CORE_URL", "CORE_PASSWORD",
        "CORE_API_KEY", "CORE_SECRET", "MODEL_PROVIDERS_FILE", "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL", "EMBEDDING_API_KEY", "MAINTENANCE_STATUS_FILE", "APPROVAL_NOTIFIER_STATE_FILE",
        "OBSIDIAN_VAULT", "RESEARCH_REPO", "EXECUTOR_ONLY", "MODEL_MAX_CONCURRENCY",
        "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "BRAVE_API_KEY",
        "LOCAL_MODEL_API_KEY", "DSH_RUNTIME_PATCH", "DSH_RUNTIME_SETTINGS", "DSH_CONFIG_ERROR",
        "DSH_TELEMETRY_MODE",
        "PYTHONIOENCODING", "PATH", "SYSTEMROOT", "TEMP", "TMP",
    ]
    common += sorted(name for name in os.environ if name.startswith("INTEGRATION_"))
    enabled = settings.get("services") or {}
    loops = settings.get("loops") or {}
    assistant_poll = max(1, min(int(loops.get("assistant_poll_seconds", 5)), 60))
    approval_poll = max(2, min(int(loops.get("approval_notification_seconds", 5)), 300))
    system_monitor_interval = max(2, min(int(loops.get("system_monitor_seconds", 15)), 3600))
    consolidation_interval = max(900, min(int(loops.get("memory_consolidation_seconds", 21600)), 604800))
    discovery_interval = max(60, min(int(loops.get("discovery_seconds", 1800)), 86400))
    report_interval = max(60, min(int(loops.get("overnight_report_seconds", 300)), 3600))
    mcp_options = settings.get("mcp_http") or {}
    mcp_http_port = max(1, min(int(mcp_options.get("port", 5078)), 65535))
    mcp_http_conflict = mcp_http_port == int(os.environ["CORE_PORT"])
    mcp_http_free = not mcp_http_conflict and port_is_free("127.0.0.1", mcp_http_port)
    mcp_http_enabled = bool(enabled.get("mcp_http", True) and mcp_http_free)
    services = [
        {"name": "core", "enabled": True, "command": [python, "core_server.py"], "cwd": source,
         "env_allowlist": common, "health_url": os.environ["CORE_URL"] + "/api/system/health",
         "health_headers_env": {"X-API-Key": "CORE_API_KEY"}, "health_grace_seconds": 15,
         "health_failure_limit": 3, "log_max_bytes": 5_000_000, "log_keep": 5},
        {"name": "assistant-worker", "enabled": False if trading else enabled.get("assistant_worker", True),
         "command": [python, "assistant_worker.py", "--interval", str(assistant_poll)], "cwd": source,
         "env_allowlist": common + ["ASSISTANT_RESOURCE_PAUSE_LEVEL"], "log_max_bytes": 5_000_000, "log_keep": 5},
        {"name": "mcp-http", "enabled": mcp_http_enabled,
         "command": [python, "mcp_http_bridge.py", "--host", "127.0.0.1", "--port", str(mcp_http_port)],
         "cwd": source, "env_allowlist": common + ["MCP_AGENT_KEY", "MCP_HTTP_TOKEN"],
         "log_max_bytes": 2_000_000, "log_keep": 3},
        {"name": "approval-notifier", "enabled": bool(os.name == "nt" and enabled.get("approval_notifications", True)),
         "command": [python, "approval_notifier.py", "--interval", str(approval_poll)], "cwd": source,
         "env_allowlist": common + ["APPROVAL_NOTIFIER_STATE_FILE"], "log_max_bytes": 2_000_000, "log_keep": 3},
        {"name": "system-monitor", "enabled": False if pilot else enabled.get("system_monitor", True),
         "command": [python, "system_monitor.py", "--watch", "--interval", str(system_monitor_interval)], "cwd": source,
         "env_allowlist": common + ["SYSTEM_MONITOR_INTERVAL", "GPU_WARN_C", "GPU_CRIT_C", "RAM_WARN_PCT", "DISK_WARN_PCT"],
         "log_max_bytes": 5_000_000, "log_keep": 3},
        {"name": "memory-consolidation", "enabled": False if pilot or trading else enabled.get("memory_consolidation", True),
         "command": [python, "memory_consolidation_worker.py", "--interval", str(consolidation_interval)], "cwd": source,
         "env_allowlist": common + ["CONSOLIDATION_RESOURCE_PAUSE_LEVEL"], "log_max_bytes": 5_000_000, "log_keep": 3},
        {"name": "discovery", "enabled": bool(overnight and not trading),
         "command": [python, "discovery_worker.py", "--interval", str(discovery_interval), "--focus",
                     str((settings.get("overnight") or {}).get(
                         "discovery_focus", "Improve the Universal Harness using local evidence, tests, and falsifiable hypotheses"))],
         "cwd": source,
         "env_allowlist": common + ["DISCOVERY_MAX_TOOL_ITERS", "DISCOVERY_MAX_PARALLEL_READS",
                                     "DISCOVERY_MAX_TOOL_RESULT_CHARS", "DISCOVERY_EMPTY_RESPONSE_RETRIES",
                                     "DISCOVERY_RESOURCE_PAUSE_LEVEL", "DISCOVERY_ALLOW_RESEARCH_WRITES"],
         "log_max_bytes": 5_000_000, "log_keep": 5},
        {"name": "overnight-report", "enabled": True,
         "command": [python, "overnight_report_worker.py", "--interval", str(report_interval)], "cwd": source,
         "env_allowlist": common + ["OVERNIGHT_REPORT_FILE"],
         "log_max_bytes": 2_000_000, "log_keep": 3},
    ]
    telegram_enabled = enabled.get("telegram", pilot) and bool(
        os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("ALLOWED_CHAT_IDS"))
    if telegram_enabled:
        services.append({"name": "telegram", "enabled": True, "command": [python, "telegram_agent.py"], "cwd": source,
                         "env_allowlist": ["CORE_URL", "TELEGRAM_API_KEY", "TELEGRAM_BOT_TOKEN", "ALLOWED_CHAT_IDS",
                                           "PYTHONIOENCODING"],
                         "log_max_bytes": 5_000_000, "log_keep": 3})
    ollama = _ollama_executable()
    ollama_api_ready = bool(_ollama_inventory())
    docker = shutil.which("docker")
    docker_ready = False
    if docker:
        try:
            docker_ready = subprocess.run([docker, "info"], capture_output=True, timeout=8).returncode == 0
        except Exception:
            pass
    if not trading and enabled.get("local_model", True) and ollama and not ollama_api_ready:
        services.append({"name": "local-model", "enabled": True, "command": [ollama, "serve"],
                         "cwd": str(data_dir), "env_allowlist": ["PATH", "OLLAMA_HOST", "OLLAMA_MODELS",
                                                                    "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS",
                                                                    "OLLAMA_KEEP_ALIVE", "OLLAMA_CONTEXT_LENGTH"],
                         "log_max_bytes": 5_000_000, "log_keep": 3})
    dsh = discover_dsh()
    dsh_options = settings.get("dsh") or {}
    dsh_port = max(1, min(int(dsh_options.get("web_port", 3080)), 65535))
    dsh_port_conflict = dsh_port == int(os.environ["CORE_PORT"])
    dsh_port_free = not dsh_port_conflict and port_is_free("127.0.0.1", dsh_port)
    dsh_enabled = bool(not trading and enabled.get("dsh_web", True) and dsh.get("available") and dsh_port_free
                       and not os.environ.get("DSH_CONFIG_ERROR"))
    if dsh_enabled:
        services.append({
            "name": "dsh-web", "enabled": True,
            "command": dsh_web_command(host="127.0.0.1", port=dsh_port,
                                       no_open=not bool(dsh_options.get("open_browser_on_start", True))),
            "cwd": os.environ["CORE_ROOT"],
            "env_allowlist": ["PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                              "DSH_HOME", "OLLAMA_LAUNCH_DSH_API_KEY", "DSH_TELEMETRY_MODE"],
            "log_max_bytes": 5_000_000, "log_keep": 3,
        })
    web = _free_web_readiness()
    return {
        "state_dir": str(data_dir / "maintenance"),
        "secret_file": str(data_dir / "managed-secrets.json"),
        "database_env": "CORE_DB",
        "interval_seconds": 15,
        "backup_interval_seconds": 21600,
        "backup_keep": 14,
        "dependency_check_interval_seconds": 86400,
        "check_outdated_dependencies": False,
        "readiness": {
            "ollama_installed": bool(ollama),
            "ollama_api_ready": ollama_api_ready,
            "docker_installed": bool(docker),
            "docker_daemon_ready": docker_ready,
            "telegram_configured": telegram_enabled,
            "dsh_installed": bool(dsh.get("available")),
            "dsh_version": dsh.get("version"),
            "dsh_model": dsh.get("model"),
            "dsh_context_window": dsh.get("context_window"),
            "dsh_configured": bool(dsh.get("runtime_patch") and not os.environ.get("DSH_CONFIG_ERROR")),
            "dsh_config_error": os.environ.get("DSH_CONFIG_ERROR") or ("DSH web port conflicts with the core port" if dsh_port_conflict else None),
            "dsh_web_url": f"http://127.0.0.1:{dsh_port}/" if dsh.get("available") and not dsh_port_conflict else None,
            "dsh_web_external": bool(dsh.get("available") and not dsh_port_free and not dsh_port_conflict),
            "mcp_stdio_ready": True,
            "mcp_http_ready": mcp_http_enabled,
            "mcp_http_url": f"http://127.0.0.1:{mcp_http_port}/mcp" if mcp_http_enabled else None,
            "mcp_http_error": ("MCP HTTP port conflicts with the core port" if mcp_http_conflict else
                               "MCP HTTP port is already in use" if not mcp_http_free else None),
            "ollama_context_length": int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "8192")),
            **web,
            "pilot_mode": pilot,
            "overnight_mode": overnight,
            "trading_research_mode": trading,
            "executor_only": trading,
            "local_reasoning_enabled": not trading,
        },
        "backup_directories": [
            {"name": "obsidian", "path_env": "OBSIDIAN_VAULT", "keep": 7, "max_bytes": 2_000_000_000},
            {"name": "research", "path_env": "RESEARCH_REPO", "keep": 7, "max_bytes": 2_000_000_000},
        ],
        "secrets": [
            {"name": "CORE_PASSWORD", "required": True, "max_age_days": 180, "auto_rotate": False},
            {"name": "CORE_API_KEY", "required": True, "max_age_days": 90, "auto_rotate": False},
            {"name": "CORE_SECRET", "required": True, "max_age_days": 180, "auto_rotate": False},
        ],
        "services": services,
    }


def port_is_free(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((probe_host, port))
            return True
        except OSError:
            return False


def core_is_ready(url: str, api_key: str) -> bool:
    try:
        req = urllib.request.Request(url + "/api/system/health", headers={"X-API-Key": api_key})
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def print_preflight(data_dir: Path, config: dict) -> None:
    print("\nUniversal Assistant")
    print(f"  source:  {SOURCE_DIR}")
    print(f"  data:    {data_dir}")
    print(f"  Python:  {sys.version.split()[0]}")
    ready = config["readiness"]
    mode = "trading-research" if ready.get("trading_research_mode") else "pilot" if ready.get("pilot_mode") else "overnight" if ready.get("overnight_mode") else "full"
    print(f"  mode:    {mode}")
    if ready.get("executor_only"):
        print("  reasoning: off locally (deterministic executor only)")
    print(f"  Ollama:  {'API ready' if ready.get('ollama_api_ready') else 'available' if ready['ollama_installed'] else 'not installed (model jobs unavailable)'}")
    print(f"  context: {ready.get('ollama_context_length', '?')} tokens requested (restart an external Ollama server to apply)")
    dsh_state = (f"ready, {ready.get('dsh_model')} @ {ready.get('dsh_context_window')} context"
                 if ready.get("dsh_configured") else ready.get("dsh_config_error") or "not installed")
    print(f"  DSH:     {dsh_state}")
    print(f"  Web:     {'ready' if ready.get('free_web_retrieval_ready') else 'unavailable'}; "
          f"{ready.get('web_search_backend', 'ddg')} search + static HTTPS reading (no browser automation)")
    print(f"  Docker:  {'ready' if ready['docker_daemon_ready'] else 'not ready (sandbox tests unavailable)'}")
    print(f"  MCP:     stdio ready; HTTP {ready.get('mcp_http_url') or ready.get('mcp_http_error') or 'disabled'}")
    print(f"  Telegram:{' configured (scoped credential)' if ready.get('telegram_configured') else ' off'}")
    print(f"  repo:    {os.environ.get('CORE_ROOT', 'not configured')}")
    print(f"  vault:   {os.environ.get('OBSIDIAN_VAULT', 'not configured')}")


def open_command_center(url: str) -> bool:
    """Open the UI with the native Windows URL handler, then fall back."""
    if os.name == "nt" and hasattr(os, "startfile"):
        try:
            os.startfile(url)
            return True
        except OSError:
            pass
    return bool(webbrowser.open(url))


def wait_and_open(url: str, api_key: str, stop: threading.Event) -> None:
    for _ in range(60):
        if stop.wait(0.5):
            return
        if core_is_ready(url, api_key):
            opened = open_command_center(url)
            print(f"\nDashboard {'opened' if opened else 'ready'}: {url}")
            return
    print(f"\nCore did not become ready. Check the logs under {private_data_dir() / 'maintenance' / 'logs'}")


def enable_overnight_coding(url: str, api_key: str, stop: threading.Event) -> None:
    """Install one idempotent, isolated-copy coding schedule after core is ready."""
    for _ in range(120):
        if stop.wait(0.5):
            return
        if core_is_ready(url, api_key):
            break
    else:
        print("  overnight: coding schedule not installed because core never became ready")
        return
    schedule = {
        "name": "isolated-self-improvement",
        "objective": (
            "Read operator context and active goals. Scan the configured source, create an isolated source copy, "
            "fix the highest-value reproducible bug or unfinished feature only in that copy, run Docker tests, "
            "and export a reviewed patch artifact with a one-paragraph summary. Never modify or promote the "
            "authoritative source."
        ),
        "interval_seconds": 86400,
        "enabled": True,
        "payload": {
            "self_improvement": True, "self_improvement_source": "", "template": "coding",
            "deliver_as": "patch", "critic_review": True, "approval_mode": "auto_edit",
            "max_steps": 40, "max_seconds": 7200, "max_parallel_reads": 2,
        },
    }
    request = urllib.request.Request(
        url + "/api/owner/schedules", data=json.dumps(schedule).encode("utf-8"), method="POST",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
        print("  overnight: isolated self-improvement schedule enabled (reviewable copies only)")
    except Exception as exc:
        print(f"  overnight: coding schedule setup failed: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true", help="Run only the core, one assistant worker, one local route, and optional Telegram")
    parser.add_argument("--setup-telegram", action="store_true", help="Securely configure an allowlisted Telegram bot")
    parser.add_argument("--setup-alpaca", action="store_true", help="Securely configure Alpaca market-data credentials")
    parser.add_argument("--configure-paths", action="store_true", help="Choose and persist the repository workspace and Obsidian vault")
    parser.add_argument("--overnight", action="store_true", help="Supervise conservative autonomous research alongside the normal platform")
    parser.add_argument("--trading", action="store_true", help="Run the lean stock-research profile without generic discovery, memory consolidation, or DSH web")
    parser.add_argument("--open-browser", action="store_true", help="Open the Command Center after the core becomes ready, overriding the saved preference")
    args = parser.parse_args()
    if sum(bool(value) for value in (args.pilot, args.overnight, args.trading)) > 1:
        parser.error("--pilot, --overnight, and --trading are separate operating modes")
    data_dir = private_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = load_or_create_settings(data_dir)
    if args.configure_paths:
        settings = configure_paths(data_dir, settings)
    secrets_file = load_or_create_secrets(data_dir)
    if args.setup_telegram:
        secrets_file = configure_telegram(data_dir, secrets_file)
    if args.setup_alpaca:
        secrets_file = configure_alpaca(data_dir, secrets_file)
    providers = ensure_provider_config(data_dir, settings, pilot=args.pilot)
    env = apply_environment(data_dir, settings, secrets_file, providers, executor_only=args.trading)
    config = build_maintenance_config(data_dir, settings, pilot=args.pilot, overnight=args.overnight, trading=args.trading)
    atomic_json(data_dir / "maintenance.generated.json", config, private=True)
    print_preflight(data_dir, config)

    host, port, url = env["CORE_BIND"], int(env["CORE_PORT"]), env["CORE_URL"]
    open_browser = args.open_browser or (
        settings.get("open_browser", True) and os.environ.get("ASSISTANT_NO_BROWSER") != "1"
    )
    if args.overnight:
        workspace = Path(env["CORE_ROOT"])
        if not (workspace / ".git").exists():
            print("ERROR: overnight coding requires workspace to be an existing Git repository.")
            print("Run START_PILOT.cmd configure and select the source repository, then try again.")
            return 2
        if not config["readiness"].get("docker_daemon_ready"):
            print("ERROR: overnight coding requires the Docker sandbox to be running.")
            return 2
    if not port_is_free(host, port):
        if core_is_ready(url, env["CORE_API_KEY"]):
            if args.overnight:
                print("ERROR: the regular platform is already running. Stop it before starting overnight mode.")
                return 2
            print("  status:  already running")
            if open_browser:
                open_command_center(url)
            return 0
        print(f"ERROR: {host}:{port} is already in use by another process.")
        return 2

    print("  status:  starting supervised services")
    print("  stop:    close this window or press Ctrl+C\n")
    daemon = MaintenanceDaemon(config)
    stop = threading.Event()

    def request_stop(*_args):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    if open_browser:
        threading.Thread(target=wait_and_open, args=(url, env["CORE_API_KEY"], stop), daemon=True).start()
    if args.overnight:
        threading.Thread(target=enable_overnight_coding,
                         args=(url, env["CORE_API_KEY"], stop), daemon=True).start()
    try:
        while not stop.is_set():
            status = daemon.cycle()
            summary = ", ".join(f"{item['name']}={item['status']}" for item in status["services"])
            print(time.strftime("%H:%M:%S"), summary, flush=True)
            stop.wait(max(5, int(config["interval_seconds"])))
    finally:
        print("Stopping supervised services...")
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
