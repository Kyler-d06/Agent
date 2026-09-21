"""Shared contracts. Authority is enforced by the core, never by model output."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def actor_key(owner_key, actor):
    return hmac.new(owner_key.encode(), ("agent-platform/v1/" + actor).encode(), hashlib.sha256).hexdigest()


def validate(schema, value):
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)


def object_schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def tool(name, description, properties=None, required=(), *, effect="read", **extra):
    return {"name": name, "description": description, "input_schema": object_schema(properties, required),
            "method": "POST", "path": "/api/tool-gateway", "effect": effect,
            "requires_confirmation": effect in {"execute", "external", "admin"}, **extra}


def confined(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or ":" in str(relative):
        raise ValueError("expected a relative workspace path")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes workspace")
    # Check both spelling and resolved destination: an innocent-looking symlink
    # must not expose private files elsewhere inside the same workspace.
    for part in (*rel.parts, *target.relative_to(root).parts):
        low = part.lower()
        if low.startswith(".") or low.endswith((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal")) or low in {"__pycache__", "node_modules"} or any(
            word in low for word in (".env", ".pem", ".key", "credential", "secret", "session", "profile")
        ):
            raise ValueError("private path")
    return target


def safe_env(names=()):
    # Windows' pathlib and several standard tools require these non-secret
    # profile variables even in a deliberately sanitized child environment.
    base = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG",
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA")
    return {k: os.environ[k] for k in (*base, *names) if k in os.environ}


def select_tools(tools, objective, limit=24):
    terms = set(re.findall(r"[a-z0-9_]{3,}", objective.lower()))
    essential = {"discover_tools", "recall_memory", "remember", "list_work_templates", "request_capability"}
    def score(t):
        words = set(re.findall(r"[a-z0-9_]{3,}", (t["name"].replace("_", " ") + " " + t["description"]).lower()))
        return (100 if t["name"] in essential else 0) + len(words & terms)
    ranked = sorted(tools, key=lambda t: (-score(t), t["name"]))
    # Small local models degrade sharply when every unused schema is included.
    # Keep essential discovery tools plus tools that actually match the task.
    relevant = [t for t in ranked if t["name"] in essential or score(t) > 0]
    return relevant[:max(1, min(limit, 100))]
