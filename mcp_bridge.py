"""Expose the core tool registry over MCP stdio, using the same permission gateway.

Run: python mcp_bridge.py. Log diagnostics to stderr; stdout is protocol-only.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

import requests

from platform_contracts import actor_key


def _core_api_key() -> str:
    """Resolve the owner key without requiring it in every MCP client config."""
    direct = os.environ.get("CORE_API_KEY")
    if direct:
        return direct
    configured = os.environ.get("ASSISTANT_DATA_DIR")
    if configured:
        data_dir = Path(configured).expanduser().resolve()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        data_dir = (Path(os.environ["LOCALAPPDATA"]) / "UniversalAssistant").resolve()
    else:
        data_dir = (Path.home() / ".local" / "share" / "universal-assistant").resolve()
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


def build_server():
    """Build the shared low-level server used by stdio and HTTP transports."""
    from mcp.server.lowlevel import Server
    import mcp.types as types
    server = Server("universal-assistant")
    base = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
    headers = {"X-API-Key": os.environ.get("MCP_AGENT_KEY") or actor_key(_core_api_key(), "mcp")}

    def request(method, path, **kwargs):
        r = requests.request(method, base + path, headers=headers, timeout=300, **kwargs)
        r.raise_for_status()
        return r.json()

    @server.list_tools()
    async def list_tools():
        catalog = await asyncio.to_thread(request, "GET", "/api/tools")
        return [types.Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"]) for t in catalog]

    @server.call_tool()
    async def call_tool(name, arguments):
        import json
        result = await asyncio.to_thread(request, "POST", "/api/tool-gateway",
                                         json={"name": name, "args": arguments or {}, "request_id": uuid.uuid4().hex})
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
