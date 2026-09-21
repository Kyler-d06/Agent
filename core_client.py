"""Trusted client transport; model arguments never control credentials or URLs."""
from __future__ import annotations

import os
import time
import uuid

import requests

from platform_contracts import actor_key


class CoreClient:
    def __init__(self, actor="assistant"):
        self.actor = actor
        self.base = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
        self.owner_key = os.environ.get("CORE_API_KEY", "")
        if not self.owner_key and not os.environ.get(actor.upper() + "_API_KEY"):
            raise ValueError("configure CORE_API_KEY or the scoped " + actor.upper() + "_API_KEY")
        self.key = os.environ.get(actor.upper() + "_API_KEY") or actor_key(self.owner_key, actor)
        self.job = None

    def headers(self, owner=False):
        headers = {"X-API-Key": self.owner_key if owner else self.key, "Content-Type": "application/json"}
        if self.job:
            headers.update({"X-Job-Id": self.job["id"], "X-Lease-Token": self.job["lease_token"]})
        return headers

    def request(self, method, path, *, data=None, params=None, owner=False, timeout=300):
        r = requests.request(method, self.base + path, headers=self.headers(owner), json=data, params=params, timeout=timeout)
        try:
            result = r.json()
        except ValueError:
            return {"ok": False, "error": {"message": f"core returned HTTP {r.status_code} without JSON"}}
        if r.status_code >= 400 and (not isinstance(result, dict) or result.get("ok") is not False):
            return {"ok": False, "error": {"message": f"core returned HTTP {r.status_code}"}}
        return result

    def tools(self):
        result = self.request("GET", "/api/tools")
        if not isinstance(result, list):
            raise RuntimeError("could not load tool catalog: " + str(result))
        return result

    def invoke(self, name, args, request_id=None, confirmed=False):
        rid = request_id or uuid.uuid4().hex
        if confirmed:
            if not self.owner_key:
                return {"ok": False, "result": None, "error": {
                    "message": "owner approval must be granted from the dashboard or owner CLI",
                    "code": "approval_required",
                }}
            # Only the trusted Telegram yes/owner command path sets this flag.
            # Exact arguments bind a single-use grant to the reviewed action.
            grant = self.request("POST", "/api/owner/grants", owner=True,
                                 data={"actor": self.actor, "tool": name, "constraints": args, "uses": 1, "expires_at": time.time() + 120})
            if not grant.get("ok"):
                return grant
        payload = {"name": name, "args": args, "request_id": rid}
        if self.job:
            payload.update({"job_id": self.job["id"], "lease_token": self.job["lease_token"]})
        return self.request("POST", "/api/tool-gateway", data=payload)

    def chat(self, messages, tools=None, temperature=None, task_type="general", prefer_fallback=False):
        response = self.request("POST", "/api/models/chat", data={"messages": messages, "tools": tools,
                                "temperature": temperature, "task_type": task_type,
                                "prefer_fallback": bool(prefer_fallback)}, timeout=650)
        if not response.get("ok"):
            raise RuntimeError("model gateway failed: " + str(response.get("error")))
        return response["result"]

    def checkpoint(self, state=None):
        if not self.job:
            return {}
        payload = {"id": self.job["id"], "lease_token": self.job["lease_token"]}
        if state is not None:
            payload["state"] = state
        response = self.request("POST", "/api/runtime/jobs/checkpoint", data=payload)
        if not response.get("ok"):
            raise RuntimeError("checkpoint rejected: " + str(response.get("error")))
        return response["result"]["state"]
