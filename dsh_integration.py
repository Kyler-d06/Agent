"""Bounded bridge to the owner-installed DeepSeek Harness.

DSH remains a separate upstream runtime. This adapter invokes only its documented
profile launcher with fixed argv, a configured workspace, conservative permission
mode, bounded output, cancellation, and durable event callbacks. It never shells
through model-supplied text.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import yaml

from platform_contracts import safe_env


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAX_STREAM_CHARS = 2_000_000
MAX_LINE_CHARS = 32_000


def _read_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"DSH settings must contain an object: {path}")
    return value


def _model_metadata(settings: dict) -> dict:
    default = settings.get("agent-default-model") or {}
    provider = str(default.get("provider") or "") or None
    model = str(default.get("model") or "") or None
    context_window = None
    models = (((settings.get("llm-pi-ai") or {}).get("providers") or {}).get("ollama") or {}).get("models") or []
    for candidate in models:
        if isinstance(candidate, dict) and candidate.get("id") == model:
            try:
                context_window = int(candidate.get("contextWindow"))
            except (TypeError, ValueError):
                pass
            break
    return {"provider": provider, "model": model, "context_window": context_window}


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def discover_dsh() -> dict:
    """Return non-secret installation metadata without starting DSH."""
    node = shutil.which("node")
    candidates = []
    if os.environ.get("DSH_BIN"):
        candidates.append(Path(os.environ["DSH_BIN"]))
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js")
    dsh_bin = next((path.expanduser().resolve() for path in candidates if path.is_file()), None)
    patch_candidates = []
    if os.environ.get("DSH_OLLAMA_PATCH"):
        patch_candidates.append(Path(os.environ["DSH_OLLAMA_PATCH"]))
    if os.environ.get("USERPROFILE"):
        patch_candidates.append(Path(os.environ["USERPROFILE"]) / ".ollama" / "launch" / "dsh" / "ollama.cordis.yml")
    ollama_patch = next((path.expanduser().resolve() for path in patch_candidates if path.is_file()), None)
    settings_candidates = []
    if os.environ.get("DSH_RUNTIME_SETTINGS"):
        settings_candidates.append(Path(os.environ["DSH_RUNTIME_SETTINGS"]))
    if os.environ.get("DSH_OLLAMA_SETTINGS"):
        settings_candidates.append(Path(os.environ["DSH_OLLAMA_SETTINGS"]))
    if ollama_patch:
        settings_candidates.append(ollama_patch.parent / "settings.yaml")
    settings_path = next((path.expanduser().resolve() for path in settings_candidates if path.is_file()), None)
    metadata = {"provider": None, "model": None, "context_window": None}
    settings_error = None
    if settings_path:
        try:
            metadata = _model_metadata(_read_yaml(settings_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            settings_error = f"{type(exc).__name__}: {exc}"
    version = None
    if dsh_bin:
        try:
            package = json.loads((dsh_bin.parents[1] / "package.json").read_text(encoding="utf-8"))
            version = str(package.get("version") or "") or None
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return {
        "available": bool(node and dsh_bin and ollama_patch),
        "node": node,
        "bin": str(dsh_bin) if dsh_bin else None,
        "ollama_patch": str(ollama_patch) if ollama_patch else None,
        "runtime_patch": os.environ.get("DSH_RUNTIME_PATCH"),
        "settings": str(settings_path) if settings_path else None,
        "version": version,
        "settings_error": settings_error,
        **metadata,
    }


def prepare_runtime_patch(data_dir, provider_file=None, settings=None) -> dict:
    """Create a private final settings layer with one explicit model/context."""
    found = discover_dsh()
    if not found["available"]:
        return {**found, "configured": False}
    source_settings = Path(found["settings"] or "")
    if not source_settings.is_file():
        return {**found, "configured": False, "settings_error": "Ollama DSH settings file is unavailable"}
    document = _read_yaml(source_settings)
    options = (settings or {}).get("dsh") or {}
    desired_model = str(options.get("model") or "").strip()
    if not desired_model and provider_file:
        try:
            provider_document = json.loads(Path(provider_file).read_text(encoding="utf-8"))
            for provider in provider_document.get("providers", []):
                if (provider.get("enabled", True) and provider.get("type", "api") == "api"
                        and provider.get("cost_class", "local") == "local"
                        and provider.get("model")):
                    desired_model = str(provider["model"])
                    break
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    desired_model = desired_model or found.get("model")
    try:
        context_window = int(options.get("context_window", 8192))
    except (TypeError, ValueError) as exc:
        raise ValueError("dsh.context_window must be an integer") from exc
    if not 2048 <= context_window <= 262144:
        raise ValueError("dsh.context_window must be between 2048 and 262144")
    if not desired_model:
        raise ValueError("DSH has no configured Ollama model")
    models = (((document.get("llm-pi-ai") or {}).get("providers") or {}).get("ollama") or {}).get("models") or []
    selected = next((item for item in models if isinstance(item, dict) and item.get("id") == desired_model), None)
    if selected is None:
        available = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
        raise ValueError(f"DSH model {desired_model!r} is absent from the Ollama launch settings; available={available}")
    selected["contextWindow"] = context_window
    reasoning_effort = str(options.get("reasoning_effort", "default") or "default")
    if reasoning_effort not in {"default", "none", "low", "medium", "high"}:
        raise ValueError("dsh.reasoning_effort must be default, none, low, medium, or high")
    selection = {"provider": "ollama", "model": desired_model}
    # Missing model metadata means DSH owns the default. Writing the literal
    # string "none" is not equivalent to omission and is rejected by models
    # (including the Ollama-generated qwen3.5:4b entry) that expose no effort IDs.
    if reasoning_effort != "default":
        selection["reasoningEffort"] = reasoning_effort
    document["agent-default-model"] = selection
    if isinstance(document.get("web-search-deepseek"), dict):
        document["web-search-deepseek"]["model"] = desired_model
    root = Path(data_dir).expanduser().resolve() / "dsh"
    managed_settings = root / "settings.yaml"
    runtime_patch = root / "runtime.cordis.yml"
    _atomic_text(managed_settings, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    _atomic_text(runtime_patch, yaml.safe_dump([
        {"id": "settings", "config": {"path": str(managed_settings)}}
    ], sort_keys=False, allow_unicode=True))
    os.environ["DSH_RUNTIME_SETTINGS"] = str(managed_settings)
    os.environ["DSH_RUNTIME_PATCH"] = str(runtime_patch)
    return {**discover_dsh(), "configured": True, "model": desired_model,
            "provider": "ollama", "context_window": context_window,
            "reasoning_effort": reasoning_effort}


def _command(profile: str, *args: str) -> list[str]:
    found = discover_dsh()
    if not found["available"]:
        raise RuntimeError("DSH, Node.js, or the Ollama DSH configuration is unavailable")
    command = [found["node"], found["bin"], "--profile", profile, "--patch", found["ollama_patch"]]
    runtime_patch = found.get("runtime_patch")
    if runtime_patch:
        runtime_path = Path(runtime_patch).expanduser().resolve()
        if not runtime_path.is_file():
            raise RuntimeError(f"configured DSH runtime patch is missing: {runtime_path}")
        command.extend(["--patch", str(runtime_path)])
    return [*command, *map(str, args)]


def web_command(*, host="127.0.0.1", port=3080, no_open=True) -> list[str]:
    port = max(1, min(int(port), 65535))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("integrated DSH Web must remain loopback-only")
    args = ["--host", host, "--port", str(port)]
    if no_open:
        args.append("--no-open")
    return _command("web", *args)


def headless_command(objective: str) -> list[str]:
    objective = str(objective or "").strip()
    if not objective or len(objective) > 200_000:
        raise ValueError("DSH objective must contain 1 to 200000 characters")
    return _command("headless", objective)


def _git(root: Path, *args: str, timeout=20) -> str:
    result = subprocess.run(["git", *args], cwd=root, env=safe_env(), shell=False,
                            capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "git command failed")[-2000:])
    return result.stdout.strip()


def repository_snapshot(workspace) -> dict:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError(f"DSH workspace must be an existing Git checkout: {root}")
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise ValueError(f"configured workspace must be the Git root ({top})")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    try:
        head = _git(root, "rev-parse", "--verify", "HEAD")
    except RuntimeError:
        head = None
    return {"workspace": str(root), "head": head,
            "dirty": bool(status), "status": status[-100_000:]}


def isolated_clean_checkout(workspace) -> dict:
    """Snapshot a dirty checkout into a clean, disposable Git repository for DSH."""
    source = Path(workspace).expanduser().resolve()
    if not source.is_dir() or not (source / ".git").exists():
        raise ValueError(f"DSH workspace must be an existing Git checkout: {source}")
    copies = source / "self_improvement_copies"
    copies.mkdir(parents=True, exist_ok=True)
    destination = copies / (time.strftime("%Y%m%d-%H%M%S") + "-dsh-" + uuid.uuid4().hex[:8])
    ignored = shutil.ignore_patterns(
        ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
        "self_improvement_copies", "worktrees", ".mypy_cache", ".ruff_cache",
    )
    shutil.copytree(source, destination, ignore=ignored, symlinks=False)
    _git(destination, "init", timeout=60)
    _git(destination, "config", "core.autocrlf", "false")
    _git(destination, "add", "--all", timeout=120)
    _git(destination, "-c", "user.name=Universal Assistant", "-c",
         "user.email=local@universal-assistant.invalid", "commit", "-m", "Isolated DSH baseline", timeout=120)
    snapshot = repository_snapshot(destination)
    if snapshot["dirty"]:
        raise RuntimeError("isolated DSH baseline was not clean after creation")
    return {**snapshot, "source_workspace": str(source), "isolated": True}


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.kill()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def _session_log_snapshot() -> dict[str, tuple[int, int]]:
    """Index DSH's own durable logs without opening transcript content."""
    home = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).expanduser().resolve()
    root = home / "sessions"
    if not root.is_dir():
        return {}
    found = {}
    for path in root.glob("**/session.v3.jsonl.zstd"):
        try:
            stat = path.stat()
            found[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return found


def run_headless(objective: str, workspace, *, timeout_seconds=3600,
                 event: Callable[[str, dict], None] | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 require_clean=True) -> dict:
    """Run one fresh DSH session and return its final answer and bounded trace."""
    started = time.monotonic()
    source_before = repository_snapshot(workspace)
    before = source_before
    if require_clean and source_before["dirty"]:
        before = isolated_clean_checkout(workspace)
        if event:
            event("dsh.isolated_workspace", {
                "source_workspace": source_before["workspace"], "workspace": before["workspace"],
                "reason": "source checkout had pre-existing changes", "source_status": source_before["status"],
            })
    command = headless_command(objective)
    timeout_seconds = max(30, min(int(timeout_seconds), 86_400))
    environment = safe_env(("DSH_HOME", "OLLAMA_LAUNCH_DSH_API_KEY", "DSH_TELEMETRY_MODE"))
    environment.setdefault("OLLAMA_LAUNCH_DSH_API_KEY", "local")
    # Keep the complete DSH trace on this machine. DSH supports DISABLED as a
    # first-class mode and then constructs no OpenTelemetry exporter state.
    environment.setdefault("DSH_TELEMETRY_MODE", "DISABLED")
    environment["DSH_PERMISSION_MODE"] = "workspace-write"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    native_before = _session_log_snapshot()
    process = subprocess.Popen(command, cwd=before["workspace"], env=environment, shell=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               errors="replace", bufsize=1, creationflags=creationflags,
                               start_new_session=os.name != "nt")
    if event:
        discovered = discover_dsh()
        event("dsh.started", {"pid": process.pid, "workspace": before["workspace"],
                              "version": discovered.get("version"), "permission_mode": "workspace-write",
                              "provider": discovered.get("provider"), "model": discovered.get("model"),
                              "context_window": discovered.get("context_window")})
    incoming: queue.Queue = queue.Queue()
    buffers = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}

    def pump(channel, stream):
        try:
            for raw in iter(stream.readline, ""):
                incoming.put((channel, ANSI.sub("", raw)[:MAX_LINE_CHARS]))
        finally:
            incoming.put((channel, None))

    threads = [threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
               threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True)]
    for thread in threads:
        thread.start()
    ended = set()
    termination = None
    deadline = time.monotonic() + timeout_seconds
    while len(ended) < 2 or process.poll() is None:
        if cancelled and cancelled():
            termination = "cancelled"
            _stop_process(process)
        elif time.monotonic() >= deadline:
            termination = "timeout"
            _stop_process(process)
        try:
            channel, line = incoming.get(timeout=0.25)
        except queue.Empty:
            if termination and process.poll() is not None:
                break
            continue
        if line is None:
            ended.add(channel)
            continue
        recorded = False
        if sizes[channel] < MAX_STREAM_CHARS:
            remaining = MAX_STREAM_CHARS - sizes[channel]
            kept = line[:remaining]
            buffers[channel].append(kept)
            sizes[channel] += len(kept)
            recorded = bool(kept.strip())
        if event and recorded:
            event("dsh.trace", {"channel": channel, "text": kept.rstrip()})
    for thread in threads:
        thread.join(timeout=2)
    returncode = process.wait(timeout=5)
    stdout = "".join(buffers["stdout"]).strip()
    stderr = "".join(buffers["stderr"]).strip()
    after = repository_snapshot(before["workspace"])
    native_after = _session_log_snapshot()
    native_session_logs = sorted(
        path for path, signature in native_after.items()
        if native_before.get(path) != signature
    )
    result = {
        "ok": returncode == 0 and bool(stdout) and not termination,
        "engine": "dsh", "answer": stdout, "trace": stderr,
        "returncode": returncode, "termination": termination,
        "duration_seconds": round(time.monotonic() - started, 3),
        "workspace": before["workspace"], "head_before": before["head"],
        "head_after": after["head"], "git_status": after["status"],
        "source_workspace": source_before["workspace"],
        "isolated_workspace": bool(before.get("isolated")),
        "preexisting_source_status": source_before["status"] if source_before["dirty"] else "",
        # DSH's compressed event log is the authoritative full-fidelity trace.
        # We expose its location for owner forensics but do not parse a private,
        # versioned transcript format or duplicate raw reasoning into SQLite.
        "native_session_logs": native_session_logs,
        "verification": {"status": "observed", "checks": [
            {"name": "dsh_exit_zero", "passed": returncode == 0},
            {"name": "dsh_final_answer", "passed": bool(stdout)},
            {"name": "workspace_was_clean", "passed": not before["dirty"]},
            {"name": "authoritative_source_untouched", "passed": bool(before.get("isolated")) or not source_before["dirty"]},
        ]},
    }
    if termination:
        result["error"] = f"DSH job {termination}"
    elif returncode:
        result["error"] = (stderr or f"DSH exited with code {returncode}")[-4000:]
    if event:
        event("dsh.completed", {key: result.get(key) for key in (
            "ok", "returncode", "termination", "duration_seconds", "workspace",
            "head_before", "head_after", "git_status", "native_session_logs", "error")})
    return result
