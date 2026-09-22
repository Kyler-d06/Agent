"""Expose the core tool registry over MCP stdio, using the same permission gateway.

Run: python mcp_bridge.py. Log diagnostics to stderr; stdout is protocol-only.
"""
import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from platform_contracts import actor_key


HANDOFF_TOOL = "save_handoff_checkpoint"
HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "current_status": {"type": "string"},
        "completed": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "notes_for_next_model": {"type": "string"},
    },
    "required": ["objective", "current_status", "next_steps"],
    "additionalProperties": False,
}


class HandoffJournal:
    """Continuously maintain a secret-minimized cross-model transfer sheet."""

    def __init__(self, vault):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.target = Path(vault).resolve() / "Model Handoffs" / "ACTIVE - Claude MCP.md"
        self.summary = {}
        self.activity = []
        self.calls_since_summary = 0
        self.lock = threading.Lock()
        self._write()

    @staticmethod
    def _text(value, limit=6000):
        return str(value or "").strip()[:limit]

    @classmethod
    def _items(cls, value):
        return [cls._text(item, 1000) for item in (value or [])[:50] if cls._text(item, 1000)]

    @staticmethod
    def _status(result):
        if not isinstance(result, dict):
            return "unknown"
        if result.get("ok") is False:
            error = result.get("error") or {}
            return "error: " + str(error.get("code") or error.get("message") or "failed")[:160]
        return "ok"

    def record(self, name, result):
        with self.lock:
            self.activity.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "tool": str(name)[:120],
                "status": self._status(result),
            })
            self.activity = self.activity[-100:]
            self.calls_since_summary += 1
            self._write()
            return self.calls_since_summary

    def checkpoint(self, value):
        with self.lock:
            self.summary = {
                "objective": self._text(value.get("objective")),
                "current_status": self._text(value.get("current_status")),
                "completed": self._items(value.get("completed")),
                "decisions": self._items(value.get("decisions")),
                "files_changed": self._items(value.get("files_changed")),
                "tests": self._items(value.get("tests")),
                "next_steps": self._items(value.get("next_steps")),
                "blockers": self._items(value.get("blockers")),
                "notes_for_next_model": self._text(value.get("notes_for_next_model")),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.calls_since_summary = 0
            self._write()
            return {"path": str(self.target), "updated": True}

    @staticmethod
    def _section(title, items):
        rows = items or ["None recorded."]
        return "## " + title + "\n\n" + "\n".join("- " + item for item in rows) + "\n"

    def _render(self):
        summary = self.summary
        text = [
            "---",
            "tags: [model-handoff, mcp, active]",
            f"session_started: {self.started_at}",
            f"updated: {datetime.now(timezone.utc).isoformat()}",
            "---", "", "# Active model handoff", "",
            "> This sheet is maintained continuously by the Universal Assistant MCP bridge. "
            "Claude subscription usage is not visible to MCP, so checkpoints are time/tool based.", "",
            "## Objective", "", summary.get("objective") or "Not supplied by the model yet.", "",
            "## Current status", "", summary.get("current_status") or "MCP session connected; detailed checkpoint pending.", "",
            self._section("Completed", summary.get("completed")),
            self._section("Decisions and assumptions", summary.get("decisions")),
            self._section("Files changed", summary.get("files_changed")),
            self._section("Tests and evidence", summary.get("tests")),
            self._section("Next steps", summary.get("next_steps")),
            self._section("Blockers and required permissions", summary.get("blockers")),
            "## Notes for the next model", "", summary.get("notes_for_next_model") or "None recorded.", "",
            "## Automatic MCP activity (secret-minimized)", "",
        ]
        text.extend(
            f"- {row['at']} — `{row['tool']}` — {row['status']}" for row in self.activity
        )
        if not self.activity:
            text.append("- Connected; no tool calls recorded yet.")
        return "\n".join(text) + "\n"

    def _write(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target.with_name(self.target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(self._render(), encoding="utf-8")
            os.replace(temporary, self.target)
        finally:
            temporary.unlink(missing_ok=True)


def _assistant_data_dir() -> Path:
    configured = os.environ.get("ASSISTANT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "UniversalAssistant").resolve()
    return (Path.home() / ".local" / "share" / "universal-assistant").resolve()


def _core_api_key() -> str:
    """Resolve the owner key without requiring it in every MCP client config."""
    direct = os.environ.get("CORE_API_KEY")
    if direct:
        return direct
    data_dir = _assistant_data_dir()
    try:
        records = json.loads((data_dir / "managed-secrets.json").read_text(encoding="utf-8"))
        value = records["CORE_API_KEY"]
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "MCP bridge could not load CORE_API_KEY; start the Universal Assistant once "
            "or set CORE_API_KEY/MCP_AGENT_KEY in the MCP client environment"
        ) from exc
    raise RuntimeError("managed CORE_API_KEY is empty")


def _obsidian_vault() -> Path:
    """Resolve the operator-selected vault without requesting owner-only dashboard state."""
    configured = os.environ.get("OBSIDIAN_VAULT")
    if configured:
        return Path(configured).expanduser().resolve()
    deployment = _assistant_data_dir() / "deployment.json"
    try:
        value = json.loads(deployment.read_text(encoding="utf-8"))
        vault = value.get("obsidian_vault") if isinstance(value, dict) else None
        if isinstance(vault, str) and vault.strip():
            return Path(vault).expanduser().resolve()
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("MCP bridge could not load the configured Obsidian vault") from exc
    raise RuntimeError("OBSIDIAN_VAULT is not configured")


def build_server():
    """Build the shared low-level server used by stdio and HTTP transports."""
    from mcp.server.lowlevel import Server
    import mcp.types as types
    server = Server("universal-assistant")
    base = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
    headers = {"X-API-Key": os.environ.get("MCP_AGENT_KEY") or actor_key(_core_api_key(), "mcp")}
    journal = None

    def request(method, path, **kwargs):
        r = requests.request(method, base + path, headers=headers, timeout=300, **kwargs)
        r.raise_for_status()
        return r.json()

    def handoff_journal():
        nonlocal journal
        if journal is None:
            journal = HandoffJournal(_obsidian_vault())
        return journal

    @server.list_tools()
    async def list_tools():
        catalog = await asyncio.to_thread(request, "GET", "/api/tools")
        tools = [types.Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"]) for t in catalog]
        tools.append(types.Tool(
            name=HANDOFF_TOOL,
            description=("Update the active Markdown transfer sheet for the next model. Call at the start, after every "
                         "major milestone, and at least every four MCP tool calls. Exclude credentials and secrets."),
            inputSchema=HANDOFF_SCHEMA,
        ))
        try:
            await asyncio.to_thread(handoff_journal)
        except Exception:
            pass
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == HANDOFF_TOOL:
            try:
                result = await asyncio.to_thread(handoff_journal().checkpoint, arguments or {})
                payload = {"ok": True, "result": result}
            except Exception as exc:
                payload = {"ok": False, "error": {"code": "handoff_write_failed", "message": str(exc)[:500]}}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))],
                                        isError=not payload["ok"])
        result = await asyncio.to_thread(request, "POST", "/api/tool-gateway",
                                         json={"name": name, "args": arguments or {}, "request_id": uuid.uuid4().hex})
        try:
            count = await asyncio.to_thread(handoff_journal().record, name, result)
            result["handoff"] = {
                "path": str(handoff_journal().target),
                "checkpoint_due": count >= 4,
                "message": ("Call save_handoff_checkpoint now so another model can resume this work."
                            if count >= 4 else "Automatic activity sheet updated."),
            }
        except Exception:
            pass
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))], isError=not result.get("ok", False))

    return server


def main():
    from mcp.server.stdio import stdio_server
    server = build_server()

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()
