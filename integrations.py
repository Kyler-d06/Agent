"""Owner-installed adapters for HTTP, CLI, Python, MCP and browser applications."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests

from platform_contracts import canonical, safe_env, validate
from platform_contracts import confined


def check_manifest(manifest):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", manifest.get("name", "")):
        raise ValueError("invalid integration name")
    if manifest.get("type") not in {"http", "cli", "python", "mcp", "browser"}:
        raise ValueError("unsupported adapter")
    if not isinstance(manifest.get("tools"), list) or not 1 <= len(manifest["tools"]) <= 200:
        raise ValueError("manifest needs 1-200 tools")
    seen = set()
    from jsonschema import Draft202012Validator
    for t in manifest["tools"]:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,30}", t.get("name", "")) or t["name"] in seen:
            raise ValueError("invalid or duplicate tool name")
        seen.add(t["name"])
        if t.get("effect") not in {"read", "write", "execute", "external"}:
            raise ValueError("each tool must declare effect")
        if not t.get("description") or t.get("input_schema", {}).get("type") != "object":
            raise ValueError("description and object input_schema required")
        Draft202012Validator.check_schema(t["input_schema"])
        if t.get("output_schema"):
            Draft202012Validator.check_schema(t["output_schema"])
        if t.get("pricing") is not None:
            pricing = t["pricing"]
            if not isinstance(pricing, dict) or not isinstance(pricing.get("request_usd"), (int, float)) or pricing["request_usd"] < 0:
                raise ValueError("pricing.request_usd must be a non-negative number")
        if manifest["type"] == "browser" and any(s.get("action") in {"upload", "download"} for s in t.get("steps", [])):
            if t["effect"] == "read" or not manifest.get("transfer_root"):
                raise ValueError("browser transfers require a non-read effect and a configured transfer_root")
    # Secrets are referenced by environment name; literal credentials are not stored.
    for name in manifest.get("headers_env", {}).values():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("headers_env values must name environment variables")
    return manifest


async def mcp_request(config, operation, args=None):
    from contextlib import AsyncExitStack
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    async with AsyncExitStack() as stack:
        if config.get("transport", "stdio") == "stdio":
            params = StdioServerParameters(command=config["command"], args=config.get("args", []), env=safe_env(config.get("env_allowlist", [])))
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            import httpx
            headers = {k: os.environ[v] for k, v in config.get("headers_env", {}).items()}
            client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=60))
            read, write, _ = await stack.enter_async_context(streamable_http_client(config["url"], http_client=client))
        session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)))
        await session.initialize()
        if operation == "list":
            out, cursor = [], None
            for _ in range(20):
                result = await session.list_tools(cursor=cursor)
                out.extend(t.model_dump(by_alias=True, exclude_none=True) for t in result.tools)
                cursor = result.nextCursor
                if not cursor:
                    return out
            raise ValueError("MCP tool list exceeds 20 pages")
        result = await session.call_tool(operation, args or {})
        if result.isError:
            raise RuntimeError("MCP tool reported failure: " + canonical([c.model_dump() for c in result.content])[:2000])
        return result.model_dump(by_alias=True, exclude_none=True)


class IntegrationRegistry:
    def __init__(self, store):
        self.store = store

    def install(self, manifest):
        import time
        check_manifest(manifest)
        with self.store.connect() as db:
            db.execute("INSERT INTO integration_manifests VALUES(?,?,1,?) ON CONFLICT(name) DO UPDATE SET manifest_json=excluded.manifest_json,enabled=1,updated_at=excluded.updated_at",
                       (manifest["name"], canonical(manifest), time.time()))
        return {"name": manifest["name"], "tools": len(manifest["tools"])}

    def manifests(self):
        with self.store.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT manifest_json FROM integration_manifests WHERE enabled=1 ORDER BY name")]

    def tools(self):
        out = []
        for manifest in self.manifests():
            for spec in manifest["tools"]:
                out.append({"name": "ext_" + manifest["name"] + "_" + spec["name"], "description": spec["description"],
                            "input_schema": spec["input_schema"], "output_schema": spec.get("output_schema"),
                            "method": "POST", "path": "/api/tool-gateway", "effect": spec["effect"],
                            "requires_confirmation": spec["effect"] != "read", "integration": manifest["name"],
                            "pricing": spec.get("pricing")})
        return out

    def invoke(self, name, args):
        for m in self.manifests():
            for spec in m["tools"]:
                if name != "ext_" + m["name"] + "_" + spec["name"]:
                    continue
                validate(spec["input_schema"], args)
                result = getattr(self, "_" + m["type"])(m, spec, args)
                if spec.get("output_schema"):
                    validate(spec["output_schema"], result)
                return result
        raise ValueError("integration tool is unavailable")

    def _http(self, manifest, spec, args):
        base = manifest["base_url"].rstrip("/")
        path = spec["path"]
        if urlsplit(base).scheme not in {"http", "https"} or not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise ValueError("invalid configured endpoint")
        headers = {k: os.environ[v] for k, v in manifest.get("headers_env", {}).items()}
        method = spec.get("method", "POST").upper()
        response = requests.request(method, base + path, headers=headers,
                                    params=args if method == "GET" else None, json=args if method != "GET" else None,
                                    timeout=min(manifest.get("timeout", 60), 300), allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise ValueError("integration redirects require an explicit endpoint update")
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("integration response exceeds 2 MB")
        return response.json()

    def _cli(self, manifest, spec, args):
        command = spec.get("command", manifest.get("command"))
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("CLI requires an owner-configured argv array")
        # Model arguments travel as JSON stdin, never as shell text or extra flags.
        proc = subprocess.run(command, input=canonical(args), text=True, capture_output=True,
                              cwd=manifest.get("cwd"), env=safe_env(manifest.get("env_allowlist", [])),
                              timeout=min(manifest.get("timeout", 60), 300), shell=False)
        if proc.returncode:
            raise RuntimeError("configured command failed: " + proc.stderr[:2000])
        if len(proc.stdout) > 2_000_000:
            raise ValueError("integration response exceeds 2 MB")
        return json.loads(proc.stdout)

    def _python(self, manifest, spec, args):
        # A trusted installed module, not arbitrary code returned by a model.
        module = spec.get("module", manifest.get("module", ""))
        if not re.fullmatch(r"[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*", module):
            raise ValueError("invalid installed module")
        return self._cli(manifest, {"command": [manifest.get("python", sys.executable), "-m", module]}, args)

    def _mcp(self, manifest, spec, args):
        return asyncio.run(mcp_request(manifest, spec.get("remote_name", spec["name"]), args))

    def _browser(self, manifest, spec, args):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(manifest["profile_dir"], headless=manifest.get("headless", True), accept_downloads=True)
            try:
                page = context.new_page()
                page.set_default_timeout(30000)
                page.goto(spec.get("url", manifest["url"]), wait_until="domcontentloaded")
                downloads = []
                for step in spec.get("steps", []):
                    locator = page.get_by_role(step["role"], name=step.get("name"), exact=True) if "role" in step else page.locator(step["selector"])
                    action = step["action"]
                    if action == "fill":
                        locator.fill(str(args[step["argument"]]))
                    elif action == "click":
                        locator.click()
                    elif action == "wait":
                        locator.wait_for(state=step.get("state", "visible"))
                    elif action == "select":
                        locator.select_option(str(args[step["argument"]]))
                    elif action == "upload":
                        path = confined(manifest["transfer_root"], str(args[step["argument"]]))
                        if not path.is_file() or path.stat().st_size > 100_000_000:
                            raise ValueError("upload must be a file under 100 MB in the configured transfer root")
                        locator.set_input_files(str(path))
                    elif action == "download":
                        path = confined(manifest["transfer_root"], str(args[step["argument"]]))
                        if path.exists():
                            raise ValueError("download destination already exists")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with page.expect_download() as pending:
                            locator.click()
                        download = pending.value
                        if download.failure():
                            raise RuntimeError("browser download failed")
                        download.save_as(str(path))
                        import hashlib
                        with path.open("rb") as f:
                            sha = hashlib.file_digest(f, "sha256").hexdigest()
                        downloads.append({"path": str(path), "sha256": sha, "bytes": path.stat().st_size})
                    else:
                        raise ValueError("unsupported configured browser action")
                observation = page.locator(spec["result_selector"]).inner_text()
                expected = spec.get("expected_text")
                if expected and expected not in observation:
                    raise ValueError("browser postcondition was not observed")
                return {"text": observation[:50000], "url": page.url, "verified": bool(expected), "downloads": downloads}
            finally:
                context.close()
