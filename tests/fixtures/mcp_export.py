"""Run the actual MCP bridge with a deterministic fake core HTTP boundary."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["CORE_API_KEY"] = "test-owner-key"

import mcp_bridge


class Response:
    def __init__(self, body):
        self.body = body
    def raise_for_status(self):
        pass
    def json(self):
        return self.body


def request(method, url, **kwargs):
    if url.endswith("/api/tools"):
        return Response([{"name": "echo", "description": "Echo input", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}])
    return Response({"ok": True, "result": kwargs["json"]["args"]["text"], "error": None})


mcp_bridge.requests.request = request
mcp_bridge.main()
