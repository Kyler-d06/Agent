"""Authenticated Streamable HTTP transport for the Universal Harness MCP server.

The default listener is loopback-only. A public ChatGPT connector additionally
needs an operator-managed HTTPS reverse proxy/tunnel; this process never opens a
tunnel or weakens the core permission gateway itself.
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import os

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp_bridge import _core_api_key, build_server
from platform_contracts import actor_key


def transport_token() -> str:
    """A transport-only secret; it is not the core owner credential."""
    return os.environ.get("MCP_HTTP_TOKEN") or actor_key(_core_api_key(), "mcp-http-transport")


class StreamableEndpoint:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


class BearerGuard:
    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("utf-8", "replace")
        expected = "Bearer " + self.token
        if not hmac.compare_digest(supplied, expected):
            response = PlainTextResponse("Bearer token required", status_code=401,
                                         headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(host="127.0.0.1", port=5078, token=None):
    allowed_hosts = [f"{host}:{port}", host]
    if host in {"127.0.0.1", "localhost"}:
        allowed_hosts += [f"localhost:{port}", f"127.0.0.1:{port}"]
    manager = StreamableHTTPSessionManager(
        app=build_server(), json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(set(allowed_hosts)),
            allowed_origins=[],
        ),
        max_request_body_size=1_000_000,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    endpoint = BearerGuard(StreamableEndpoint(manager), token or transport_token())
    return Starlette(routes=[Route("/mcp", endpoint=endpoint)], lifespan=lifespan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5078)
    parser.add_argument("--allow-network", action="store_true",
                        help="required when binding beyond loopback; HTTPS must be supplied by a trusted proxy")
    parser.add_argument("--print-token", action="store_true",
                        help="print the transport token for explicit connector setup and exit")
    args = parser.parse_args()
    if args.print_token:
        print(transport_token())
        return
    if args.host not in {"127.0.0.1", "localhost"} and not args.allow_network:
        parser.error("non-loopback binding requires --allow-network")
    import uvicorn
    uvicorn.run(create_app(args.host, args.port), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
