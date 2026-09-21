"""Stable deployment entry point for the core application."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("agent_core", Path(__file__).with_name("core_app.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
app = core.app

if __name__ == "__main__":
    # Loopback is the safe deployment default. Set CORE_BIND explicitly to a
    # Tailscale address when remote tailnet access is desired.
    bind_host = __import__("os").environ.get("CORE_BIND", "127.0.0.1")
    try:
        from waitress import serve
    except ImportError as exc:
        raise SystemExit("waitress is required for deployment; install requirements.txt") from exc
    serve(app, host=bind_host, port=core.PORT, threads=8)
