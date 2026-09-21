#!/usr/bin/env python3
"""
core_server.py — the one backend everything else in this project talks to.

Single SQLite database, one password, one process. The dashboard, your
Telegram bot's LLM tool calls, web_capture.py, scan_to_cad.py output, and
anything you build later all read and write through this same set of flat
HTTP endpoints — nothing is stuck living inside any one client anymore.

Run it once, on the Pi or laptop:
    pip install flask
    CORE_PASSWORD=pickarealpassword CORE_ROOT=/path/to/project python3 core_server.py

Reach it from anywhere on your tailnet:
    http://<tailscale-hostname>:5077        <- dashboard UI, in any browser
    http://<tailscale-hostname>:5077/api/*  <- everything below, for scripts/bots

GET /api/tools returns a machine-readable list of every LLM-callable tool
(name, description, JSON schema, HTTP method + path) — point your Telegram
bot's tool-use loop at this endpoint once and it never needs updating by
hand again; add a tool here, it shows up there.

SECURITY: same rule as fileedit_app before it — this can execute files.
Tailnet only, real password, never Funnel this out publicly.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from functools import wraps
from pathlib import Path

from bs4 import BeautifulSoup
from flask import Flask, request, session, redirect, jsonify, abort, g
from agent_platform import AgentPlatform, PLATFORM_TOOLS
from intelligence_platform import IntelligencePlatform, INTELLIGENCE_TOOLS
from forecasting_platform import ForecastingPlatform, FORECAST_TOOLS
from system_monitor import snapshot as system_snapshot, health as system_health
from mesh_platform import MeshPlatform, MESH_TOOLS
from vault_platform import VaultPlatform, VAULT_TOOLS
from universal_platform import UniversalPlatform
from runtime_store import redact
from web_safety import safe_get, validate_public_url

# ---------- config ----------

ROOT_DIR = os.path.realpath(os.environ.get("CORE_ROOT", os.path.expanduser("~/projects")))
DB_PATH = os.environ.get("CORE_DB", os.path.join(ROOT_DIR, ".core_server.db"))
PASSWORD = os.environ.get("CORE_PASSWORD", "")
if not PASSWORD or PASSWORD == "changeme":
    raise RuntimeError("Set CORE_PASSWORD to a private dashboard password before starting the assistant")
API_KEY = os.environ.get("CORE_API_KEY", PASSWORD)
PORT = int(os.environ.get("CORE_PORT", "5077"))
BIND_HOST = os.environ.get("CORE_BIND", "127.0.0.1")
EXECUTOR_ONLY = os.environ.get("EXECUTOR_ONLY", "0") == "1"

# Research tools. "ddg" needs no signup and is the default; its HTML
# structure could change without notice since it's not a documented API.
# "brave" is a real API with a stable contract, IF you have a working key —
# reports on whether Brave still has a free tier conflict as of writing
# this, so verify directly at brave.com/search/api before relying on it.
SEARCH_BACKEND = os.environ.get("SEARCH_BACKEND", "ddg")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
READ_PAGE_MAX_CHARS = 8000
LOOP_FREQUENCIES = {
    "assistant_poll_seconds": (1, 60, 5),
    "approval_notification_seconds": (2, 300, 5),
    "system_monitor_seconds": (2, 3600, 15),
    "memory_consolidation_seconds": (900, 604800, 21600),
    "discovery_seconds": (60, 86400, 1800),
    "overnight_report_seconds": (60, 3600, 300),
}

MANAGED_SECRET_SPECS = {
    "APCA_API_KEY_ID": {"label": "Alpaca API key ID", "group": "Alpaca", "minimum": 8},
    "APCA_API_SECRET_KEY": {"label": "Alpaca API secret", "group": "Alpaca", "minimum": 16},
    "TELEGRAM_BOT_TOKEN": {"label": "Telegram bot token", "group": "Telegram", "minimum": 20},
    "ALLOWED_CHAT_IDS": {"label": "Telegram allowed chat IDs", "group": "Telegram", "minimum": 1},
    "BRAVE_API_KEY": {"label": "Brave Search API key", "group": "Web research", "minimum": 8},
}
PROTECTED_SECRET_NAMES = {"CORE_PASSWORD", "CORE_API_KEY", "CORE_SECRET", "MCP_AGENT_KEY", "MCP_HTTP_TOKEN"}


def _managed_secret_path():
    return Path(DB_PATH).resolve().parent / "managed-secrets.json"


def _managed_secret_records():
    path = _managed_secret_path()
    if not path.is_file():
        return {}
    if path.stat().st_size > 1_000_000:
        raise ValueError("managed secret file exceeds 1 MB")
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, dict):
        raise ValueError("managed secret file is invalid")
    return records


def _managed_secret_status():
    try:
        records = _managed_secret_records()
    except (OSError, ValueError, json.JSONDecodeError):
        records = {}
    names = sorted(set(MANAGED_SECRET_SPECS) | {name for name in records if name.startswith("INTEGRATION_")})
    now = time.time()
    result = []
    for name in names:
        record = records.get(name) if isinstance(records.get(name), dict) else {}
        rotated = record.get("rotated_at")
        age_days = round((now - float(rotated)) / 86400, 1) if isinstance(rotated, (int, float)) else None
        spec = MANAGED_SECRET_SPECS.get(name) or {"label": name, "group": "Custom integration"}
        result.append({"name": name, "label": spec["label"], "group": spec["group"],
                       "configured": bool(record.get("value")), "age_days": age_days})
    return result


def _claude_desktop_config_path():
    # The Microsoft Store build virtualizes %APPDATA% inside its package.  Claude's
    # own log reports this LocalCache path and ignores the ordinary roaming path.
    localappdata = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and localappdata:
        packages = Path(localappdata).expanduser().resolve() / "Packages"
        packaged = sorted(
            (item for item in packages.glob("Claude_*") if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if packaged:
            return packaged[0] / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata).expanduser().resolve() / "Claude" / "claude_desktop_config.json"
    return Path.home().resolve() / ".config" / "Claude" / "claude_desktop_config.json"


def _claude_mcp_entry():
    source = Path(__file__).resolve().parent
    project_python = source / ".venv" / "Scripts" / "python.exe"
    executable = project_python if project_python.is_file() else Path(sys.executable).resolve()
    return {"command": str(executable), "args": [str(source / "mcp_bridge.py")]}


def _claude_mcp_status():
    path = _claude_desktop_config_path()
    status = {"path": str(path), "exists": path.is_file(), "configured": False, "valid": True}
    if not path.is_file():
        return status
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError("configuration exceeds 1 MB")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("configuration root is not an object")
        entry = (value.get("mcpServers") or {}).get("universal-assistant")
        status["configured"] = entry == _claude_mcp_entry()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        status.update({"valid": False, "error": str(exc)[:500]})
    return status


def _write_managed_secrets(records):
    path = _managed_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalized_loop_frequencies(saved):
    saved = saved if isinstance(saved, dict) else {}
    values = {}
    for name, (minimum, maximum, default) in LOOP_FREQUENCIES.items():
        try:
            value = int(saved.get(name, default))
        except (TypeError, ValueError):
            value = default
        values[name] = max(minimum, min(value, maximum))
    return values

# Discovery / research-lab integration. SQLite is canonical runtime state;
# Obsidian is the human-readable mirror, and RESEARCH_REPO is the local Git
# repository used for reproducible experiments and discovery artifacts.
OBSIDIAN_VAULT = os.path.realpath(os.environ.get("OBSIDIAN_VAULT", os.path.join(ROOT_DIR, "obsidian")))
RESEARCH_REPO = os.path.realpath(os.environ.get("RESEARCH_REPO", os.path.join(ROOT_DIR, "research_repo")))
DISCOVERY_VAULT_DIR = os.environ.get("DISCOVERY_VAULT_DIR", "Research")
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "Discovery Agent")
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "discovery-agent@local")

RUNNERS = {".py": ["python3"], ".sh": ["bash"], ".js": ["node"]}
MAX_OUTPUT_CHARS = 20000
RUN_TIMEOUT_SECONDS = 30

# Executed files get only these env vars, not a full copy of core_server's
# environment — CORE_API_KEY, CORE_PASSWORD, and anything else stay out of
# reach of code the agent writes and runs.
SAFE_ENV_ALLOWLIST = ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "TMPDIR"]

# Never readable/writable/runnable/listable through the file API, even
# though they live inside ROOT_DIR — path confinement alone doesn't protect
# secrets that happen to sit inside the project folder.
SENSITIVE_PATTERNS = [".env", ".key", ".pem", "_session.json", ".session", "credentials", "secrets", ".git", ".ssh"]

os.makedirs(ROOT_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("CORE_SECRET", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")


# ---------- database ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.execute("PRAGMA busy_timeout=30000")
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY, name TEXT UNIQUE, status_url TEXT,
        status TEXT DEFAULT 'unconfigured', latency INTEGER, last_checked TEXT
    );
    CREATE TABLE IF NOT EXISTS capture_entries (
        id TEXT PRIMARY KEY, text TEXT, tag TEXT, source TEXT, ts TEXT
    );
    CREATE TABLE IF NOT EXISTS vault_links (
        id TEXT PRIMARY KEY, name TEXT, url TEXT, tag TEXT
    );
    CREATE TABLE IF NOT EXISTS audio_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER DEFAULT 0, note TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, tag TEXT, ts TEXT, source_json TEXT, payload_json TEXT
    );
    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY, ts TEXT, tool TEXT, args_json TEXT, result_json TEXT, ok INTEGER
    );

    -- Discovery engine: normalized, queryable epistemic state.
    CREATE TABLE IF NOT EXISTS research_questions (
        id TEXT PRIMARY KEY, question TEXT NOT NULL, domain TEXT, status TEXT DEFAULT 'open',
        priority REAL DEFAULT 0.5, rationale TEXT, created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS hypotheses (
        id TEXT PRIMARY KEY, question_id TEXT, claim TEXT NOT NULL, domain TEXT,
        status TEXT DEFAULT 'candidate', prior REAL DEFAULT 0.25, confidence REAL DEFAULT 0.25,
        novelty REAL DEFAULT 0.5, importance REAL DEFAULT 0.5, testability REAL DEFAULT 0.5,
        actionability REAL DEFAULT 0.5, consensus_estimate REAL DEFAULT 0.5,
        falsification_criterion TEXT, strongest_counterargument TEXT, next_test TEXT,
        created_at TEXT, updated_at TEXT,
        FOREIGN KEY(question_id) REFERENCES research_questions(id)
    );
    CREATE TABLE IF NOT EXISTS evidence (
        id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, stance TEXT NOT NULL,
        summary TEXT NOT NULL, source_url TEXT, source_title TEXT, source_type TEXT,
        reliability REAL DEFAULT 0.5, independence REAL DEFAULT 0.5, weight REAL DEFAULT 0.5,
        observed_at TEXT, created_at TEXT,
        FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
    );
    CREATE TABLE IF NOT EXISTS predictions (
        id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, prediction TEXT NOT NULL,
        due_at TEXT, status TEXT DEFAULT 'open', outcome TEXT, probability REAL DEFAULT 0.5,
        created_at TEXT, resolved_at TEXT,
        FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
    );
    CREATE TABLE IF NOT EXISTS consensus_claims (
        id TEXT PRIMARY KEY, domain TEXT, claim TEXT NOT NULL, estimate REAL DEFAULT 0.5,
        basis TEXT, source_urls_json TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS research_tasks (
        id TEXT PRIMARY KEY, hypothesis_id TEXT, question_id TEXT, task TEXT NOT NULL,
        task_type TEXT DEFAULT 'research', status TEXT DEFAULT 'queued',
        expected_information_gain REAL DEFAULT 0.5, estimated_cost REAL DEFAULT 0.5,
        priority REAL DEFAULT 0.5, result_json TEXT, created_at TEXT, updated_at TEXT,
        FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id),
        FOREIGN KEY(question_id) REFERENCES research_questions(id)
    );
    CREATE TABLE IF NOT EXISTS experiments (
        id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, name TEXT NOT NULL, method TEXT,
        artifact_path TEXT, status TEXT DEFAULT 'planned', result_summary TEXT,
        result_json TEXT, created_at TEXT, updated_at TEXT,
        FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
    );
    CREATE TABLE IF NOT EXISTS discoveries (
        id TEXT PRIMARY KEY, hypothesis_id TEXT, title TEXT NOT NULL, summary TEXT NOT NULL,
        score REAL DEFAULT 0.0, status TEXT DEFAULT 'candidate', created_at TEXT, updated_at TEXT,
        FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
    );
    CREATE TABLE IF NOT EXISTS hypothesis_history (
        id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, ts TEXT, confidence REAL, status TEXT,
        reason TEXT, FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
    );
    CREATE INDEX IF NOT EXISTS idx_evidence_hypothesis ON evidence(hypothesis_id);
    CREATE INDEX IF NOT EXISTS idx_predictions_hypothesis ON predictions(hypothesis_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON research_tasks(status, priority DESC);
    CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status);

    INSERT OR IGNORE INTO audio_state (id, enabled, note) VALUES (1, 0, '');
    """)
    db.commit()
    db.close()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- standardized result envelope (used by LLM-facing tool endpoints) ----------
# The dashboard's own endpoints (nodes/add, capture/remove, /api/state, etc.)
# keep their original plain-JSON shape unchanged — only the endpoints listed
# in TOOLS, the ones an LLM actually calls, use this. An LLM reasoning over
# results needs an unambiguous ok/error signal more than a human clicking a
# button does; changing the dashboard's shape too would just add rewrite
# risk to its already-working JS for no real benefit.

def ok(result):
    return jsonify({"ok": True, "result": result, "error": None})


def err(message, code=400):
    return jsonify({"ok": False, "result": None, "error": {"message": message}}), code


# ---------- action ledger ----------
# Every LLM-facing tool call gets recorded here automatically, regardless of
# whether it succeeded — this is pure observability, it doesn't gate or
# change what any tool does, it just means "what did the agent actually do"
# is answerable later without trusting the model's own account of it.

def log_action(tool_name, args, response_body):
    try:
        db = get_db()
        was_ok = bool(response_body.get("ok", True)) if isinstance(response_body, dict) else True
        db.execute("INSERT INTO actions (id, ts, tool, args_json, result_json, ok) VALUES (?, ?, ?, ?, ?, ?)",
                   (str(uuid.uuid4()), now_iso(), tool_name, json.dumps(redact(args)),
                    json.dumps(redact(response_body)), int(was_ok)))
        db.commit()
    except Exception as e:
        print(f"[action log failed for {tool_name}]: {e}")


def logged_tool(name):
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **kw):
            args = request.get_json(silent=True) or dict(request.args) or {}
            resp = f(*a, **kw)
            resp_obj = resp[0] if isinstance(resp, tuple) else resp
            try:
                body = resp_obj.get_json()
            except Exception:
                body = None
            log_action(name, args, body)
            return resp
        return wrapper
    return decorator


# ---------- auth ----------

def require_login():
    if not session.get("ok"):
        abort(401)


@app.before_request
def guard():
    open_paths = {"/login"}
    if request.path in open_paths or request.path.startswith("/static"):
        return
    if request.path.startswith("/api/"):
        if "universal_platform" in globals():
            return universal_platform.guard()
        if request.headers.get("X-API-Key") == API_KEY:
            return
        if not session.get("ok"):
            abort(401)
        return
    if request.path == "/" and not session.get("ok"):
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password") or (request.get_json(silent=True) or {}).get("password")
        if pw == PASSWORD:
            session["ok"] = True
            return redirect("/") if request.form.get("password") else jsonify({"ok": True})
        return (LOGIN_PAGE.replace("{{ERROR}}", "<p class='err'>Wrong password.</p>")
                if request.form.get("password") else (jsonify({"error": "wrong password"}), 401))
    return LOGIN_PAGE.replace("{{ERROR}}", "")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------- file safety (shared with fileedit_app's approach) ----------

def is_sensitive(path: str) -> bool:
    lower = path.lower()
    return any(pat in lower for pat in SENSITIVE_PATTERNS)


def safe_path(rel_path: str, allow_sensitive: bool = False) -> str:
    rel_path = (rel_path or "").lstrip("/")
    target = os.path.realpath(os.path.join(ROOT_DIR, rel_path))
    if target != ROOT_DIR and not target.startswith(ROOT_DIR + os.sep):
        abort(403, "path escapes project root")
    if not allow_sensitive and is_sensitive(target):
        abort(403, "path matches a protected pattern (.env/.key/.pem/session/credentials/secrets/.git/.ssh)")
    return target


# ---------- tool: nodes ----------

@app.route("/api/nodes/add", methods=["POST"])
def nodes_add():
    d = request.get_json(force=True)
    name = d.get("name")
    if not name:
        return jsonify({"error": "name required"}), 400
    db = get_db()
    node_id = str(uuid.uuid4())
    try:
        db.execute("INSERT INTO nodes (id, name, status_url, status) VALUES (?, ?, ?, 'unconfigured')",
                   (node_id, name, d.get("status_url", "")))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": f"a node named '{name}' already exists"}), 409
    return jsonify({"id": node_id, "name": name})


@app.route("/api/nodes/remove", methods=["POST"])
def nodes_remove():
    d = request.get_json(force=True)
    get_db().execute("DELETE FROM nodes WHERE id = ?", (d.get("id"),))
    get_db().commit()
    return jsonify({"removed": True})


@app.route("/api/nodes/check", methods=["POST"])
@logged_tool("check_node")
def nodes_check():
    """Tool: check_node — ping a node's status_url, return live status + latency."""
    d = request.get_json(force=True)
    db = get_db()
    row = None
    if d.get("id"):
        row = db.execute("SELECT * FROM nodes WHERE id = ?", (d["id"],)).fetchone()
    elif d.get("name"):
        row = db.execute("SELECT * FROM nodes WHERE name = ?", (d["name"],)).fetchone()
    if not row:
        known = [r["name"] for r in db.execute("SELECT name FROM nodes")]
        return err(f"node not found. known nodes: {known}", 404)

    status, latency = "unconfigured", None
    if row["status_url"]:
        start = time.time()
        try:
            req = urllib.request.Request(row["status_url"], headers={"User-Agent": "core_server/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                latency = round((time.time() - start) * 1000)
                status = "online" if resp.status < 400 else "offline"
        except Exception:
            status = "unreachable"
    db.execute("UPDATE nodes SET status=?, latency=?, last_checked=? WHERE id=?",
               (status, latency, now_iso(), row["id"]))
    db.commit()
    return ok({"id": row["id"], "name": row["name"], "status": status, "latency": latency})


# ---------- tool: capture ----------

@app.route("/api/capture/add", methods=["POST"])
@logged_tool("capture_note")
def capture_add():
    """Tool: capture_note — append a tagged, timestamped note."""
    d = request.get_json(force=True)
    text = d.get("text")
    if not text:
        return err("text required")
    entry_id = str(uuid.uuid4())
    db = get_db()
    db.execute("INSERT INTO capture_entries (id, text, tag, source, ts) VALUES (?, ?, ?, ?, ?)",
               (entry_id, text, d.get("tag", ""), d.get("source", "manual"), now_iso()))
    db.commit()
    return ok({"id": entry_id})


@app.route("/api/capture/remove", methods=["POST"])
def capture_remove():
    d = request.get_json(force=True)
    get_db().execute("DELETE FROM capture_entries WHERE id = ?", (d.get("id"),))
    get_db().commit()
    return jsonify({"removed": True})


# ---------- tool: vault ----------

@app.route("/api/vault/add", methods=["POST"])
@logged_tool("add_bookmark")
def vault_add():
    """Tool: add_bookmark — save a tagged reference link."""
    d = request.get_json(force=True)
    name, url = d.get("name"), d.get("url")
    if not name or not url:
        return err("name and url required")
    link_id = str(uuid.uuid4())
    db = get_db()
    db.execute("INSERT INTO vault_links (id, name, url, tag) VALUES (?, ?, ?, ?)",
               (link_id, name, url, d.get("tag", "")))
    db.commit()
    return ok({"id": link_id})


@app.route("/api/vault/remove", methods=["POST"])
def vault_remove():
    d = request.get_json(force=True)
    get_db().execute("DELETE FROM vault_links WHERE id = ?", (d.get("id"),))
    get_db().commit()
    return jsonify({"removed": True})


# ---------- tool: audio ----------

@app.route("/api/audio/toggle", methods=["POST"])
@logged_tool("toggle_ambient")
def audio_toggle():
    """Tool: toggle_ambient — flip the ambient relay flag."""
    d = request.get_json(force=True) or {}
    db = get_db()
    if "enabled" in d:
        enabled = 1 if d["enabled"] else 0
    else:
        cur = db.execute("SELECT enabled FROM audio_state WHERE id = 1").fetchone()
        enabled = 0 if cur["enabled"] else 1
    db.execute("UPDATE audio_state SET enabled = ? WHERE id = 1", (enabled,))
    db.commit()
    return ok({"enabled": bool(enabled)})


# ---------- tool: ingest (web_capture.py / scan_to_cad.py feed in here) ----------

@app.route("/api/ingest", methods=["POST"])
@logged_tool("ingest_event")
def ingest():
    """Tool: ingest_event — store an externally-captured entry (or a batch)
    in the same shape everything else uses. web_capture.py's captures.jsonl
    lines POST here one at a time (or batched via {"entries": [...]})."""
    d = request.get_json(force=True)
    entries = d.get("entries") if "entries" in d else [d]
    db = get_db()
    count = 0
    persisted_events = []
    for e in entries:
        eid = e.get("id") or str(uuid.uuid4())
        exists = db.execute("SELECT 1 FROM events WHERE id = ?", (eid,)).fetchone()
        if exists:
            continue
        tag = e.get("tag", "")
        source = e.get("source", {})
        payload = e.get("payload", {})
        db.execute("INSERT INTO events (id, tag, ts, source_json, payload_json) VALUES (?, ?, ?, ?, ?)",
                   (eid, tag, e.get("timestamp", now_iso()), json.dumps(source), json.dumps(payload)))
        persisted_events.append({
            "id": eid,
            "event_type": e.get("event_type") or tag or (source.get("type") if isinstance(source, dict) else "") or "ingest",
            "tag": tag,
            "source": source,
            "payload": payload,
        })
        count += 1
    db.commit()

    # Once the general agent platform has been registered, every ordinary
    # ingest can also act as an event-bus observation. Duplicate ingests do
    # not re-trigger workflows because workflow_runs are unique by event/workflow
    # in the dispatch logic. This keeps web_capture.py and other existing feeders
    # compatible without forcing them onto a second ingestion API.
    matched = []
    platform = globals().get("agent_platform")
    if platform is not None:
        for event in persisted_events:
            matched.extend(platform.dispatch_event(event))
    return ok({"ingested": count, "skipped_duplicates": len(entries) - count, "matched_workflows": matched})


# ---------- tool: search ----------

@app.route("/api/search")
@logged_tool("search_memory")
def search():
    """Tool: search_memory — cross-reference capture entries, vault links,
    and ingested events by tag or keyword."""
    q = f"%{request.args.get('q', '')}%"
    db = get_db()
    entries = db.execute(
        "SELECT id, text, tag, source, ts FROM capture_entries WHERE text LIKE ? OR tag LIKE ? ORDER BY ts DESC LIMIT 50",
        (q, q)).fetchall()
    links = db.execute(
        "SELECT id, name, url, tag FROM vault_links WHERE name LIKE ? OR tag LIKE ?", (q, q)).fetchall()
    events = db.execute(
        "SELECT id, tag, ts, source_json, payload_json FROM events WHERE tag LIKE ? OR payload_json LIKE ? ORDER BY ts DESC LIMIT 50",
        (q, q)).fetchall()
    hypotheses = db.execute(
        "SELECT * FROM hypotheses WHERE claim LIKE ? OR domain LIKE ? OR falsification_criterion LIKE ? OR strongest_counterargument LIKE ? ORDER BY updated_at DESC LIMIT 50",
        (q, q, q, q)).fetchall()
    evidence_rows = db.execute(
        "SELECT * FROM evidence WHERE summary LIKE ? OR source_title LIKE ? OR source_url LIKE ? ORDER BY created_at DESC LIMIT 50",
        (q, q, q)).fetchall()
    questions = db.execute(
        "SELECT * FROM research_questions WHERE question LIKE ? OR domain LIKE ? OR rationale LIKE ? ORDER BY updated_at DESC LIMIT 50",
        (q, q, q)).fetchall()
    world = db.execute(
        "SELECT id,source_type,ts,title,text,url,author,topics_json FROM world_items WHERE title LIKE ? OR text LIKE ? OR author LIKE ? OR topics_json LIKE ? ORDER BY ts DESC LIMIT 50",
        (q, q, q, q)).fetchall()
    knowledge = db.execute(
        "SELECT id,source_type,title,source_url,artifact_path,topics_json,substr(text,1,5000) text,updated_at FROM knowledge_items WHERE title LIKE ? OR text LIKE ? OR topics_json LIKE ? ORDER BY updated_at DESC LIMIT 50",
        (q, q, q)).fetchall()
    return ok({
        "capture_entries": [dict(r) for r in entries],
        "vault_links": [dict(r) for r in links],
        "events": [{**dict(r), "source": json.loads(r["source_json"]), "payload": json.loads(r["payload_json"])}
                   for r in events],
        "research_questions": [dict(r) for r in questions],
        "hypotheses": [{**dict(r), "discovery_score": hypothesis_score(dict(r))} for r in hypotheses],
        "evidence": [dict(r) for r in evidence_rows],
        "world": [{**dict(r), "topics": json.loads(r["topics_json"] or "[]")} for r in world],
        "knowledge": [{**dict(r), "topics": json.loads(r["topics_json"] or "[]")} for r in knowledge],
    })


# ---------- full state, for the dashboard's one-shot load ----------

@app.route("/api/state")
def state():
    db = get_db()
    nodes = [dict(r) for r in db.execute("SELECT * FROM nodes")]
    capture = [dict(r) for r in db.execute("SELECT * FROM capture_entries ORDER BY ts DESC LIMIT 30")]
    vault = [dict(r) for r in db.execute("SELECT * FROM vault_links")]
    audio = dict(db.execute("SELECT * FROM audio_state WHERE id = 1").fetchone())
    event_count = db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    hypothesis_count = db.execute("SELECT COUNT(*) c FROM hypotheses").fetchone()["c"]
    queued_research = db.execute("SELECT COUNT(*) c FROM research_tasks WHERE status='queued'").fetchone()["c"]
    discovery_count = db.execute("SELECT COUNT(*) c FROM discoveries").fetchone()["c"]
    capabilities = [dict(r) for r in db.execute(
        "SELECT name,status,risk,last_test_ok,successful_runs,failed_runs,autonomous_allowed FROM capabilities ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()]
    goals = [dict(r) for r in db.execute(
        "SELECT id,title,status,priority,next_action FROM goals WHERE status!='archived' ORDER BY priority DESC,updated_at DESC LIMIT 12"
    ).fetchall()]
    workflows = [dict(r) for r in db.execute(
        "SELECT id,name,enabled,description FROM workflows ORDER BY updated_at DESC LIMIT 12"
    ).fetchall()]
    jobs = [{**dict(r), "result": json.loads(r["result_json"] or "null")} for r in db.execute(
        "SELECT id,kind,status,priority,objective,payload_json,result_json,updated_at FROM agent_jobs ORDER BY created_at DESC LIMIT 12"
    ).fetchall()]
    for job in jobs:
        payload = json.loads(job.pop("payload_json", None) or "{}")
        job["engine"] = payload.get("engine", "universal")
        job["browser_escalation"] = bool(payload.get("_browser_escalation_authorized"))
        job.pop("result_json", None)
    world_count = db.execute("SELECT COUNT(*) c FROM world_items").fetchone()["c"]
    knowledge_count = db.execute("SELECT COUNT(*) c FROM knowledge_items").fetchone()["c"]
    feed_count = db.execute("SELECT COUNT(*) c FROM source_feeds WHERE enabled=1").fetchone()["c"]
    model_count = db.execute("SELECT COUNT(*) c FROM models WHERE enabled=1").fetchone()["c"]
    models = [dict(r) for r in db.execute("SELECT name,provider,success_rate,runs,latency_ema_ms FROM models WHERE enabled=1 ORDER BY runs DESC,name LIMIT 8").fetchall()]
    forecasting = globals().get("forecasting_platform")
    forecast_summary = forecasting.summary() if forecasting is not None else {}
    deployment_path = Path(DB_PATH).resolve().parent / "deployment.json"
    try:
        deployment = json.loads(deployment_path.read_text(encoding="utf-8")) if deployment_path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        deployment = {}
    dsh_settings = deployment.get("dsh") or {}
    saved_loops = deployment.get("loops") or {}
    loop_frequencies = normalized_loop_frequencies(saved_loops)
    operator_context_path = Path(RESEARCH_REPO).resolve() / "OPERATOR_CONTEXT.md"
    try:
        operator_context = operator_context_path.read_text(encoding="utf-8")[:32000] if operator_context_path.is_file() else ""
    except (OSError, UnicodeDecodeError):
        operator_context = ""
    report_path = Path(DB_PATH).resolve().parent / "OVERNIGHT_REPORT.md"
    try:
        overnight_report = {
            "path": str(report_path), "updated_at": report_path.stat().st_mtime,
            "content": report_path.read_text(encoding="utf-8")[:200000],
        } if report_path.is_file() and report_path.stat().st_size <= 500_000 else {}
    except (OSError, UnicodeDecodeError):
        overnight_report = {}
    return jsonify({
        "nodes": nodes, "capture": capture, "vault": vault, "audio": audio, "event_count": event_count,
        "hypothesis_count": hypothesis_count, "queued_research": queued_research, "discovery_count": discovery_count,
        "capabilities": capabilities, "goals": goals, "workflows": workflows, "jobs": jobs,
        "world_count": world_count, "knowledge_count": knowledge_count, "feed_count": feed_count,
        "model_count": model_count, "models": models, "forecasting": forecast_summary,
        "overnight_report": overnight_report,
        "configuration": {"workspace": ROOT_DIR, "obsidian_vault": OBSIDIAN_VAULT, "research_repo": RESEARCH_REPO,
                          "executor_only": EXECUTOR_ONLY,
                          "repository_ready": (Path(ROOT_DIR).resolve() / ".git").exists(),
                          "operator_context_path": str(operator_context_path), "operator_context": operator_context,
                          "dsh_model": dsh_settings.get("model", ""),
                          "dsh_context_window": dsh_settings.get("context_window", 8192),
                          "dsh_reasoning_effort": dsh_settings.get("reasoning_effort", "default"),
                          "loop_frequencies": loop_frequencies,
                          "claude_mcp": _claude_mcp_status(),
                          "managed_secrets": _managed_secret_status()},
    })


@app.route("/api/owner/operator-context", methods=["POST"])
@logged_tool("update_operator_context")
def update_operator_context():
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    d = request.get_json(silent=True) or {}
    content = d.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > 64000:
        return err("operator context must be text under 64 KB")
    repository = Path(RESEARCH_REPO).resolve()
    target = repository / "OPERATOR_CONTEXT.md"
    if not repository.is_dir() or target.parent != repository:
        return err("configured research repository is unavailable")
    temporary = repository / ("OPERATOR_CONTEXT." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    universal_platform.store.event("configuration.operator_context_updated", {
        "actor": "owner", "path": str(target), "bytes": len(content.encode("utf-8")),
    })
    return ok({"path": str(target), "bytes": len(content.encode("utf-8"))})


def _pending_action(value):
    if isinstance(value, dict):
        pending = value.get("pending_action")
        if isinstance(pending, dict) and pending.get("tool") and isinstance(pending.get("args", {}), dict):
            return pending
        for child in value.values():
            found = _pending_action(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _pending_action(child)
            if found:
                return found
    return None


@app.route("/api/owner/jobs/approve-once", methods=["POST"])
@logged_tool("approve_job_action")
def approve_job_action():
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    job_id = str((request.get_json(silent=True) or {}).get("id") or "")
    row = get_db().execute("SELECT status,result_json FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] not in {"blocked", "awaiting_approval"}:
        return err("job is not waiting for approval", 409)
    try:
        result = json.loads(row["result_json"] or "{}")
    except json.JSONDecodeError:
        return err("job approval request is invalid", 409)
    pending = _pending_action(result)
    if not pending:
        return err("job has no exact pending tool action", 409)
    tool, args = str(pending["tool"]), pending.get("args") or {}
    grant = universal_platform.store.grant("assistant", tool, args, time.time() + 600, uses=1)
    resumed = universal_platform.store.control(job_id, "resume")
    universal_platform.store.event("permission.approved_once", {
        "actor": "owner", "job_id": job_id, "tool": tool, "args": args, "grant_id": grant["id"],
    })
    return ok({"grant": grant, "job": resumed})


@app.route("/api/owner/open-configured-path", methods=["POST"])
@logged_tool("open_configured_path")
def open_configured_path():
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    name = str((request.get_json(silent=True) or {}).get("name") or "")
    paths = {
        "repository": Path(ROOT_DIR).resolve(),
        "obsidian": Path(OBSIDIAN_VAULT).resolve(),
        "overnight_report": (Path(DB_PATH).resolve().parent / "OVERNIGHT_REPORT.md").resolve(),
    }
    target = paths.get(name)
    if target is None:
        return err("name must be repository, obsidian, or overnight_report", 400)
    if not target.exists():
        return err(f"configured path does not exist: {target}", 404)
    if os.name != "nt" or not hasattr(os, "startfile"):
        return err("opening configured paths is available on the Windows host", 501)
    os.startfile(str(target))
    universal_platform.store.event("owner.path_opened", {"actor": "owner", "name": name, "path": str(target)})
    return ok({"name": name, "path": str(target)})


@app.route("/api/owner/settings/paths", methods=["POST"])
def configure_owner_paths():
    """Stage an existing Git checkout and Obsidian folder for the next restart."""
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    d = request.get_json(silent=True) or {}
    repository_text = str(d.get("repository") or "").strip()
    vault_text = str(d.get("obsidian_vault") or "").strip()
    if not repository_text or not vault_text:
        return err("repository and obsidian_vault are required")
    repository = Path(repository_text).expanduser().resolve()
    vault = Path(vault_text).expanduser().resolve()
    if not repository.is_dir() or not (repository / ".git").exists():
        return err(f"Repository must be an existing Git checkout containing .git: {repository}")
    if not vault.is_dir():
        return err(f"Obsidian vault must be an existing folder: {vault}")
    if repository.parent == repository or vault.parent == vault:
        return err("A drive root cannot be used as the repository or vault")
    settings_path = Path(DB_PATH).resolve().parent / "deployment.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
        settings.update({"workspace": str(repository), "research_repo": str(repository),
                         "obsidian_vault": str(vault)})
        temporary = settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, settings_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return err(f"Could not save deployment settings: {exc}", 500)
    universal_platform.store.event("configuration.paths_staged", {
        "actor": "owner", "repository": str(repository), "obsidian_vault": str(vault),
        "restart_required": True,
    })
    return ok({"repository": str(repository), "obsidian_vault": str(vault), "restart_required": True})


@app.route("/api/owner/settings/dsh", methods=["POST"])
def configure_owner_dsh():
    """Stage the exact DSH Ollama model and bounded context for restart."""
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    d = request.get_json(silent=True) or {}
    model = str(d.get("model") or "").strip()
    if not model or len(model) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model):
        return err("model must be an exact Ollama tag")
    try:
        context_window = int(d.get("context_window", 8192))
    except (TypeError, ValueError):
        return err("context_window must be an integer")
    if not 2048 <= context_window <= 262144:
        return err("context_window must be between 2048 and 262144")
    reasoning_effort = str(d.get("reasoning_effort") or "default")
    if reasoning_effort not in {"default", "none", "low", "medium", "high"}:
        return err("reasoning_effort must be default, none, low, medium, or high")
    settings_path = Path(DB_PATH).resolve().parent / "deployment.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
        previous = settings.get("dsh") or {}
        settings["dsh"] = {**previous, "model": model, "context_window": context_window,
                           "reasoning_effort": reasoning_effort}
        temporary = settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, settings_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return err(f"Could not save DSH settings: {exc}", 500)
    universal_platform.store.event("configuration.dsh_staged", {
        "actor": "owner", "provider": "ollama", "model": model,
        "context_window": context_window, "reasoning_effort": reasoning_effort,
        "restart_required": True,
    })
    return ok({"provider": "ollama", "model": model, "context_window": context_window,
               "reasoning_effort": reasoning_effort, "restart_required": True})


@app.route("/api/owner/settings/loops", methods=["POST"])
def configure_owner_loops():
    """Validate and stage recurring worker frequencies for the next restart."""
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    d = request.get_json(silent=True) or {}
    values = {}
    for name, (minimum, maximum, default) in LOOP_FREQUENCIES.items():
        try:
            value = int(d.get(name, default))
        except (TypeError, ValueError):
            return err(f"{name} must be an integer number of seconds")
        if not minimum <= value <= maximum:
            return err(f"{name} must be between {minimum} and {maximum} seconds")
        values[name] = value
    settings_path = Path(DB_PATH).resolve().parent / "deployment.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
        settings["loops"] = values
        temporary = settings_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, settings_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return err(f"Could not save loop frequencies: {exc}", 500)
    universal_platform.store.event("configuration.loops_staged", {
        "actor": "owner", "frequencies": values, "restart_required": True,
    })
    return ok({"frequencies": values, "restart_required": True})


@app.route("/api/owner/integrations/claude-mcp", methods=["POST"])
def configure_claude_mcp():
    """Merge the local stdio bridge into Claude Desktop without replacing other servers."""
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    path = _claude_desktop_config_path()
    backup = None
    try:
        if path.is_file():
            if path.stat().st_size > 1_000_000:
                return err("Claude Desktop configuration exceeds 1 MB; refusing to modify it")
            raw = path.read_text(encoding="utf-8")
            config = json.loads(raw)
            if not isinstance(config, dict):
                return err("Claude Desktop configuration root must be a JSON object")
        else:
            raw, config = None, {}
        servers = config.get("mcpServers")
        if servers is None:
            servers = {}
        if not isinstance(servers, dict):
            return err("Claude Desktop mcpServers must be a JSON object")
        entry = _claude_mcp_entry()
        changed = servers.get("universal-assistant") != entry
        servers["universal-assistant"] = entry
        config["mcpServers"] = servers
        path.parent.mkdir(parents=True, exist_ok=True)
        if changed and raw is not None:
            backup = path.with_name(path.name + ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
            backup.write_text(raw, encoding="utf-8")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except json.JSONDecodeError as exc:
        return err(f"Claude Desktop configuration is invalid JSON and was not changed: {exc}")
    except OSError as exc:
        return err(f"Could not update Claude Desktop configuration: {exc}", 500)
    universal_platform.store.event("configuration.claude_mcp_updated", {
        "actor": "owner", "path": str(path), "changed": changed,
        "backup": str(backup) if backup else None, "claude_restart_required": True,
    })
    return ok({"path": str(path), "configured": True, "changed": changed,
               "backup": str(backup) if backup else None, "claude_restart_required": True})


@app.route("/api/owner/settings/secrets", methods=["POST"])
def configure_owner_secret():
    """Create, replace, or remove one managed secret without ever returning its value."""
    if getattr(g, "actor", None) != "owner":
        return err("owner access required", 403)
    d = request.get_json(silent=True) or {}
    name = str(d.get("name") or "").strip().upper()
    action = str(d.get("action") or "set").strip().lower()
    if name in PROTECTED_SECRET_NAMES:
        return err("core authentication secrets cannot be changed from the dashboard")
    if name not in MANAGED_SECRET_SPECS and not re.fullmatch(r"INTEGRATION_[A-Z0-9_]{3,52}", name):
        return err("choose a supported secret or use a name beginning with INTEGRATION_")
    try:
        records = _managed_secret_records()
        if action == "remove":
            removed = records.pop(name, None) is not None
            _write_managed_secrets(records)
            os.environ.pop(name, None)
            universal_platform.store.event("configuration.secret_removed", {
                "actor": "owner", "name": name, "removed": removed, "restart_required": True,
            })
            return ok({"name": name, "configured": False, "removed": removed, "restart_required": True})
        if action != "set":
            return err("action must be set or remove")
        value = d.get("value")
        if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > 8192:
            return err("secret value must be text under 8 KB")
        value = value.strip()
        minimum = int((MANAGED_SECRET_SPECS.get(name) or {}).get("minimum", 1))
        if len(value) < minimum:
            return err(f"{name} must be at least {minimum} characters")
        if name == "ALLOWED_CHAT_IDS" and not re.fullmatch(r"-?\d+(?:\s*,\s*-?\d+)*", value):
            return err("ALLOWED_CHAT_IDS must be one or more numeric chat IDs separated by commas")
        if name == "TELEGRAM_BOT_TOKEN" and not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{20,}", value):
            return err("TELEGRAM_BOT_TOKEN does not match Telegram's bot-token format")
        records[name] = {"value": value, "rotated_at": time.time()}
        _write_managed_secrets(records)
        # The core can use data/research keys immediately. Supervised child
        # services (notably Telegram) receive the new value after restart.
        os.environ[name] = value
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return err(f"Could not update managed secret: {exc}", 500)
    universal_platform.store.event("configuration.secret_updated", {
        "actor": "owner", "name": name, "restart_required": True,
    })
    return ok({"name": name, "configured": True, "restart_required": True})


# ---------- file ops (folded in from fileedit_app) ----------

@app.route("/api/files/list")
@logged_tool("list_files")
def files_list():
    """Tool: list_files"""
    target = safe_path(request.args.get("path", ""))
    if not os.path.isdir(target):
        return err("not a directory", 404)
    dirs, files = [], []
    for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
        if entry.name.startswith(".") or is_sensitive(entry.name):
            continue
        if entry.is_dir():
            dirs.append(entry.name)
        else:
            files.append({"name": entry.name, "runnable": Path(entry.name).suffix in RUNNERS})
    return ok({"path": request.args.get("path", ""), "dirs": dirs, "files": files})


@app.route("/api/files/read")
@logged_tool("read_file")
def files_read():
    """Tool: read_file"""
    target = safe_path(request.args.get("path", ""))
    if not os.path.isfile(target):
        return err("not a file", 404)
    return ok({"path": request.args.get("path", ""), "content": Path(target).read_text(errors="replace")})


@app.route("/api/files/save", methods=["POST"])
@logged_tool("save_file")
def files_save():
    """Tool: save_file"""
    d = request.get_json(force=True)
    target = safe_path(d.get("path", ""))
    Path(target).write_text(d.get("content", ""))
    return ok({"saved": True})


def build_safe_env():
    return {k: v for k, v in os.environ.items() if k in SAFE_ENV_ALLOWLIST}


@app.route("/api/files/run", methods=["POST"])
@logged_tool("run_file")
def files_run():
    """Tool: run_file"""
    d = request.get_json(force=True)
    target = safe_path(d.get("path", ""))
    ext = Path(target).suffix
    if ext not in RUNNERS:
        return err(f"no runner configured for {ext} files")
    try:
        proc = subprocess.run(RUNNERS[ext] + [os.path.basename(target)], cwd=os.path.dirname(target),
                               capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
                               env=build_safe_env())
        return ok({"returncode": proc.returncode,
                   "stdout": (proc.stdout or "")[:MAX_OUTPUT_CHARS],
                   "stderr": (proc.stderr or "")[:MAX_OUTPUT_CHARS]})
    except subprocess.TimeoutExpired:
        return err(f"timed out after {RUN_TIMEOUT_SECONDS}s", 408)


@app.route("/api/actions")
def actions():
    """Tool: list_actions — review recent tool calls (what ran, args, result, success)."""
    limit = min(int(request.args.get("limit", 50)), 200)
    db = get_db()
    rows = db.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return ok([{**dict(r), "args": json.loads(r["args_json"]), "result": json.loads(r["result_json"] or "null"),
                "ok": bool(r["ok"])} for r in rows])


# ---------- tool: research ----------

def check_robots_allowed(url: str, user_agent: str = "core_server/1.0") -> bool:
    """Same courtesy check as web_capture.py: if a site's robots.txt
    disallows the path, don't fetch it — regardless of what's being looked
    up or why. Unreachable robots.txt defaults to allowed, not blocked."""
    try:
        validate_public_url(url)
    except ValueError:
        return False
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = safe_get(robots_url, headers={"User-Agent": user_agent}, timeout=8)
        rp.parse(resp.text.splitlines())
    except Exception:
        return True
    return rp.can_fetch(user_agent, url)


def search_ddg(query: str, count: int):
    """Scrapes DuckDuckGo's no-JS HTML endpoint. No API key, but no
    contract either — this is the one part of this project I could not
    execute-test against the live target (sandboxed, no general internet
    access). The result__a / result__snippet class names have been stable
    on this endpoint for years, but if DDG changes their markup this will
    quietly return zero results rather than erroring — that's the specific
    failure mode to check for if this tool seems to stop working."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    if not check_robots_allowed(url):
        return None, "robots.txt disallows this path"
    try:
        resp = safe_get(url, headers={"User-Agent": "Mozilla/5.0 (core_server research tool)"}, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return None, f"search request failed: {e}"
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for r in soup.select(".result")[:count]:
        a = r.select_one(".result__a")
        snippet = r.select_one(".result__snippet")
        if not a:
            continue
        results.append({
            "title": a.get_text(strip=True),
            "url": a.get("href", ""),
            "snippet": snippet.get_text(strip=True) if snippet else "",
        })
    return results, None


def search_brave(query: str, count: int):
    if not BRAVE_API_KEY:
        return None, "SEARCH_BACKEND=brave but BRAVE_API_KEY is not set"
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": count})
    try:
        resp = safe_get(url, headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None, f"Brave API request failed: {e}"
    results = [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
               for r in data.get("web", {}).get("results", [])[:count]]
    return results, None


@app.route("/api/research/search")
@logged_tool("web_search")
def research_search():
    """Tool: web_search — search the web, returns titles/urls/snippets."""
    query = request.args.get("q", "")
    count = min(int(request.args.get("count", 5)), 15)
    if not query:
        return err("q required")
    fn = search_brave if SEARCH_BACKEND == "brave" else search_ddg
    results, error = fn(query, count)
    if error:
        return err(error, 502)
    try:
        configured_cost = os.environ.get("BRAVE_COST_PER_REQUEST_USD") if SEARCH_BACKEND == "brave" else "0"
        universal_platform.store.meter(SEARCH_BACKEND, "web_search", cost_usd=float(configured_cost) if configured_cost else None)
    except Exception:
        pass  # Search succeeded; missing telemetry must not cause a costly retry.
    return ok({"query": query, "backend": SEARCH_BACKEND, "results": results})


@app.route("/api/research/read")
@logged_tool("read_page")
def research_read():
    """Tool: read_page — fetch a URL and return its readable text content.
    Static/server-rendered pages only — this does not run JavaScript. For
    a page that needs JS to render its content, use web_capture.py's
    recipe system instead (it drives a real browser)."""
    url = request.args.get("url", "")
    if not url:
        return err("url required")
    try:
        validate_public_url(url)
    except ValueError as exc:
        return err(str(exc), 400)
    if not check_robots_allowed(url):
        return err("robots.txt disallows this page", 403)
    try:
        resp = safe_get(url, headers={"User-Agent": "Mozilla/5.0 (core_server research tool)"}, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        return err(f"fetch failed: {e}", 502)

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "title"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return ok({"url": url, "title": title, "text": text[:READ_PAGE_MAX_CHARS], "truncated": len(text) > READ_PAGE_MAX_CHARS})



# ---------- discovery engine ----------

VALID_STANCES = {"support", "contradict", "neutral"}
VALID_HYPOTHESIS_STATUS = {"candidate", "investigating", "supported", "rejected", "dormant"}


def clamp01(value, default=0.5):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def make_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def row_dict(row):
    return dict(row) if row else None


def hypothesis_score(h):
    """Priority/value score, not probability-of-truth. Confidence is only one
    factor so novel, high-impact but uncertain ideas can still earn research."""
    confidence = clamp01(h.get("confidence"), 0.25)
    novelty = clamp01(h.get("novelty"))
    importance = clamp01(h.get("importance"))
    testability = clamp01(h.get("testability"))
    actionability = clamp01(h.get("actionability"))
    consensus = clamp01(h.get("consensus_estimate"))
    divergence = 1.0 - consensus
    return round(10.0 * (
        0.18 * confidence + 0.22 * novelty + 0.24 * importance +
        0.16 * testability + 0.10 * actionability + 0.10 * divergence
    ), 3)


def recompute_confidence(hypothesis_id: str):
    """Conservative evidence update. Evidence weight is tempered by source
    reliability and independence; contradictory evidence subtracts. This is
    deliberately not presented as formal Bayesian inference."""
    db = get_db()
    h = db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    if not h:
        return None
    evs = db.execute("SELECT * FROM evidence WHERE hypothesis_id=?", (hypothesis_id,)).fetchall()
    prior = clamp01(h["prior"], 0.25)
    signal = 0.0
    for e in evs:
        strength = clamp01(e["weight"]) * (0.4 + 0.6 * clamp01(e["reliability"])) * (0.5 + 0.5 * clamp01(e["independence"]))
        if e["stance"] == "support":
            signal += strength
        elif e["stance"] == "contradict":
            signal -= strength
    # Bounded, intentionally cautious update: +/- 4 strong independent items
    # can move confidence substantially, but no pile of evidence makes it 0/1.
    confidence = max(0.02, min(0.98, prior + 0.14 * signal))
    db.execute("UPDATE hypotheses SET confidence=?, updated_at=? WHERE id=?", (confidence, now_iso(), hypothesis_id))
    db.execute("INSERT INTO hypothesis_history (id,hypothesis_id,ts,confidence,status,reason) VALUES (?,?,?,?,?,?)",
               (make_id("HH"), hypothesis_id, now_iso(), confidence, h["status"], "evidence recompute"))
    db.commit()
    return round(confidence, 4)


def render_hypothesis_markdown(hypothesis_id: str) -> str | None:
    db = get_db()
    h = db.execute("SELECT * FROM hypotheses WHERE id=?", (hypothesis_id,)).fetchone()
    if not h:
        return None
    evidence_rows = db.execute("SELECT * FROM evidence WHERE hypothesis_id=? ORDER BY created_at", (hypothesis_id,)).fetchall()
    predictions_rows = db.execute("SELECT * FROM predictions WHERE hypothesis_id=? ORDER BY created_at", (hypothesis_id,)).fetchall()
    experiments_rows = db.execute("SELECT * FROM experiments WHERE hypothesis_id=? ORDER BY created_at", (hypothesis_id,)).fetchall()
    hd = dict(h)
    score = hypothesis_score(hd)
    def q(v):
        return json.dumps(v or "", ensure_ascii=False)
    lines = [
        "---",
        f"id: {hd['id']}",
        f"domain: {q(hd.get('domain'))}",
        f"status: {hd.get('status')}",
        f"prior: {hd.get('prior')}",
        f"confidence: {hd.get('confidence')}",
        f"novelty: {hd.get('novelty')}",
        f"importance: {hd.get('importance')}",
        f"testability: {hd.get('testability')}",
        f"actionability: {hd.get('actionability')}",
        f"consensus_estimate: {hd.get('consensus_estimate')}",
        f"discovery_score: {score}",
        f"question_id: {q(hd.get('question_id'))}",
        f"updated_at: {q(hd.get('updated_at'))}",
        "---", "", f"# {hd['id']} — Hypothesis", "", hd["claim"], "",
        "## Falsification criterion", "", hd.get("falsification_criterion") or "Not recorded.", "",
        "## Strongest counterargument", "", hd.get("strongest_counterargument") or "Not recorded.", "",
        "## Next decisive test", "", hd.get("next_test") or "Not recorded.", "",
        "## Evidence", ""
    ]
    if not evidence_rows:
        lines.append("No evidence recorded yet.")
    for e in evidence_rows:
        marker = {"support": "+", "contradict": "-", "neutral": "~"}.get(e["stance"], "~")
        src = f" — [{e['source_title'] or e['source_url']}]({e['source_url']})" if e["source_url"] else ""
        lines.append(f"- **{marker} {e['stance']}** ({e['reliability']:.2f} reliability): {e['summary']}{src}")
    lines += ["", "## Predictions", ""]
    if not predictions_rows:
        lines.append("No predictions recorded yet.")
    for pr in predictions_rows:
        due = f"; due {pr['due_at']}" if pr["due_at"] else ""
        lines.append(f"- **{pr['status']}** ({pr['probability']:.2f}{due}): {pr['prediction']}")
    lines += ["", "## Experiments", ""]
    if not experiments_rows:
        lines.append("No experiments recorded yet.")
    for ex in experiments_rows:
        art = f" — `{ex['artifact_path']}`" if ex["artifact_path"] else ""
        lines.append(f"- **{ex['status']}**: {ex['name']}{art}")
    return "\n".join(lines) + "\n"


def ensure_obsidian_path(kind: str) -> Path:
    folder = Path(OBSIDIAN_VAULT, DISCOVERY_VAULT_DIR, kind)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def sync_hypothesis_to_obsidian(hypothesis_id: str):
    content = render_hypothesis_markdown(hypothesis_id)
    if content is None:
        return None
    path = ensure_obsidian_path("Hypotheses") / f"{hypothesis_id}.md"
    path.write_text(content, encoding="utf-8")
    return str(path)


@app.route("/api/discovery/questions", methods=["POST"])
@logged_tool("create_question")
def discovery_create_question():
    d = request.get_json(force=True)
    question = (d.get("question") or "").strip()
    if not question:
        return err("question required")
    qid = make_id("Q")
    now = now_iso()
    get_db().execute("INSERT INTO research_questions (id,question,domain,status,priority,rationale,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (qid, question, d.get("domain", ""), "open", clamp01(d.get("priority")), d.get("rationale", ""), now, now))
    get_db().commit()
    return ok({"id": qid, "question": question})


@app.route("/api/discovery/hypotheses", methods=["POST", "GET"])
@logged_tool("create_hypothesis")
def discovery_hypotheses():
    db = get_db()
    if request.method == "GET":
        status = request.args.get("status")
        limit = min(int(request.args.get("limit", 50)), 200)
        if status:
            rows = db.execute("SELECT * FROM hypotheses WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM hypotheses ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return ok([{**dict(r), "discovery_score": hypothesis_score(dict(r))} for r in rows])
    d = request.get_json(force=True)
    claim = (d.get("claim") or "").strip()
    if not claim:
        return err("claim required")
    required_reasoning = ("falsification_criterion", "strongest_counterargument", "next_test")
    if any(not str(d.get(field) or "").strip() for field in required_reasoning):
        return err("falsification_criterion, strongest_counterargument, and next_test are required")
    question_id = d.get("question_id") or None
    if question_id and not db.execute("SELECT 1 FROM research_questions WHERE id=?", (question_id,)).fetchone():
        return err("question_id not found", 404)
    hid = make_id("H")
    prior = clamp01(d.get("prior"), 0.25)
    now = now_iso()
    vals = (hid, question_id, claim, d.get("domain", ""), "candidate", prior, prior,
            clamp01(d.get("novelty")), clamp01(d.get("importance")), clamp01(d.get("testability")),
            clamp01(d.get("actionability")), clamp01(d.get("consensus_estimate")),
            d.get("falsification_criterion", ""), d.get("strongest_counterargument", ""), d.get("next_test", ""), now, now)
    db.execute("""INSERT INTO hypotheses
        (id,question_id,claim,domain,status,prior,confidence,novelty,importance,testability,actionability,consensus_estimate,
         falsification_criterion,strongest_counterargument,next_test,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
    db.execute("INSERT INTO hypothesis_history (id,hypothesis_id,ts,confidence,status,reason) VALUES (?,?,?,?,?,?)",
               (make_id("HH"), hid, now, prior, "candidate", "created"))
    db.commit()
    path = sync_hypothesis_to_obsidian(hid) if d.get("sync_obsidian", True) else None
    return ok({"id": hid, "confidence": prior, "obsidian_path": path})


@app.route("/api/discovery/hypotheses/<hid>", methods=["GET", "POST"])
@logged_tool("update_hypothesis")
def discovery_hypothesis(hid):
    db = get_db()
    row = db.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if not row:
        return err("hypothesis not found", 404)
    if request.method == "GET":
        evidence_rows = db.execute("SELECT * FROM evidence WHERE hypothesis_id=? ORDER BY created_at DESC", (hid,)).fetchall()
        prediction_rows = db.execute("SELECT * FROM predictions WHERE hypothesis_id=? ORDER BY created_at DESC", (hid,)).fetchall()
        task_rows = db.execute("SELECT * FROM research_tasks WHERE hypothesis_id=? ORDER BY priority DESC", (hid,)).fetchall()
        result = {**dict(row), "discovery_score": hypothesis_score(dict(row)),
                  "evidence": [dict(r) for r in evidence_rows], "predictions": [dict(r) for r in prediction_rows],
                  "research_tasks": [{**dict(r), "result": json.loads(r["result_json"] or "null")} for r in task_rows]}
        return ok(result)
    d = request.get_json(force=True)
    allowed = {"claim", "domain", "status", "prior", "confidence", "novelty", "importance", "testability",
               "actionability", "consensus_estimate", "falsification_criterion", "strongest_counterargument", "next_test"}
    updates, vals = [], []
    for key in allowed:
        if key not in d:
            continue
        value = d[key]
        if key == "status":
            if value not in VALID_HYPOTHESIS_STATUS:
                return err(f"invalid status; choose one of {sorted(VALID_HYPOTHESIS_STATUS)}")
        elif key in {"prior", "confidence", "novelty", "importance", "testability", "actionability", "consensus_estimate"}:
            value = clamp01(value)
        updates.append(f"{key}=?")
        vals.append(value)
    if not updates:
        return err("no supported fields supplied")
    updates.append("updated_at=?"); vals.append(now_iso()); vals.append(hid)
    db.execute(f"UPDATE hypotheses SET {', '.join(updates)} WHERE id=?", vals)
    new = db.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    db.execute("INSERT INTO hypothesis_history (id,hypothesis_id,ts,confidence,status,reason) VALUES (?,?,?,?,?,?)",
               (make_id("HH"), hid, now_iso(), new["confidence"], new["status"], d.get("reason", "manual/model update")))
    db.commit()
    path = sync_hypothesis_to_obsidian(hid) if d.get("sync_obsidian", True) else None
    return ok({**dict(new), "discovery_score": hypothesis_score(dict(new)), "obsidian_path": path})


@app.route("/api/discovery/evidence", methods=["POST"])
@logged_tool("add_evidence")
def discovery_add_evidence():
    d = request.get_json(force=True)
    hid = d.get("hypothesis_id")
    summary = (d.get("summary") or "").strip()
    stance = d.get("stance", "neutral")
    if not hid or not summary:
        return err("hypothesis_id and summary required")
    if stance not in VALID_STANCES:
        return err(f"stance must be one of {sorted(VALID_STANCES)}")
    source_url = str(d.get("source_url") or "").strip()
    parsed_source = urllib.parse.urlsplit(source_url)
    remote_source = parsed_source.scheme in {"http", "https"} and bool(parsed_source.netloc)
    local_source = parsed_source.scheme in {"workspace", "obsidian"} and bool(parsed_source.path or parsed_source.netloc)
    if not (remote_source or local_source):
        return err("source_url must be a retrieved http(s), workspace://, or obsidian:// reference")
    db = get_db()
    if not db.execute("SELECT 1 FROM hypotheses WHERE id=?", (hid,)).fetchone():
        return err("hypothesis not found", 404)
    eid = make_id("E")
    db.execute("""INSERT INTO evidence
        (id,hypothesis_id,stance,summary,source_url,source_title,source_type,reliability,independence,weight,observed_at,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, hid, stance, summary, source_url, d.get("source_title", ""), d.get("source_type", "web"),
         clamp01(d.get("reliability")), clamp01(d.get("independence")), clamp01(d.get("weight")),
         d.get("observed_at") or now_iso(), now_iso()))
    db.commit()
    confidence = recompute_confidence(hid)
    path = sync_hypothesis_to_obsidian(hid)
    return ok({"id": eid, "hypothesis_id": hid, "new_confidence": confidence, "obsidian_path": path})


@app.route("/api/discovery/predictions", methods=["POST"])
@logged_tool("record_prediction")
def discovery_add_prediction():
    d = request.get_json(force=True)
    hid, prediction = d.get("hypothesis_id"), (d.get("prediction") or "").strip()
    if not hid or not prediction:
        return err("hypothesis_id and prediction required")
    db = get_db()
    if not db.execute("SELECT 1 FROM hypotheses WHERE id=?", (hid,)).fetchone():
        return err("hypothesis not found", 404)
    pid = make_id("P")
    db.execute("INSERT INTO predictions (id,hypothesis_id,prediction,due_at,status,outcome,probability,created_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (pid, hid, prediction, d.get("due_at"), "open", None, clamp01(d.get("probability")), now_iso(), None))
    db.commit(); sync_hypothesis_to_obsidian(hid)
    return ok({"id": pid})


@app.route("/api/discovery/predictions/<pid>/resolve", methods=["POST"])
@logged_tool("resolve_prediction")
def discovery_resolve_prediction(pid):
    d = request.get_json(force=True)
    status = d.get("status")
    if status not in {"correct", "incorrect", "mixed", "unresolved"}:
        return err("status must be correct, incorrect, mixed, or unresolved")
    db = get_db()
    row = db.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
    if not row:
        return err("prediction not found", 404)
    db.execute("UPDATE predictions SET status=?, outcome=?, resolved_at=? WHERE id=?",
               (status, d.get("outcome", ""), now_iso(), pid))
    db.commit(); sync_hypothesis_to_obsidian(row["hypothesis_id"])
    platform = globals().get("agent_platform")
    event = None
    if platform is not None:
        event = platform.publish_event(
            "prediction.resolved", tag="prediction", source={"type": "discovery_engine"},
            payload={"prediction_id": pid, "hypothesis_id": row["hypothesis_id"], "status": status, "outcome": d.get("outcome", "")},
        )
    return ok({"id": pid, "status": status, "event": event})


@app.route("/api/discovery/consensus", methods=["POST"])
@logged_tool("record_consensus")
def discovery_record_consensus():
    d = request.get_json(force=True)
    claim = (d.get("claim") or "").strip()
    if not claim:
        return err("claim required")
    cid, now = make_id("C"), now_iso()
    urls = d.get("source_urls", [])
    get_db().execute("INSERT INTO consensus_claims (id,domain,claim,estimate,basis,source_urls_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, d.get("domain", ""), claim, clamp01(d.get("estimate")), d.get("basis", ""), json.dumps(urls), now, now))
    get_db().commit()
    return ok({"id": cid})


@app.route("/api/discovery/tasks", methods=["POST", "GET"])
@logged_tool("research_queue")
def discovery_tasks():
    db = get_db()
    if request.method == "GET":
        status = request.args.get("status", "queued")
        limit = min(int(request.args.get("limit", 20)), 100)
        rows = db.execute("SELECT * FROM research_tasks WHERE status=? ORDER BY priority DESC, created_at ASC LIMIT ?", (status, limit)).fetchall()
        return ok([{**dict(r), "result": json.loads(r["result_json"] or "null")} for r in rows])
    d = request.get_json(force=True)
    task = (d.get("task") or "").strip()
    if not task:
        return err("task required")
    eig = clamp01(d.get("expected_information_gain"))
    cost = max(0.05, clamp01(d.get("estimated_cost")))
    explicit = d.get("priority")
    priority = clamp01(explicit) if explicit is not None else round(eig / (cost + 0.25), 4)
    tid, now = make_id("T"), now_iso()
    db.execute("""INSERT INTO research_tasks
        (id,hypothesis_id,question_id,task,task_type,status,expected_information_gain,estimated_cost,priority,result_json,created_at,updated_at)
        VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?)""",
        (tid, d.get("hypothesis_id"), d.get("question_id"), task, d.get("task_type", "research"), eig, cost, priority, None, now, now))
    db.commit()
    return ok({"id": tid, "priority": priority})


@app.route("/api/discovery/tasks/<tid>/complete", methods=["POST"])
@logged_tool("complete_research_task")
def discovery_complete_task(tid):
    d = request.get_json(force=True)
    db = get_db()
    if not db.execute("SELECT 1 FROM research_tasks WHERE id=?", (tid,)).fetchone():
        return err("task not found", 404)
    status = d.get("status", "done")
    if status not in {"done", "failed", "blocked"}:
        return err("status must be done, failed, or blocked")
    db.execute("UPDATE research_tasks SET status=?, result_json=?, updated_at=? WHERE id=?",
               (status, json.dumps(d.get("result")), now_iso(), tid))
    db.commit()
    return ok({"id": tid, "status": status})


@app.route("/api/discovery/experiments", methods=["POST"])
@logged_tool("create_experiment")
def discovery_create_experiment():
    d = request.get_json(force=True)
    hid, name = d.get("hypothesis_id"), (d.get("name") or "").strip()
    if not hid or not name:
        return err("hypothesis_id and name required")
    db = get_db()
    if not db.execute("SELECT 1 FROM hypotheses WHERE id=?", (hid,)).fetchone():
        return err("hypothesis not found", 404)
    xid, now = make_id("EXP"), now_iso()
    artifact = d.get("artifact_path") or f"experiments/{hid}/{xid}"
    db.execute("INSERT INTO experiments (id,hypothesis_id,name,method,artifact_path,status,result_summary,result_json,created_at,updated_at) VALUES (?,?,?,?,?,'planned',?,?,?,?)",
               (xid, hid, name, d.get("method", ""), artifact, None, None, now, now))
    db.commit(); sync_hypothesis_to_obsidian(hid)
    return ok({"id": xid, "artifact_path": artifact})


@app.route("/api/discovery/experiments/<xid>/result", methods=["POST"])
@logged_tool("record_experiment_result")
def discovery_experiment_result(xid):
    d = request.get_json(force=True)
    db = get_db(); row = db.execute("SELECT * FROM experiments WHERE id=?", (xid,)).fetchone()
    if not row:
        return err("experiment not found", 404)
    status = d.get("status", "completed")
    db.execute("UPDATE experiments SET status=?, result_summary=?, result_json=?, updated_at=? WHERE id=?",
               (status, d.get("summary", ""), json.dumps(d.get("result")), now_iso(), xid))
    db.commit(); sync_hypothesis_to_obsidian(row["hypothesis_id"])
    return ok({"id": xid, "status": status})


@app.route("/api/discovery/hypothesis")
@logged_tool("get_hypothesis")
def discovery_get_hypothesis_tool():
    hid = request.args.get("hid")
    if not hid:
        return err("hid required")
    db = get_db()
    row = db.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if not row:
        return err("hypothesis not found", 404)
    evidence_rows = db.execute("SELECT * FROM evidence WHERE hypothesis_id=? ORDER BY created_at DESC", (hid,)).fetchall()
    prediction_rows = db.execute("SELECT * FROM predictions WHERE hypothesis_id=? ORDER BY created_at DESC", (hid,)).fetchall()
    experiment_rows = db.execute("SELECT * FROM experiments WHERE hypothesis_id=? ORDER BY created_at DESC", (hid,)).fetchall()
    return ok({**dict(row), "discovery_score": hypothesis_score(dict(row)),
               "evidence": [dict(r) for r in evidence_rows],
               "predictions": [dict(r) for r in prediction_rows],
               "experiments": [{**dict(r), "result": json.loads(r["result_json"] or "null")} for r in experiment_rows]})


@app.route("/api/discovery/hypothesis/update", methods=["POST"])
@logged_tool("update_hypothesis")
def discovery_hypothesis_update_tool():
    d = request.get_json(force=True)
    hid = d.get("hid")
    if not hid:
        return err("hid required")
    # Reuse the same implementation under a path that the current Telegram
    # dispatcher can call without URL-template substitution.
    return discovery_hypothesis.__wrapped__(hid) if hasattr(discovery_hypothesis, "__wrapped__") else discovery_hypothesis(hid)


@app.route("/api/discovery/prediction/resolve", methods=["POST"])
@logged_tool("resolve_prediction")
def discovery_prediction_resolve_tool():
    d = request.get_json(force=True)
    pid = d.get("pid")
    if not pid:
        return err("pid required")
    return discovery_resolve_prediction.__wrapped__(pid) if hasattr(discovery_resolve_prediction, "__wrapped__") else discovery_resolve_prediction(pid)


@app.route("/api/discovery/task/complete", methods=["POST"])
@logged_tool("complete_research_task")
def discovery_task_complete_tool():
    d = request.get_json(force=True)
    tid = d.get("tid")
    if not tid:
        return err("tid required")
    return discovery_complete_task.__wrapped__(tid) if hasattr(discovery_complete_task, "__wrapped__") else discovery_complete_task(tid)


@app.route("/api/discovery/experiment/result", methods=["POST"])
@logged_tool("record_experiment_result")
def discovery_experiment_result_tool():
    d = request.get_json(force=True)
    xid = d.get("xid")
    if not xid:
        return err("xid required")
    return discovery_experiment_result.__wrapped__(xid) if hasattr(discovery_experiment_result, "__wrapped__") else discovery_experiment_result(xid)


@app.route("/api/discovery/rank")
@logged_tool("rank_hypotheses")
def discovery_rank():
    limit = min(int(request.args.get("limit", 20)), 100)
    rows = get_db().execute("SELECT * FROM hypotheses WHERE status NOT IN ('rejected')").fetchall()
    ranked = sorted(({**dict(r), "discovery_score": hypothesis_score(dict(r))} for r in rows), key=lambda x: x["discovery_score"], reverse=True)
    return ok(ranked[:limit])


@app.route("/api/discovery/discoveries", methods=["POST", "GET"])
@logged_tool("create_discovery")
def discovery_discoveries():
    db = get_db()
    if request.method == "GET":
        limit = min(int(request.args.get("limit", 30)), 100)
        rows = db.execute("SELECT * FROM discoveries ORDER BY score DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()
        return ok([dict(r) for r in rows])
    d = request.get_json(force=True)
    title, summary = (d.get("title") or "").strip(), (d.get("summary") or "").strip()
    if not title or not summary:
        return err("title and summary required")
    did, now = make_id("D"), now_iso()
    score = float(d.get("score", 0.0))
    db.execute("INSERT INTO discoveries (id,hypothesis_id,title,summary,score,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
               (did, d.get("hypothesis_id"), title, summary, score, d.get("status", "candidate"), now, now))
    db.commit()
    folder = ensure_obsidian_path("Discoveries")
    (folder / f"{did}.md").write_text(f"---\nid: {did}\nscore: {score}\nstatus: {d.get('status','candidate')}\n---\n\n# {title}\n\n{summary}\n", encoding="utf-8")
    platform = globals().get("agent_platform")
    event = None
    if platform is not None:
        event = platform.publish_event(
            "discovery.created", tag="discovery", source={"type": "discovery_engine"},
            payload={"discovery_id": did, "hypothesis_id": d.get("hypothesis_id"), "title": title, "summary": summary, "score": score},
        )
    return ok({"id": did, "event": event})


@app.route("/api/discovery/sync", methods=["POST"])
@logged_tool("sync_obsidian")
def discovery_sync_obsidian():
    rows = get_db().execute("SELECT id FROM hypotheses").fetchall()
    paths = [sync_hypothesis_to_obsidian(r["id"]) for r in rows]
    return ok({"synced": len(paths), "vault": OBSIDIAN_VAULT, "paths": paths[:20]})


def git_run(args, cwd=RESEARCH_REPO):
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30,
                           env={**build_safe_env(), "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME, "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
                                "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME, "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL})
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


@app.route("/api/discovery/git/status")
@logged_tool("research_git_status")
def discovery_git_status():
    if not os.path.isdir(os.path.join(RESEARCH_REPO, ".git")):
        return err(f"RESEARCH_REPO is not a git repository: {RESEARCH_REPO}", 404)
    rc, out, stderr = git_run(["status", "--short", "--branch"])
    if rc:
        return err(stderr or "git status failed", 500)
    return ok({"repo": RESEARCH_REPO, "status": out})


@app.route("/api/discovery/git/commit", methods=["POST"])
@logged_tool("commit_research")
def discovery_git_commit():
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()
    paths = d.get("paths") or []
    if not message:
        return err("message required")
    if not os.path.isdir(os.path.join(RESEARCH_REPO, ".git")):
        return err(f"RESEARCH_REPO is not a git repository: {RESEARCH_REPO}", 404)
    # Explicit relative paths only; never 'git add -A', so unrelated/private
    # files cannot be silently swept into an autonomous research commit.
    if not paths or not isinstance(paths, list):
        return err("paths must be a non-empty list of relative repo paths")
    safe = []
    repo_real = os.path.realpath(RESEARCH_REPO)
    for rel in paths:
        target = os.path.realpath(os.path.join(repo_real, str(rel)))
        if target != repo_real and not target.startswith(repo_real + os.sep):
            return err(f"path escapes research repo: {rel}", 403)
        if is_sensitive(target):
            return err(f"refusing sensitive path: {rel}", 403)
        safe.append(str(rel))
    rc, _, stderr = git_run(["add", "--"] + safe)
    if rc:
        return err(stderr or "git add failed", 500)
    rc, out, stderr = git_run(["commit", "-m", message])
    if rc:
        return err(stderr or out or "git commit failed", 500)
    rc2, sha, _ = git_run(["rev-parse", "HEAD"])
    return ok({"commit": sha if rc2 == 0 else None, "message": message, "paths": safe})



def confined_context_path(base: str, rel_path: str) -> str:
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, (rel_path or "").lstrip("/")))
    if target != base_real and not target.startswith(base_real + os.sep):
        abort(403, "path escapes context root")
    if is_sensitive(target):
        abort(403, "path matches a protected pattern")
    return target


def search_text_tree(base: str, query: str, limit=30):
    root = Path(base)
    if not root.exists():
        return []
    q = query.lower()
    results = []
    allowed_ext = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml", ".js", ".ts", ".tsx", ".jsx"}
    excluded = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
                ".mypy_cache", ".ruff_cache", "self_improvement_copies", "worktrees"}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name.lower() not in excluded and not name.startswith(".")]
        for filename in filenames:
            if len(results) >= limit:
                return results
            path = Path(directory) / filename
            if path.suffix.lower() not in allowed_ext or is_sensitive(str(path)):
                continue
            try:
                if path.is_symlink() or path.stat().st_size > 500_000:
                    continue
                text = path.read_text(errors="replace")
            except OSError:
                continue
            idx = text.lower().find(q)
            if idx < 0:
                continue
            start, end = max(0, idx - 220), min(len(text), idx + len(query) + 420)
            results.append({"path": str(path.relative_to(root)), "snippet": " ".join(text[start:end].split())})
    return results


@app.route("/api/context/search")
@logged_tool("search_context")
def context_search():
    query = (request.args.get("q") or "").strip()
    source = request.args.get("source", "both")
    limit = min(int(request.args.get("limit", 20)), 50)
    if not query:
        return err("q required")
    if source not in {"obsidian", "research_repo", "both"}:
        return err("source must be obsidian, research_repo, or both")
    result = {}
    if source in {"obsidian", "both"}:
        result["obsidian"] = search_text_tree(OBSIDIAN_VAULT, query, limit)
    if source in {"research_repo", "both"}:
        result["research_repo"] = search_text_tree(RESEARCH_REPO, query, limit)
    return ok(result)


@app.route("/api/context/read")
@logged_tool("read_context_file")
def context_read():
    source = request.args.get("source", "")
    rel = request.args.get("path", "")
    if source == "obsidian":
        base = OBSIDIAN_VAULT
    elif source == "research_repo":
        base = RESEARCH_REPO
    else:
        return err("source must be obsidian or research_repo")
    target = confined_context_path(base, rel)
    if not os.path.isfile(target):
        return err("context file not found", 404)
    text = Path(target).read_text(errors="replace")
    return ok({"source": source, "path": rel, "content": text[:20000], "truncated": len(text) > 20000})


@app.route("/api/discovery/brief")
@logged_tool("discovery_brief")
def discovery_brief():
    db = get_db()
    hypotheses = [dict(r) for r in db.execute("SELECT * FROM hypotheses WHERE status NOT IN ('rejected')").fetchall()]
    ranked = sorted(({**h, "discovery_score": hypothesis_score(h)} for h in hypotheses), key=lambda x: x["discovery_score"], reverse=True)[:5]
    tasks = [dict(r) for r in db.execute("SELECT * FROM research_tasks WHERE status='queued' ORDER BY priority DESC LIMIT 5").fetchall()]
    unresolved = [dict(r) for r in db.execute("SELECT * FROM predictions WHERE status='open' ORDER BY due_at IS NULL, due_at LIMIT 5").fetchall()]
    return ok({"top_hypotheses": ranked, "next_tasks": tasks, "open_predictions": unresolved})


# ---------- general agent platform ----------
# Dynamic capabilities, context brokering, goals, event-driven workflows, and
# durable assistant jobs live in agent_platform.py so the core does not become
# an unmaintainable single file. The extension uses this same SQLite DB and
# auth/action-ledger conventions, and active generated tools are appended to
# /api/tools at request time.

init_db()

agent_platform = AgentPlatform(
    app=app,
    db_path=DB_PATH,
    root_dir=ROOT_DIR,
    obsidian_vault=OBSIDIAN_VAULT,
    research_repo=RESEARCH_REPO,
    get_db=get_db,
    ok=ok,
    err=err,
    logged_tool=logged_tool,
    now_iso=now_iso,
    max_output_chars=MAX_OUTPUT_CHARS,
)

# Intelligence/runtime extension: model+harness routing metadata, worker profiles,
# evaluations, normalized world context, durable knowledge ingestion, skills, and
# self-observation. It shares the same SQLite database and event bus.
intelligence_platform = IntelligencePlatform(
    app=app,
    db_path=DB_PATH,
    get_db=get_db,
    ok=ok,
    err=err,
    logged_tool=logged_tool,
    now_iso=now_iso,
    publish_event=agent_platform.publish_event,
)


mesh_platform = MeshPlatform(app=app, get_db=get_db, ok=ok, err=err, logged_tool=logged_tool)
vault_platform = VaultPlatform(app=app, ok=ok, err=err, logged_tool=logged_tool, vault_path=OBSIDIAN_VAULT)

# ---------- tool: system telemetry ----------

@app.route("/api/system/metrics")
@logged_tool("get_system_metrics")
def get_system_metrics():
    """Tool: get_system_metrics — read-only CPU/RAM/disk/GPU telemetry."""
    return ok(system_snapshot())


@app.route("/api/system/health")
@logged_tool("get_system_health")
def get_system_health():
    """Tool: get_system_health — summarize telemetry against warning thresholds."""
    return ok(system_health())


SYSTEM_TOOLS = [
    {"name": "get_system_metrics", "description": "Read current host and NVIDIA GPU telemetry: CPU, RAM, disk, GPU temperature, utilization, VRAM, power, fan, and pstate.",
     "method": "GET", "path": "/api/system/metrics",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_system_health", "description": "Check current system/GPU telemetry against configured warning thresholds and return active alerts.",
     "method": "GET", "path": "/api/system/health",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


# ---------- tool schema, for the LLM side ----------

TOOLS = [
    {"name": "check_node", "description": "Check live status and latency of a mesh node by name.",
     "method": "POST", "path": "/api/nodes/check",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "capture_note", "description": "Save a tagged, timestamped note to the capture log.",
     "method": "POST", "path": "/api/capture/add",
     "input_schema": {"type": "object", "properties": {
         "text": {"type": "string"}, "tag": {"type": "string"}}, "required": ["text"]}},
    {"name": "add_bookmark", "description": "Save a tagged reference link to the vault.",
     "method": "POST", "path": "/api/vault/add",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "url": {"type": "string"}, "tag": {"type": "string"}},
         "required": ["name", "url"]}},
    {"name": "toggle_ambient", "description": "Turn the ambient audio relay on or off.",
     "method": "POST", "path": "/api/audio/toggle",
     "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": []}},
    {"name": "search_memory", "description": "Search capture notes, vault links, and ingested events by keyword or tag.",
     "method": "GET", "path": "/api/search",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "ingest_event", "description": "Store an externally-sourced entry (e.g. from a scraper or scan) in the shared memory store.",
     "method": "POST", "path": "/api/ingest",
     "input_schema": {"type": "object", "properties": {
         "tag": {"type": "string"}, "source": {"type": "object"}, "payload": {"type": "object"}},
         "required": ["payload"]}},
    {"name": "list_files", "description": "List files and folders under a path in the project root.",
     "method": "GET", "path": "/api/files/list",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"name": "read_file", "description": "Read a text file's contents.",
     "method": "GET", "path": "/api/files/read",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "save_file", "description": "Write text content to a file, overwriting it.",
     "method": "POST", "path": "/api/files/save",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "run_file", "description": "Execute a .py/.sh/.js file and return its output.",
     "method": "POST", "path": "/api/files/run",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_actions", "description": "Review recently executed tool calls and their results.",
     "method": "GET", "path": "/api/actions",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}},
    {"name": "web_search", "description": "Search the web. Returns titles, URLs, and snippets.",
     "method": "GET", "path": "/api/research/search",
     "input_schema": {"type": "object", "properties": {
         "q": {"type": "string"}, "count": {"type": "integer"}}, "required": ["q"]}},
    {"name": "read_page", "description": "Fetch a URL and return its readable text content (static pages only, no JS rendering).",
     "method": "GET", "path": "/api/research/read",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "create_question", "description": "Create a durable research question for autonomous investigation.",
     "method": "POST", "path": "/api/discovery/questions",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}, "domain": {"type": "string"}, "priority": {"type": "number"}, "rationale": {"type": "string"}}, "required": ["question"]}},
    {"name": "create_hypothesis", "description": "Create a falsifiable hypothesis with a falsification criterion, strongest counterargument, and concrete next test.",
     "method": "POST", "path": "/api/discovery/hypotheses",
     "input_schema": {"type": "object", "properties": {"question_id": {"type": "string"}, "claim": {"type": "string"}, "domain": {"type": "string"}, "prior": {"type": "number"}, "novelty": {"type": "number"}, "importance": {"type": "number"}, "testability": {"type": "number"}, "actionability": {"type": "number"}, "consensus_estimate": {"type": "number"}, "falsification_criterion": {"type": "string"}, "strongest_counterargument": {"type": "string"}, "next_test": {"type": "string"}}, "required": ["claim", "falsification_criterion", "strongest_counterargument", "next_test"]}},
    {"name": "get_hypothesis", "description": "Read a hypothesis together with its evidence, predictions, experiments, and discovery score.",
     "method": "GET", "path": "/api/discovery/hypothesis",
     "input_schema": {"type": "object", "properties": {"hid": {"type": "string"}}, "required": ["hid"]}},
    {"name": "update_hypothesis", "description": "Update a hypothesis after research, including its status, confidence, falsification criterion, counterargument, or next test.",
     "method": "POST", "path": "/api/discovery/hypothesis/update",
     "input_schema": {"type": "object", "properties": {"hid": {"type": "string"}, "status": {"type": "string"}, "confidence": {"type": "number"}, "strongest_counterargument": {"type": "string"}, "next_test": {"type": "string"}, "reason": {"type": "string"}}, "required": ["hid"]}},
    {"name": "add_evidence", "description": "Attach evidence using a retrieved http(s), workspace://, or obsidian:// source reference; confidence is conservatively recomputed.",
     "method": "POST", "path": "/api/discovery/evidence",
     "input_schema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "stance": {"type": "string", "enum": ["support", "contradict", "neutral"]}, "summary": {"type": "string"}, "source_url": {"type": "string"}, "source_title": {"type": "string"}, "source_type": {"type": "string"}, "reliability": {"type": "number"}, "independence": {"type": "number"}, "weight": {"type": "number"}}, "required": ["hypothesis_id", "stance", "summary", "source_url"]}},
    {"name": "record_prediction", "description": "Record a concrete future prediction implied by a hypothesis so the system can later score calibration.",
     "method": "POST", "path": "/api/discovery/predictions",
     "input_schema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "prediction": {"type": "string"}, "due_at": {"type": "string"}, "probability": {"type": "number"}}, "required": ["hypothesis_id", "prediction"]}},
    {"name": "resolve_prediction", "description": "Resolve a previous prediction as correct, incorrect, mixed, or unresolved.",
     "method": "POST", "path": "/api/discovery/prediction/resolve",
     "input_schema": {"type": "object", "properties": {"pid": {"type": "string"}, "status": {"type": "string", "enum": ["correct", "incorrect", "mixed", "unresolved"]}, "outcome": {"type": "string"}}, "required": ["pid", "status"]}},
    {"name": "record_consensus", "description": "Store the current consensus claim and estimated prevalence before searching for non-consensus alternatives.",
     "method": "POST", "path": "/api/discovery/consensus",
     "input_schema": {"type": "object", "properties": {"domain": {"type": "string"}, "claim": {"type": "string"}, "estimate": {"type": "number"}, "basis": {"type": "string"}, "source_urls": {"type": "array", "items": {"type": "string"}}}, "required": ["claim"]}},
    {"name": "research_queue", "description": "Create a research task, prioritized by expected information gain versus cost.",
     "method": "POST", "path": "/api/discovery/tasks",
     "input_schema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "question_id": {"type": "string"}, "task": {"type": "string"}, "task_type": {"type": "string"}, "expected_information_gain": {"type": "number"}, "estimated_cost": {"type": "number"}, "priority": {"type": "number"}}, "required": ["task"]}},
    {"name": "complete_research_task", "description": "Mark a queued research task done, failed, or blocked and store its structured result.",
     "method": "POST", "path": "/api/discovery/task/complete",
     "input_schema": {"type": "object", "properties": {"tid": {"type": "string"}, "status": {"type": "string"}, "result": {}}, "required": ["tid"]}},
    {"name": "create_experiment", "description": "Create a reproducible experiment linked to a hypothesis and a GitHub/local-repo artifact path.",
     "method": "POST", "path": "/api/discovery/experiments",
     "input_schema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "name": {"type": "string"}, "method": {"type": "string"}, "artifact_path": {"type": "string"}}, "required": ["hypothesis_id", "name"]}},
    {"name": "record_experiment_result", "description": "Store the result of a hypothesis experiment.",
     "method": "POST", "path": "/api/discovery/experiment/result",
     "input_schema": {"type": "object", "properties": {"xid": {"type": "string"}, "status": {"type": "string"}, "summary": {"type": "string"}, "result": {}}, "required": ["xid"]}},
    {"name": "rank_hypotheses", "description": "Rank active hypotheses by confidence, novelty, importance, testability, actionability, and consensus divergence.",
     "method": "GET", "path": "/api/discovery/rank",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}},
    {"name": "create_discovery", "description": "Promote a sufficiently supported hypothesis into a durable discovery candidate.",
     "method": "POST", "path": "/api/discovery/discoveries",
     "input_schema": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"}, "score": {"type": "number"}, "status": {"type": "string"}}, "required": ["title", "summary"]}},
    {"name": "sync_obsidian", "description": "Regenerate human-readable Obsidian hypothesis notes from canonical SQLite research state.",
     "method": "POST", "path": "/api/discovery/sync",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "research_git_status", "description": "Show the local research Git repository status.",
     "method": "GET", "path": "/api/discovery/git/status",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "commit_research", "description": "Commit explicitly named, non-sensitive research artifact paths to the local Git repository. Does not push.",
     "method": "POST", "path": "/api/discovery/git/commit",
     "input_schema": {"type": "object", "properties": {"message": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["message", "paths"]}},
    {"name": "search_context", "description": "Keyword-search the Obsidian vault and/or local research Git repository for prior notes, code, experiments, and context. Use short literal terms, not regular expressions.",
     "method": "GET", "path": "/api/context/search",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}, "source": {"type": "string", "enum": ["obsidian", "research_repo", "both"]}, "limit": {"type": "integer"}}, "required": ["q"]}},
    {"name": "read_context_file", "description": "Read a non-sensitive file from the Obsidian vault or local research Git repository.",
     "method": "GET", "path": "/api/context/read",
     "input_schema": {"type": "object", "properties": {"source": {"type": "string", "enum": ["obsidian", "research_repo"]}, "path": {"type": "string"}}, "required": ["source", "path"]}},
    {"name": "discovery_brief", "description": "Get the current top hypotheses, highest-priority queued research tasks, and unresolved predictions.",
     "method": "GET", "path": "/api/discovery/brief",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
] + PLATFORM_TOOLS + INTELLIGENCE_TOOLS + FORECAST_TOOLS + SYSTEM_TOOLS + MESH_TOOLS + VAULT_TOOLS

universal_platform = UniversalPlatform(app, DB_PATH, ROOT_DIR, API_KEY,
                                       lambda: TOOLS + agent_platform.dynamic_tools())
agent_platform.runtime_store = universal_platform.store
agent_platform.knowledge_store = universal_platform.knowledge

forecasting_platform = ForecastingPlatform(
    app=app,
    db_path=DB_PATH,
    get_db=get_db,
    ok=ok,
    err=err,
    logged_tool=logged_tool,
    now_iso=now_iso,
    publish_event=agent_platform.publish_event,
    queue_job=agent_platform._queue_job,
    verify_artifact=universal_platform.work.verify_artifact,
)


@app.route("/api/tools")
def tools():
    # Generated capabilities become tools without restarting telegram_agent.
    # Their paths are concrete (/api/capabilities/<actual-name>/invoke), not
    # templates, so the existing dispatcher can call them literally.
    return jsonify(universal_platform.catalog())


# ---------- frontend ----------

LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>core</title>
<style>
body{background:#1C1B19;color:#EDEAE2;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
form{background:#242320;border:1px solid #3A372F;padding:24px;width:280px;}
input{width:100%;padding:10px;margin-top:10px;background:#2B2A25;border:1px solid #3A372F;
  color:#EDEAE2;font-size:14px;box-sizing:border-box;}
button{width:100%;margin-top:14px;padding:10px;background:#4FD8C4;border:none;
  color:#10201D;font-weight:600;cursor:pointer;}
.err{color:#D6534A;font-size:13px;}
h2{font-size:16px;font-weight:500;margin:0;}
</style></head><body>
<form method="POST">
<h2>core_server</h2>
{{ERROR}}
<input type="password" name="password" placeholder="password" autofocus>
<button type="submit">enter</button>
</form>
</body></html>"""

MAIN_PAGE = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Command Center</title>
<style>
:root{--bg:#1C1B19;--panel:#242320;--panel2:#2B2A25;--line:#3A372F;--text:#EDEAE2;--dim:#93897E;
  --teal:#4FD8C4;--amber:#E3A73D;--red:#D6534A;--grey:#5C594F;
  --mono:ui-monospace,'Cascadia Code',Consolas,monospace;--sans:system-ui,'Segoe UI',sans-serif;}
*{box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:var(--sans);margin:0;}
#app{max-width:1100px;margin:0 auto;padding:20px 16px 56px;}
header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
  background:var(--panel);border:1px solid var(--line);margin-bottom:16px;}
header a{color:var(--dim);font-size:12px;text-decoration:none;}
.evc{font-family:var(--mono);font-size:12px;color:var(--dim);}
.search-bar{margin-bottom:12px;}
.input{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  font-family:var(--sans);font-size:14px;padding:10px;width:100%;}
.panel{background:var(--panel);border:1px solid var(--line);margin-bottom:10px;}
.panel h3{margin:0;padding:12px 16px;font-size:14px;font-weight:500;border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;color:var(--text);}
.panel h3 span{font-family:var(--mono);font-size:11px;color:var(--dim);}
.panel .body{padding:10px 16px;display:flex;flex-direction:column;gap:6px;}
.row{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:13px;
  padding:6px 0;border-bottom:1px solid var(--line);}
.row:last-child{border-bottom:none;}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
.dot-online{background:var(--teal);} .dot-offline{background:var(--red);}
.dot-unreachable{background:var(--amber);} .dot-unconfigured{background:var(--grey);}
.tag{font-family:var(--mono);font-size:10px;color:var(--amber);border:1px solid var(--line);padding:1px 6px;}
a.link{color:var(--teal);text-decoration:none;flex:1;}
.hint{font-size:12px;color:var(--dim);margin:4px 0;}
.ghost{background:none;border:1px dashed var(--line);color:var(--dim);font-family:var(--mono);
  font-size:12px;padding:8px;cursor:pointer;width:100%;margin-top:4px;}
.ghost:hover{border-color:var(--teal);color:var(--teal);}
.icon{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;}
.toggle{font-family:var(--mono);font-size:12px;padding:4px 12px;border:1px solid var(--line);
  background:var(--panel2);color:var(--dim);cursor:pointer;}
.toggle.on{border-color:var(--teal);color:var(--teal);}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px;align-items:start;}
.good{color:var(--teal)} .warn{color:var(--amber)} .bad{color:var(--red)}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions textarea,.actions button{grid-column:1/-1}
.preset-row{grid-column:1/-1;display:grid;grid-template-columns:minmax(130px,1fr) minmax(130px,1fr) auto auto;gap:6px}.preset-row button{grid-column:auto;padding:7px 10px}
.audit-item{font-family:var(--mono);font-size:12px;border-bottom:1px solid var(--line);padding:7px 0;}
.audit-item summary{cursor:pointer;color:var(--text);display:flex;gap:8px;align-items:center;}
.audit-item pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel2);padding:9px;color:var(--dim);max-height:420px;overflow:auto;}
.workbench{display:grid;grid-template-columns:210px minmax(0,1fr) 290px;gap:12px;align-items:start;}
.side-panel{position:sticky;top:10px;background:var(--panel);border:1px solid var(--line);padding:12px;max-height:calc(100vh - 20px);overflow:auto;}
.brand{font-family:var(--mono);font-size:15px;color:var(--teal);margin-bottom:4px}.brand-sub{font-size:11px;color:var(--dim);margin-bottom:14px;}
.nav-list{display:flex;flex-direction:column}.nav-button{display:flex;width:100%;gap:8px;align-items:center;border:0;border-left:2px solid transparent;background:none;color:var(--dim);padding:9px 10px;text-align:left;cursor:pointer;font-family:var(--mono);}
.nav-button:hover,.nav-button.active{color:var(--text);background:var(--panel2);border-left-color:var(--teal);}
.view-title{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}.view-title h2{font-size:18px;margin:0}.view-title p{font-size:12px;color:var(--dim);margin:4px 0 0;}
.session{border:1px solid var(--line);background:var(--panel);margin-bottom:10px}.session-head{display:flex;gap:8px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;color:var(--dim);}
.message{padding:12px 14px;border-bottom:1px solid var(--line);white-space:pre-wrap;line-height:1.45;font-size:13px}.message:last-child{border-bottom:0}.message.user{background:var(--panel2)}.message.assistant{border-left:2px solid var(--teal)}
.message-label{display:block;font-family:var(--mono);font-size:10px;color:var(--amber);margin-bottom:6px;text-transform:uppercase;}.composer{position:sticky;bottom:8px;z-index:3;box-shadow:0 -10px 30px var(--bg)}
.composer-close{font-size:20px;line-height:1;padding:0 2px}.composer-launcher{position:sticky;bottom:8px;z-index:3;background:var(--panel);box-shadow:0 -10px 30px var(--bg)}
.explain{font-size:12px;line-height:1.45;color:var(--dim);margin:3px 0 8px}.path-value{font-family:var(--mono);font-size:11px;word-break:break-all;color:var(--text)}
.consent{grid-column:1/-1;display:flex;gap:8px;align-items:flex-start;padding:9px;border:1px solid var(--line);background:var(--panel2);font-size:12px;color:var(--dim)}.consent input{margin-top:2px}.inline-link{display:inline-block;text-align:center;text-decoration:none}
.metric{padding:8px;background:var(--panel2);border:1px solid var(--line);margin-bottom:6px}.metric b{display:block;font-size:11px;color:var(--amber);font-family:var(--mono);margin-bottom:3px}.metric span{font-size:12px;word-break:break-word}
.guide-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:9px}.guide-card{background:var(--panel);border:1px solid var(--line);padding:12px}.guide-card h4{margin:0 0 6px;color:var(--teal)}.guide-card p{font-size:12px;color:var(--dim);line-height:1.45;margin:0}
code{font-family:var(--mono);color:var(--teal)}
textarea{width:100%;min-height:60px;background:var(--panel2);border:1px solid var(--line);
  color:var(--text);font-family:var(--sans);font-size:13px;padding:8px;margin-top:6px;}
@media(max-width:900px){.workbench{grid-template-columns:160px minmax(0,1fr)}.inspector{position:static;grid-column:1/-1;max-height:none}}
@media(max-width:650px){.actions{grid-template-columns:1fr}.preset-row{grid-template-columns:1fr}.grid{display:block}.panel{margin-bottom:10px}.workbench{display:block}.side-panel{position:static;max-height:none;margin-bottom:10px}.nav-list{flex-direction:row;overflow:auto}.nav-button{min-width:max-content;border-left:0;border-bottom:2px solid transparent}.nav-button.active{border-bottom-color:var(--teal)}.composer{position:static}}
</style></head><body>
<div id="app">
<header><strong>Universal Harness</strong><span class="evc">work · context · systems · audit</span><a href="/logout">log out</a></header>
<div class="search-bar"><input id="search" class="input" placeholder="Search notes, reference links, and events..."></div>
<div id="results"></div>
<div id="panels"></div>
</div>
<script>
let state = null;
let platform = {};
let systemHealth = {};
let auditRecords = [];
let activeView = 'work';
let composerOpen = localStorage.getItem('universalComposerOpen') !== 'false';
let reportOpen = localStorage.getItem('universalOvernightReportOpen') !== 'false';
let selectedPreset = localStorage.getItem('universalPromptPreset') || '';

async function api(path, opts){
  try {
    const res = await fetch(path, opts);
    if(res.status === 401){ location.href = '/login'; return null; }
    const contentType = res.headers.get('content-type') || '';
    if(!contentType.includes('application/json')) return {ok:false,error:{message:`HTTP ${res.status}`}};
    return await res.json();
  } catch(error) {
    return {ok:false,error:{message:String(error)}};
  }
}

async function load(){
  const [base, statusEnvelope, healthEnvelope, auditEnvelope] = await Promise.all([
    api('/api/state'), api('/api/platform/status'), api('/api/system/health'), api('/api/owner/audit?limit=100')
  ]);
  if(!base || !Array.isArray(base.nodes)) {
    document.getElementById('panels').innerHTML = '<div class="panel"><div class="body"><p class="bad">The core state API is unavailable. Check the launcher window and core log.</p></div></div>';
    return;
  }
  state = base;
  platform = (statusEnvelope && statusEnvelope.result) || statusEnvelope || {};
  systemHealth = (healthEnvelope && healthEnvelope.result) || healthEnvelope || {};
  auditRecords = (auditEnvelope && auditEnvelope.result) || [];
  render();
}

function render(){
  const online = state.nodes.filter(n => n.status === 'online').length;
  const maintenance = platform.maintenance || {};
  const services = maintenance.services || [];
  const readiness = maintenance.readiness || {};
  // Compatibility aliases keep old saved dashboard payloads renderable while
  // the visible product uses browser-free search and static HTTPS retrieval.
  readiness.playwright_chromium_ready = Boolean(readiness.free_web_retrieval_ready);
  readiness.approved_browser_routes = 0;
  const host = (systemHealth.snapshot || {}).host || {};
  const spending = platform.spending || {};
  const jobCounts = platform.jobs || {};
  const forecasting = state.forecasting || {};
  const configuration = state.configuration || {};
  const managedSecrets = configuration.managed_secrets || [];
  const claudeMcp = configuration.claude_mcp || {};
  const overnightReport = state.overnight_report || {};
  const trading = platform.trading || {datasets:[],count:0,live_trading_enabled:false};
  const latestStockDataset = (trading.datasets||[]).find(item=>item.dataset==='sp100_stocks');
  const latestBacktest = (trading.datasets||[]).find(item=>item.dataset==='stock_edge_backtest');
  const backtest = (latestBacktest||{}).backtest || null;
  const holdout = (backtest||{}).holdout || {};
  const paper = trading.kalshi_paper || null;
  const latestKalshi = (trading.datasets||[]).find(item=>String(item.dataset||'').startsWith('kalshi_'));
  const kalshiMarketRows = ((latestKalshi||{}).markets||[]).slice(0,12).map(row=>`<div class="row"><span style="flex:1"><b>${esc(row.ticker||'')}</b><br><span class="explain">${esc(row.title||'')}</span></span><span>Y ${row.yes_ask==null?'?':'$'+Number(row.yes_ask).toFixed(2)} · N ${row.no_ask==null?'?':'$'+Number(row.no_ask).toFixed(2)}</span>${paper?`<button class="toggle" onclick="selectKalshi('${encodeURIComponent(String(row.ticker||''))}','yes')">YES</button><button class="toggle" onclick="selectKalshi('${encodeURIComponent(String(row.ticker||''))}','no')">NO</button>`:''}</div>`).join('');
  const candidateRows = ((backtest||{}).candidates||[]).map(row=>`<tr><td>${esc(row.strategy||'')}</td><td>${esc(String((row.train||{}).trades||0))}</td><td>${Number((row.train||{}).expectancy_bps||0).toFixed(2)} bps</td><td>${esc(String((row.test||{}).trades||0))}</td><td>${Number((row.test||{}).expectancy_bps||0).toFixed(2)} bps</td><td>${Number((row.test||{}).profit_factor||0).toFixed(2)}</td></tr>`).join('');
  const latestFinished = state.jobs.find(j => ['done','failed','blocked','awaiting_approval'].includes(j.status));
  const views = {
    work: `<div class="view-title"><div><h2>Work sessions</h2><p>Each request becomes a durable, logged session. Open a session for its answer, tools, and verification.</p></div><span class="tag">${state.jobs.filter(j => ['queued','running'].includes(j.status)).length} active</span></div>
      ${state.jobs.map(j => `<article class="session"><div class="session-head"><span class="tag">${esc(j.status)}</span><span class="tag">${esc(j.engine || 'universal')}</span><span>${esc(j.id)}</span><span style="margin-left:auto">${esc(j.updated_at || '')}</span></div><div class="message user"><span class="message-label">You</span>${esc(j.objective)}</div><div class="message assistant"><span class="message-label">Assistant</span>${esc(jobOutput(j))}</div>${jobDetails(j)}</article>`).join('') || '<div class="panel"><div class="body"><p class="hint">No sessions yet. Start one below.</p></div></div>'}
      ${composerOpen ? `<div class="panel composer"><h3>New session <span>durable + audited <button class="icon composer-close" onclick="setComposerOpen(false)" title="Close new session panel" aria-label="Close new session panel">&times;</button></span></h3><div class="body actions"><textarea id="jobObjective" placeholder="Describe the result you want. Include files, constraints, and how success should be checked."></textarea><select id="jobEngine" class="input" onchange="updateEngineHelp()"><option value="universal">Universal durable agent</option><option value="dsh">DSH coding agent</option></select><select id="jobTemplate" class="input" onchange="updateTemplateHelp()"><option value="">general</option><option value="coding">coding</option><option value="research">research</option><option value="forecast">forecast</option><option value="impact">impact</option><option value="office">office</option><option value="browser">browser</option><option value="operations">operations</option></select><select id="jobPriority" class="input"><option value="0.5">normal priority</option><option value="0.9">high priority</option><option value="0.1">low priority</option></select><select id="approvalMode" class="input"><option value="suggest">suggest — approve every side effect</option><option value="auto_edit" selected>auto-edit — local file edits run</option><option value="full_auto">full-auto — use existing grants automatically</option></select><p id="engineHelp" class="explain">Runs through leases, checkpoints, permission grants, verified tools, and the full audit trail.</p><p id="templateHelp" class="explain">General answers and mixed tasks. The assistant chooses only relevant tools.</p><label class="consent"><input id="autoRepairOnFailure" type="checkbox" checked><span><b>Automatically diagnose harness failures.</b><br>Deterministic stuck-agent failures may queue a repair in an isolated source copy. Suggest mode still requires your grant, execution still requires an existing grant, and patches are never promoted automatically.</span></label><label class="consent"><input id="browserEscalation" type="checkbox" ${((platform.model_routes||[]).some(p=>p.enabled && p.type==='playwright' && p.transport_policy==='approved_browser'))?'':'disabled'}><span><b>Allow approved browser fallback for this job.</b><br>This may send the objective, bounded conversation, tool schemas, and tool results to a third party. It never enables policy-blocked providers. ${((platform.model_routes||[]).some(p=>p.enabled && p.type==='playwright' && p.transport_policy==='approved_browser'))?'':'No approved browser route is configured.'}</span></label><button class="ghost" onclick="queueJob()">Run as new logged job</button></div></div>` : `<button class="ghost composer-launcher" onclick="setComposerOpen(true)">+ New session</button>`}`,
    context: `<div class="view-title"><div><h2>Workspace & context</h2><p>The repository is the editable boundary. The vault supplies durable notes and context.</p></div></div><div class="grid"><div class="panel"><h3>Connected folders <span>saved for restart</span></h3><div class="body"><label class="explain" for="repositoryPath">Editable Git repository</label><input id="repositoryPath" class="input" value="${esc(configuration.workspace || '')}" placeholder="C:\\path\\to\\repository"><label class="explain" for="vaultPath">Existing Obsidian vault</label><input id="vaultPath" class="input" value="${esc(configuration.obsidian_vault || '')}" placeholder="C:\\path\\to\\vault"><button class="ghost" onclick="savePaths()">Save folders for next restart</button><p class="explain">The repository must contain <code>.git</code>. Saving does not interrupt running jobs; restart the pilot to activate the new boundary. Terminal fallback: <code>START_PILOT.cmd configure</code>.</p></div></div><div class="panel"><h3>DSH + Ollama <span>saved for restart</span></h3><div class="body"><label class="explain" for="dshModel">Exact installed Ollama tag</label><input id="dshModel" class="input" value="${esc(configuration.dsh_model || readiness.dsh_model || 'qwen3.5:4b')}" placeholder="qwen3.5:4b"><label class="explain" for="dshContext">Context window</label><input id="dshContext" class="input" type="number" min="2048" max="262144" value="${esc(configuration.dsh_context_window || 8192)}"><label class="explain" for="dshReasoning">Reasoning effort</label><select id="dshReasoning" class="input">${['default','none','low','medium','high'].map(value=>`<option value="${value}" ${value===(configuration.dsh_reasoning_effort||'default')?'selected':''}>${value}</option>`).join('')}</select><button class="ghost" onclick="saveDshSettings()">Save DSH model + context</button><p class="explain">Use <code>default</code> unless the selected model advertises effort levels. The integrated launch adds a final DSH settings layer with provider <code>ollama</code>, avoiding the stale global provider/model pairing. An external Ollama server must already use the same context setting or be restarted under this launcher.</p></div></div><div class="panel"><h3>Operator context <span>always loaded</span></h3><div class="body"><p class="explain">Edit the priorities and constraints every autonomous goal and job should consider. This adds context, never permissions.</p><textarea id="operatorContext" style="min-height:220px" placeholder="# Operator context\n\nCurrent priorities...">${esc(configuration.operator_context || '')}</textarea><button class="ghost" onclick="saveOperatorContext()">Save operator context</button><button class="ghost" onclick="queueGoalPlanning()">Derive goals from this context</button><button class="ghost" onclick="queueSelfImprovement()">Scan source and repair an isolated copy</button><p class="path-value">${esc(configuration.operator_context_path || '')}</p></div></div><div class="panel"><h3>Quick capture <span>${state.capture.length} recent</span></h3><div class="body"><p class="explain">Save a short owner note. This is separate from files in the linked Obsidian vault.</p><input id="captureTag" class="input" placeholder="tag"><textarea id="captureText" placeholder="Type a note..."></textarea><button class="ghost" onclick="addCapture()">Save note</button>${state.capture.map(c => `<div class="row"><span class="tag">${esc(c.tag||'—')}</span><span style="flex:1">${esc(c.text)}</span><button class="icon" onclick="removeCapture('${c.id}')">&times;</button></div>`).join('')}</div></div><div class="panel"><h3>Reference links <span>${state.vault.length}</span></h3><div class="body"><p class="explain">Bookmarks for people using the dashboard; these are not the Obsidian folder connection.</p>${state.vault.map(l => `<div class="row"><span class="tag">${esc(l.tag||'—')}</span><a class="link" href="${esc(l.url)}" target="_blank">${esc(l.name)}</a><button class="icon" onclick="removeVault('${l.id}')">&times;</button></div>`).join('') || '<p class="hint">No links yet.</p>'}<button class="ghost" onclick="addVault()">Add reference link</button></div></div></div>`,
    systems: `<div class="view-title"><div><h2>Systems & capabilities</h2><p>Runtime health, models, DSH, browser transport, mesh nodes, research state, workflows, and generated tools.</p></div></div><div class="grid"><div class="panel"><h3>Runtime <span class="${maintenance.running ? 'good' : 'bad'}">${maintenance.running ? 'supervised' : 'unavailable'}</span></h3><div class="body"><p class="explain">The supervisor restarts enabled local services and reports readiness.</p><div class="row"><span class="dot dot-online"></span><span style="flex:1">core API</span><span>${esc(location.host)}</span></div>${services.map(s => `<div class="row"><span class="dot ${s.status === 'running' ? 'dot-online' : s.status === 'starting' ? 'dot-unreachable' : 'dot-offline'}"></span><span style="flex:1">${esc(s.name)}</span><span>${esc(s.status)}</span></div>`).join('')}<div class="row"><span class="dot ${readiness.ollama_installed ? 'dot-online' : 'dot-offline'}"></span><span style="flex:1">Ollama</span><span>${readiness.ollama_installed ? esc((readiness.dsh_model||'installed')+' · '+(readiness.ollama_context_length||'?')+' ctx') : 'missing'}</span></div><div class="row"><span class="dot ${readiness.dsh_configured ? 'dot-online' : 'dot-unconfigured'}"></span><span style="flex:1">DSH ${esc(readiness.dsh_version || '')}</span><span>${readiness.dsh_configured ? esc((readiness.dsh_model||'?')+' · '+(readiness.dsh_context_window||'?')+' ctx') : esc(readiness.dsh_config_error || 'not configured')}</span></div><div class="row"><span class="dot ${readiness.playwright_chromium_ready ? 'dot-online' : 'dot-offline'}"></span><span style="flex:1">Playwright Chromium</span><span>${readiness.playwright_chromium_ready ? 'ready' : 'setup required'}</span></div><div class="row"><span class="dot ${(readiness.approved_browser_routes||0)>0 ? 'dot-online' : 'dot-unconfigured'}"></span><span style="flex:1">approved browser routes</span><span>${readiness.approved_browser_routes||0}</span></div><div class="row"><span class="dot ${readiness.docker_daemon_ready ? 'dot-online' : 'dot-offline'}"></span><span style="flex:1">Docker sandbox</span><span>${readiness.docker_daemon_ready ? 'ready' : 'unavailable'}</span></div>${readiness.dsh_web_url ? `<a class="ghost inline-link" href="${esc(readiness.dsh_web_url)}" target="_blank" rel="noopener">Open DSH Web</a>` : ''}</div></div><div class="panel"><h3>Host health <span class="${systemHealth.severity === 'ok' ? 'good' : systemHealth.severity === 'critical' ? 'bad' : 'warn'}">${esc(systemHealth.severity || 'unknown')}</span></h3><div class="body"><p class="explain">Live resource pressure used to prevent unsafe local-model concurrency.</p><div class="row"><span class="tag">CPU</span><span style="flex:1">${host.cpu_count_logical || '?'} logical</span><span>${host.cpu_util_pct == null ? '?' : host.cpu_util_pct+'%'}</span></div><div class="row"><span class="tag">RAM</span><span style="flex:1">${host.ram_used_gb == null ? '?' : host.ram_used_gb+' / '+host.ram_total_gb+' GiB'}</span><span>${host.ram_used_pct == null ? '?' : host.ram_used_pct+'%'}</span></div><div class="row"><span class="tag">disk</span><span style="flex:1">${host.disk_free_gb == null ? '?' : host.disk_free_gb+' GiB free'}</span><span>${host.disk_used_pct == null ? '?' : host.disk_used_pct+'%'}</span></div>${(systemHealth.alerts || []).map(a => `<div class="row"><span class="tag">${esc(a.severity)}</span><span class="warn">${esc(a.kind)}: ${esc(a.value)}</span></div>`).join('')}</div></div><div class="panel"><h3>Models <span>${state.models.length}</span></h3><div class="body"><p class="explain">Routes are ranked by cost, availability, validity, and recent latency. Browser routes require both approved policy and per-job disclosure.</p>${(platform.model_routes||[]).map(m => `<div class="row"><span class="tag">${esc(m.type)}</span><span style="flex:1">${esc(m.name)}</span><span>${esc(m.type==='playwright' ? m.transport_policy : (m.model||'configured'))}</span></div>`).join('') || state.models.map(m => `<div class="row"><span class="tag">${esc(m.provider)}</span><span style="flex:1">${esc(m.name)}</span><span>${m.runs ? Math.round((m.success_rate||0)*100)+'% / '+Math.round(m.latency_ema_ms||0)+'ms' : 'unscored'}</span></div>`).join('') || '<p class="hint">No models.</p>'}<div class="row"><span class="tag">spend</span><span style="flex:1">today ${money(spending.today_usd)}</span><span>month ${money(spending.month_usd)}</span></div></div></div><div class="panel"><h3>Mesh nodes <span>${online}/${state.nodes.length} online</span></h3><div class="body"><p class="explain">Optional trusted computers that can run preinstalled, permission-gated scripts.</p>${state.nodes.map(n => `<div class="row"><span class="dot dot-${n.status}"></span><span style="flex:1">${esc(n.name)}</span><span>${n.latency != null ? n.latency+'ms' : n.status}</span><button class="icon" onclick="checkNode('${n.name}')">&#8635;</button><button class="icon" onclick="removeNode('${n.id}')">&times;</button></div>`).join('') || '<p class="hint">No nodes.</p>'}<button class="ghost" onclick="addNode()">Add node</button></div></div><div class="panel"><h3>Agent platform <span>${state.jobs.filter(j => ['queued','running'].includes(j.status)).length} active</span></h3><div class="body"><div class="row"><span class="tag">jobs</span><span style="flex:1">${jobCounts.queued || 0} queued / ${jobCounts.running || 0} running</span><span>${jobCounts.failed || 0} failed</span></div><div class="row"><span class="tag">tools</span><span style="flex:1">${state.capabilities.filter(c => c.status === 'active').length} active capabilities</span><span>${state.capabilities.length} tracked</span></div><div class="row"><span class="tag">flows</span><span style="flex:1">${state.workflows.filter(w => w.enabled).length} enabled workflows</span><span>${state.workflows.length} total</span></div><div class="row"><span class="tag">research</span><span style="flex:1">${state.hypothesis_count} hypotheses / ${state.discovery_count} discoveries</span><span>${state.queued_research} queued</span></div><div class="row"><span class="tag">world</span><span style="flex:1">${state.world_count} signals / ${state.knowledge_count} knowledge</span><span>${state.feed_count} feeds</span></div><div class="row"><span class="tag">ontology</span><span style="flex:1">${forecasting.ontology_entities || 0} entities / ${forecasting.ontology_relations || 0} relations</span></div><div class="row"><span class="tag">forecast</span><span style="flex:1">${forecasting.open_forecasts || 0} open / ${forecasting.resolved_forecasts || 0} resolved</span></div><div class="row"><span class="tag">impact</span><span style="flex:1">${forecasting.proposed_impacts || 0} proposed / ${forecasting.active_impacts || 0} active</span></div><div class="row"><span class="tag">events</span><span style="flex:1">${state.event_count} ingested</span></div></div></div><div class="panel"><h3>Goals <span>${state.goals.length}</span></h3><div class="body"><p class="explain">Persistent outcomes reused across jobs and context retrieval.</p>${state.goals.map(g => `<div class="row"><span class="tag">${esc(g.status)}</span><span style="flex:1">${esc(g.title)}</span><span>${Math.round((g.priority||0)*100)}</span></div>`).join('') || '<p class="hint">No goals.</p>'}</div></div><div class="panel"><h3>Generated capabilities <span>${state.capabilities.length}</span></h3><div class="body"><p class="explain">Candidate tools remain inactive until testing and owner approval.</p>${state.capabilities.map(c => `<div class="row"><span class="tag">${esc(c.status)}</span><span style="flex:1">${esc(c.name)}</span><span>${c.autonomous_allowed ? 'background' : (c.last_test_ok ? 'tested' : '')}</span></div>`).join('') || '<p class="hint">None.</p>'}</div></div><div class="panel"><h3>Schedules <span>${(platform.schedules || []).length}</span></h3><div class="body"><p class="explain">Recurring jobs coalesce missed runs and never overlap a blocked predecessor.</p>${(platform.schedules || []).map(s => `<div class="row"><span class="dot ${s.enabled ? 'dot-online' : 'dot-unconfigured'}"></span><span style="flex:1">${esc(s.name)}</span><span>${s.next_run ? new Date(s.next_run*1000).toLocaleString() : 'off'}</span></div>`).join('') || '<p class="hint">No schedules.</p>'}</div></div><div class="panel"><h3>Ambient relay <button class="toggle ${state.audio.enabled ? 'on' : ''}" onclick="toggleAudio()">${state.audio.enabled ? 'on' : 'off'}</button></h3><div class="body"><p class="explain">Controls the existing ambient audio/event relay. Off means no ambient relay activity.</p></div></div></div>`,
    trading: `<div class="view-title"><div><h2>Trading evidence lab</h2><p>Collect market data, execute fixed rules, and display reproducible results. No local model reasons, hypothesizes, or interprets outcomes.</p></div><span class="tag">paper research only</span></div>
      <div class="grid"><div class="panel"><h3>Stocks: collect data <span>delayed SIP</span></h3><div class="body"><p class="explain">Screens current S&P 100 holdings using fixed volatility and liquidity thresholds.</p><label class="explain" for="tradingVol">Minimum annualized realized volatility</label><input id="tradingVol" class="input" type="number" min="0.05" max="3" step="0.05" value="0.20"><label class="explain" for="tradingMax">Maximum symbols</label><input id="tradingMax" class="input" type="number" min="5" max="100" value="30"><label class="explain" for="tradingLookback">History in days</label><input id="tradingLookback" class="input" type="number" min="180" max="1000" value="730"><button class="ghost" onclick="collectStocks('1Day')">Collect daily swing data</button><button class="toggle" onclick="collectStocks('1Hour')">Collect hourly data</button><p class="explain">After collection, use the backtest button. Requests end at least 20 minutes in the past.</p></div></div>
      <div class="panel"><h3>Stocks: run backtest <span>train → holdout</span></h3><div class="body"><p class="explain">Runs fixed momentum, pullback, mean-reversion, and breakout rules. Training ranks the rules; the later holdout is scored once.</p><label class="explain" for="tradingCost">Round-trip costs, basis points</label><input id="tradingCost" class="input" type="number" min="0" max="100" step="1" value="10"><button class="ghost" onclick="backtestLatestStock()" ${latestStockDataset?'':'disabled'}>Backtest latest dataset</button><p class="explain">${latestStockDataset?`Latest input: ${esc(latestStockDataset.path)}`:'Collect a stock dataset first.'}</p></div></div>
      <div class="panel"><h3>Kalshi paper experiment <span>public API · no funds</span></h3><div class="body"><p class="explain">Collect open markets, order books, candles, and rules. Paper fills use current public asks; P&amp;L is graded only from Kalshi's official settlement result.</p><button class="ghost" onclick="collectKalshi('bitcoin_15m')">Collect Bitcoin 15-minute markets</button><button class="toggle" onclick="collectKalshi('weather')">Collect weather markets</button>${kalshiMarketRows||'<p class="hint">Collect Kalshi markets to show selectable tickers and quotes.</p>'}${paper?`<div class="row"><span class="tag">virtual cash</span><span style="flex:1">$${Number(paper.cash_usd||0).toFixed(2)}</span><span>P&amp;L $${Number(paper.realized_pnl_usd||0).toFixed(2)}</span></div><div class="row"><span class="tag">positions</span><span style="flex:1">${(paper.open_positions||[]).length} open</span><span>${(paper.settled_positions||[]).length} settled</span></div><label class="explain" for="kalshiTicker">Market ticker selected by you or an outside model</label><input id="kalshiTicker" class="input" placeholder="KXBTC15M-..."><select id="kalshiSide" class="input"><option value="yes">YES</option><option value="no">NO</option></select><input id="kalshiSpend" class="input" type="number" min="0.01" max="10" step="0.01" value="1.00"><textarea id="kalshiRationale" placeholder="Explicit rationale from operator or outside model"></textarea><button class="ghost" onclick="placeKalshiPaper()">Record paper fill</button><button class="toggle" onclick="reconcileKalshiPaper()">Check official settlements</button>`:`<button class="ghost" onclick="startKalshiPaper()">Start $10 virtual bankroll</button>`}<p class="explain">There is deliberately no live-order capability. A real $10 deposit is not required for this paper stage.</p></div></div></div>
      ${backtest?`<div class="panel"><h3>Latest stock backtest <span class="${String(backtest.verdict||'').startsWith('PROMISING')?'good':'warn'}">${esc(backtest.verdict||'unscored')}</span></h3><div class="body"><div class="grid"><div class="metric"><b>SELECTED RULE</b><span>${esc(backtest.selected_strategy||'none')}</span></div><div class="metric"><b>HOLDOUT TRADES</b><span>${esc(String(holdout.trades||0))}</span></div><div class="metric"><b>EXPECTANCY</b><span>${Number(holdout.expectancy_bps||0).toFixed(2)} bps/trade</span></div><div class="metric"><b>WIN RATE</b><span>${(Number(holdout.win_rate||0)*100).toFixed(1)}%</span></div><div class="metric"><b>PROFIT FACTOR</b><span>${Number(holdout.profit_factor||0).toFixed(2)}</span></div><div class="metric"><b>MAX DRAWDOWN</b><span>${(Number(holdout.max_drawdown||0)*100).toFixed(1)}%</span></div></div><table><thead><tr><th>Rule</th><th>Train trades</th><th>Train expectancy</th><th>Holdout trades</th><th>Holdout expectancy</th><th>Holdout PF</th></tr></thead><tbody>${candidateRows}</tbody></table><p class="path-value">${esc(latestBacktest.path||'')}</p></div></div>`:'<div class="panel"><div class="body"><p class="hint">No backtest result yet. The two recent Alpaca runs collected data only.</p></div></div>'}
      <div class="panel"><h3>Claude through MCP <span class="${claudeMcp.configured?'good':claudeMcp.valid===false?'bad':'warn'}">${claudeMcp.configured?'configured':claudeMcp.valid===false?'invalid JSON':'not connected'}</span></h3><div class="body"><p class="explain">Claude is the analyst and MCP client; this harness is the permission-controlled executor. Claude can inspect datasets, compare evidence, select a market or rule to test, and request a virtual paper fill with its rationale. The harness performs the exact calculation, enforces permissions and the virtual bankroll, logs the call, and writes results.</p><button class="ghost" onclick="installClaudeMcp()">${claudeMcp.configured?'Repair or refresh Claude JSON':'Add Claude JSON automatically'}</button><p class="path-value">${esc(claudeMcp.path||'')}</p><p class="explain">This preserves other Claude settings and MCP servers. Fully quit and reopen Claude Desktop afterward, then enable <b>universal-assistant</b> tools in the conversation.</p>${claudeMcp.error?`<p class="bad">${esc(claudeMcp.error)}</p>`:''}</div></div>
      <div class="panel"><h3>Research history <span>${trading.count||0} artifacts</span></h3><div class="body">${(trading.datasets||[]).map(d=>`<div class="row"><span class="tag">${esc(d.dataset||'dataset')}</span><span style="flex:1">${esc(d.path)}</span><span>${esc(String(d.record_count==null?'?':d.record_count))} records</span></div>`).join('') || '<p class="hint">No trading artifacts yet.</p>'}</div></div>`,
    secrets: `<div class="view-title"><div><h2>Keys & connections</h2><p>Owner-only managed credentials. Existing values are never returned to the browser.</p></div><span class="tag">${managedSecrets.filter(item=>item.configured).length} configured</span></div><div class="grid"><div class="panel"><h3>Alpaca <span>${managedSecrets.filter(x=>x.group==='Alpaca').every(x=>x.configured)?'configured':'setup required'}</span></h3><div class="body"><label class="explain" for="alpacaKeyId">API key ID</label><input id="alpacaKeyId" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to keep existing"><label class="explain" for="alpacaSecret">API secret</label><input id="alpacaSecret" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to keep existing"><button class="ghost" onclick="saveSecretGroup([['APCA_API_KEY_ID','alpacaKeyId'],['APCA_API_SECRET_KEY','alpacaSecret']])">Save Alpaca credentials</button><p class="explain">Available to the stock collector immediately; restart remains recommended so every supervised process receives consistent state.</p></div></div><div class="panel"><h3>Telegram <span>${managedSecrets.filter(x=>x.group==='Telegram').every(x=>x.configured)?'configured':'optional'}</span></h3><div class="body"><label class="explain" for="telegramToken">Bot token</label><input id="telegramToken" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to keep existing"><label class="explain" for="telegramChats">Allowed private chat ID(s)</label><input id="telegramChats" class="input" type="password" autocomplete="new-password" placeholder="Numeric IDs separated by commas"><button class="ghost" onclick="saveSecretGroup([['TELEGRAM_BOT_TOKEN','telegramToken'],['ALLOWED_CHAT_IDS','telegramChats']])">Save Telegram settings</button><p class="explain">Telegram requires a restart. Only the allowlisted numeric chat IDs can use the bot.</p></div></div><div class="panel"><h3>Web research <span>optional</span></h3><div class="body"><label class="explain" for="braveKey">Brave Search API key</label><input id="braveKey" class="input" type="password" autocomplete="new-password" placeholder="Leave blank to keep existing"><button class="ghost" onclick="saveSecretGroup([['BRAVE_API_KEY','braveKey']])">Save search key</button><p class="explain">The free static web path remains available without this key.</p></div></div><div class="panel"><h3>Custom integration secret <span>advanced</span></h3><div class="body"><label class="explain" for="customSecretName">Environment name</label><input id="customSecretName" class="input" placeholder="INTEGRATION_EXAMPLE_TOKEN"><label class="explain" for="customSecretValue">Secret value</label><input id="customSecretValue" class="input" type="password" autocomplete="new-password"><button class="ghost" onclick="saveCustomSecret()">Save custom secret</button><p class="explain">Custom names must begin with <code>INTEGRATION_</code>. They are stored now and exposed only to the trusted core after restart—not to generated scripts or sandbox tests.</p></div></div></div><div class="panel"><h3>Configured inventory <span>values hidden</span></h3><div class="body">${managedSecrets.map(item=>`<div class="row"><span class="dot ${item.configured?'dot-online':'dot-unconfigured'}"></span><span style="flex:1">${esc(item.label)}</span><span>${item.configured?(item.age_days==null?'configured':esc(String(item.age_days))+' days old'):'not configured'}</span>${item.configured?`<button class="toggle" onclick="removeManagedSecret('${esc(item.name)}')">Remove</button>`:''}</div>`).join('')}</div></div>`,
    audit: `<div class="view-title"><div><h2>Audit & decisions</h2><p>Redacted, durable evidence for model routing, judgments, tools, permissions, checkpoints, and failures.</p></div><span class="tag">${auditRecords.length} recent</span></div><div class="panel"><h3>Automatic operations report <span>${overnightReport.updated_at ? esc(new Date(overnightReport.updated_at*1000).toLocaleString()) : 'waiting for first update'}</span></h3><div class="body"><p class="path-value">${esc(overnightReport.path || '')}</p><pre>${esc(overnightReport.content || 'The overnight report worker has not produced its first report yet.')}</pre></div></div><div class="panel"><div class="body">${auditRecords.map(record => `<details class="audit-item"><summary><span class="tag">${esc(record.kind)}</span><span>${esc(new Date(record.ts*1000).toLocaleString())}</span><span style="flex:1">${esc(auditSummary(record))}</span></summary><pre>${esc(JSON.stringify(record.data, null, 2))}</pre></details>`).join('') || '<p class="hint">No audit records.</p>'}</div></div>`,
    guide: `<div class="view-title"><div><h2>How this works</h2><p>Short explanations for every major surface. Nothing here grants additional authority.</p></div></div><div class="guide-grid">${guideCards()}</div>`
  };
  for(const key of Object.keys(views)){
    views[key] = views[key]
      .replaceAll('Playwright Chromium', 'Free web retrieval')
      .replaceAll('Playwright ', 'Web retrieval ')
      .replaceAll('browser transport', 'web retrieval')
      .replaceAll('approved browser routes', 'browser-model escalation (removed)')
      .replaceAll('Browser routes require both approved policy and per-job disclosure.', 'Web research uses free search and static HTTPS page reading; JavaScript is not executed.');
  }
  const latestDecision = latestFinished ? auditRecords.find(r => r.kind === 'model.response' && (r.data || {}).job_id === latestFinished.id) : null;
  views.context = `<div class="panel"><h3>Autonomous cycles <span>one click + fully audited</span></h3><div class="body"><p class="explain">Start one bounded cycle now. Duplicate active cycles are coalesced, and self-improvement can edit only an isolated source copy.</p><label class="consent"><input id="cycleBrowserEscalation" type="checkbox" ${((platform.model_routes||[]).some(p=>p.enabled && p.type==='playwright' && p.transport_policy==='approved_browser'))?'':'disabled'}><span><b>Allow approved browser fallback for this research cycle.</b><br>Used only after local non-progress and only when an approved route exists.</span></label><button class="ghost" onclick="queueResearchCycle()">Run research & discovery now</button><button class="ghost" onclick="queueGoalPlanning()">Derive goals from operator context</button><button class="ghost" onclick="queueSelfImprovement('')">Scan and repair an isolated copy</button></div></div>` + views.context;
  document.getElementById('panels').innerHTML = `<div class="workbench"><aside class="side-panel"><div class="brand">Universal Harness</div><div class="brand-sub">persistent local agent + DSH adapter</div><div class="metric"><b>REPOSITORY</b><span>${esc(shortPath(configuration.workspace))}</span></div><div class="nav-list">${[['work','Work'],['trading','Trading'],['secrets','Keys'],['context','Context'],['systems','Systems'],['audit','Audit'],['guide','Guide']].map(v => `<button class="nav-button ${activeView===v[0]?'active':''}" onclick="setView('${v[0]}')">${v[1]}</button>`).join('')}</div><hr style="border:0;border-top:1px solid var(--line);margin:12px 0"><div class="explain"><span class="dot ${maintenance.running ? 'dot-online' : 'dot-offline'}" style="display:inline-block"></span> ${maintenance.running ? 'Supervisor running' : 'Supervisor unavailable'}<br><span class="dot ${readiness.ollama_api_ready ? 'dot-online' : 'dot-unreachable'}" style="display:inline-block"></span> Ollama ${readiness.ollama_api_ready ? 'ready' : 'check runtime'}<br><span class="dot ${readiness.dsh_configured ? 'dot-online' : 'dot-unconfigured'}" style="display:inline-block"></span> DSH ${readiness.dsh_configured ? 'integrated' : 'optional'}<br><span class="dot ${readiness.playwright_chromium_ready ? 'dot-online' : 'dot-unconfigured'}" style="display:inline-block"></span> Playwright ${readiness.playwright_chromium_ready ? 'ready' : 'setup'}<br><span class="dot ${readiness.docker_daemon_ready ? 'dot-online' : 'dot-unconfigured'}" style="display:inline-block"></span> Docker ${readiness.docker_daemon_ready ? 'ready' : 'optional'}</div></aside><main>${views[activeView] || views.work}</main><aside class="side-panel inspector"><div class="brand-sub">SESSION INSPECTOR</div>${latestFinished ? `<div class="metric"><b>JOB</b><span>${esc(latestFinished.id)}</span></div><div class="metric"><b>ENGINE</b><span>${esc(latestFinished.engine || 'universal')}</span></div><div class="metric"><b>STATUS</b><span>${esc(latestFinished.status)}</span></div><div class="metric"><b>VERIFICATION</b><span>${esc(((latestFinished.result||{}).verification||{}).status || 'not specified')}</span></div><div class="metric"><b>PLAN</b><span>${esc(((latestFinished.result||{}).plan||[]).map(s=>`${s.status}: ${s.step}`).join(' · ') || 'not supplied')}</span></div><div class="metric"><b>ROLES</b><span>${esc(((latestFinished.result||{}).roles||[]).join(' → ') || 'default')}</span></div><div class="metric"><b>TOOLS USED</b><span>${esc(((latestFinished.result||{}).actions||[]).map(a=>a.tool).join(', ') || 'none')}</span></div><div class="metric"><b>MODEL DECISION</b><span>${esc(latestDecision ? (latestDecision.data.decision_summary || 'No summary supplied') : latestFinished.engine === 'dsh' ? 'DSH trace is recorded under Audit' : 'Open Audit for model events')}</span></div><button class="ghost" onclick="setView('audit')">Inspect full audit</button>` : '<p class="hint">A completed or blocked job will appear here.</p>'}<hr style="border:0;border-top:1px solid var(--line);margin:12px 0"><p class="explain"><b>Two coding modes</b><br>Choose DSH for its coding-agent workflow. Its stdout/stderr and before/after Git state are ingested here, but its built-in shell and editor do not pass through Universal tool grants. Choose Universal when grants, checkpoints, and per-tool verification are required.</p>${readiness.dsh_web_url ? `<a class="ghost inline-link" href="${esc(readiness.dsh_web_url)}" target="_blank" rel="noopener">Open interactive DSH</a>` : ''}</aside></div>`;
  for(const id of ['browserEscalation','cycleBrowserEscalation']){
    const control=document.getElementById(id);
    if(control && control.closest('label')) control.closest('label').remove();
  }
  const sidebarStatus=document.querySelector('.side-panel .explain');
  if(sidebarStatus) sidebarStatus.innerHTML=sidebarStatus.innerHTML.replace('Playwright','Free web retrieval');
  document.querySelectorAll('.row').forEach(row=>{
    const label=row.querySelector('span[style="flex:1"]');
    if(!label) return;
    if(label.textContent==='Playwright Chromium'){
      label.textContent='Free web retrieval';
      const value=row.lastElementChild;
      if(value) value.textContent=readiness.free_web_retrieval_ready ? (readiness.web_search_backend||'ready') : 'unavailable';
    }
    if(label.textContent==='approved browser routes'){
      label.textContent='MCP connections';
      const value=row.lastElementChild;
      if(value) value.textContent=readiness.mcp_http_ready ? 'stdio + authenticated HTTP' : (readiness.mcp_http_error||'stdio only');
    }
  });
  const objective = document.getElementById('jobObjective');
  if(objective){
    const presets=platform.prompt_presets||[];
    const row=document.createElement('div'); row.className='preset-row';
    row.innerHTML=`<select id="promptPreset" class="input" onchange="loadPromptPreset(this.value)"><option value="">Custom prompt</option>${presets.map(p=>`<option value="${esc(p.name)}" ${selectedPreset===p.name?'selected':''}>${esc(p.name)}</option>`).join('')}</select><input id="presetName" class="input" value="${esc(selectedPreset)}" placeholder="Preset name"><button class="toggle" onclick="savePromptPreset()">Save preset</button><button class="toggle" onclick="deletePromptPreset()">Delete</button>`;
    objective.parentNode.insertBefore(row,objective);
    objective.value = sessionStorage.getItem('universalJobDraft') || '';
  }
  const repositoryInput=document.getElementById('repositoryPath');
  if(repositoryInput) repositoryInput.insertAdjacentHTML('afterend','<button class="toggle" onclick="openConfiguredPath(\\'repository\\')">Open repository</button>');
  const vaultInput=document.getElementById('vaultPath');
  if(vaultInput) vaultInput.insertAdjacentHTML('afterend','<button class="toggle" onclick="openConfiguredPath(\\'obsidian\\')">Open Obsidian vault</button>');
  if(activeView==='context'){
    const main=document.querySelector('main');
    if(main) main.insertAdjacentHTML('afterbegin',loopFrequencyPanel(configuration.loop_frequencies||{}));
  }
  if(activeView==='audit'){
    const reportPanel=document.querySelector('main .panel');
    if(reportPanel && reportOpen){
      const heading=reportPanel.querySelector('h3');
      if(heading) heading.insertAdjacentHTML('beforeend','<button class="icon" onclick="setReportOpen(false)" title="Close overnight report" aria-label="Close overnight report">&times;</button>');
      const body=reportPanel.querySelector('.body');
      if(body) body.insertAdjacentHTML('afterbegin','<button class="toggle" onclick="openConfiguredPath(\\'overnight_report\\')">Open report file</button>');
    }else if(reportPanel){
      reportPanel.outerHTML='<button class="ghost" onclick="setReportOpen(true)">Show overnight report</button>';
    }
  }
}

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function money(value){ return '$' + Number(value || 0).toFixed(4); }
function setView(view){ activeView = view; render(); window.scrollTo({top:0,behavior:'smooth'}); }
function setComposerOpen(open){
  const objective=document.getElementById('jobObjective');
  if(objective) sessionStorage.setItem('universalJobDraft',objective.value);
  composerOpen=Boolean(open);
  localStorage.setItem('universalComposerOpen',String(composerOpen));
  render();
}
function setReportOpen(open){
  reportOpen=Boolean(open);
  localStorage.setItem('universalOvernightReportOpen',String(reportOpen));
  render();
}
function shortPath(path){ const parts=String(path||'not configured').replaceAll('\\\\','/').split('/'); return parts.slice(-2).join('/'); }
function jobOutput(job){
  const result = job.result || {};
  if(result.answer) return result.answer;
  if(result.error) return typeof result.error === 'string' ? result.error : JSON.stringify(result.error, null, 2);
  if(result.pending_action) return 'Waiting for approval:\\n' + JSON.stringify(result.pending_action, null, 2);
  return job.status === 'queued' || job.status === 'running' ? `Job is ${job.status}.` : JSON.stringify(result, null, 2);
}
function pendingActionOf(value){
  if(!value || typeof value!=='object') return null;
  if(value.pending_action && value.pending_action.tool) return value.pending_action;
  for(const child of Object.values(value)){
    const found=pendingActionOf(child); if(found) return found;
  }
  return null;
}
function jobDetails(job){
  const result = job.result || {};
  const pending = pendingActionOf(result);
  const actions = result.actions || [];
  const verification = result.verification || {};
  const plan = result.plan || [];
  const nativeLogs = result.native_session_logs || [];
  const dshTrace = auditRecords.filter(record => record.kind === 'dsh.trace' && (record.data||{}).job_id === job.id)
    .slice().reverse().map(record => ({channel:record.data.channel,text:record.data.text}));
  const approval = pending ? `<div class="consent"><span style="flex:1"><b>Permission requested: ${esc(pending.tool)}</b><br><span class="explain">${esc(JSON.stringify(pending.args||{}))}</span></span><button class="ghost" onclick="approveJobAction('${esc(job.id)}')">Approve once & resume</button></div>` : '';
  const capability = job.status==='awaiting_approval' && result.status==='tested' && result.name ? `<div class="consent"><span style="flex:1"><b>Tested capability: ${esc(result.name)}</b><br><span class="explain">Activation is an administrative decision.</span></span><button class="ghost" onclick="approveCapability('${esc(job.id)}','${esc(result.name)}')">Activate capability</button></div>` : '';
  if(!actions.length && !plan.length && !verification.status && !dshTrace.length && !nativeLogs.length && !result.failure_code) return approval+capability;
  return approval+capability+`<details class="audit-item"><summary><span class="tag">details</span><span>${actions.length} tool actions · ${plan.length} plan steps · ${dshTrace.length} DSH trace records · ${nativeLogs.length} native DSH logs · ${esc(verification.status || 'not verified')}</span></summary><pre>${esc(JSON.stringify({failure_code:result.failure_code||null,diagnostics:result.diagnostics||null,automatic_recovery:result.automatic_recovery||null,plan,roles:result.roles||[],delegations:result.delegations||0,actions,verification,dsh_trace:dshTrace,native_session_logs:nativeLogs},null,2))}</pre></details>`;
}
function updateEngineHelp(){
  const select=document.getElementById('jobEngine'); const target=document.getElementById('engineHelp');
  if(!select || !target) return;
  target.textContent=select.value==='dsh'
    ? 'Runs DSH headlessly in the active clean Git checkout. Final output, stderr trace, native session-log location, exit state, and before/after Git state are logged. Open DSH Web for its full native reasoning/tool timeline. DSH built-in actions are not individually permission-gated by Universal.'
    : 'Runs through leases, checkpoints, permission grants, verified tools, and the full audit trail.';
  const browser=document.getElementById('browserEscalation'); if(browser) browser.disabled=select.value==='dsh' || !((platform.model_routes||[]).some(p=>p.enabled && p.type==='playwright' && p.transport_policy==='approved_browser'));
}
function updateTemplateHelp(){
  const select=document.getElementById('jobTemplate'); const target=document.getElementById('templateHelp');
  if(!select || !target) return;
  const help={general:'General answers and mixed tasks. The assistant chooses only relevant tools.',coding:'Repository changes. Requires scoped write permission and sandbox tests before verified completion.',research:'Source-backed investigation ending in a cited, verified report artifact.',forecast:'A resolvable probability with explicit evidence, outcomes, horizon, and later scoring.',impact:'Ranks possible work, produces a deliverable, verifies it, and records measurable outcomes.',office:'Creates or inspects documents, spreadsheets, PDFs, and CSV files. Read-only requests do not force a new file.',browser:'Uses an owner-configured browser integration recipe and verifies the resulting page state.',operations:'Reads system/mesh health and may run only preconfigured, permission-gated recovery actions.'};
  target.textContent=help[select.value] || help.general;
}
function loadPromptPreset(name){
  selectedPreset=name || '';
  localStorage.setItem('universalPromptPreset',selectedPreset);
  const preset=(platform.prompt_presets||[]).find(item=>item.name===name);
  const nameBox=document.getElementById('presetName'); if(nameBox) nameBox.value=name || '';
  if(!preset) return;
  document.getElementById('jobObjective').value=preset.objective;
  document.getElementById('jobEngine').value=preset.engine;
  document.getElementById('jobTemplate').value=preset.template;
  document.getElementById('jobPriority').value=String(preset.priority);
  document.getElementById('approvalMode').value=preset.approval_mode;
  const browser=document.getElementById('browserEscalation');
  if(browser && !browser.disabled) browser.checked=Boolean(preset.browser_escalation);
  sessionStorage.setItem('universalJobDraft',preset.objective);
  updateEngineHelp(); updateTemplateHelp();
}
async function savePromptPreset(){
  const name=document.getElementById('presetName').value.trim();
  const objective=document.getElementById('jobObjective').value.trim();
  if(!name || !objective){ alert('Enter a preset name and prompt first.'); return; }
  const browser=document.getElementById('browserEscalation');
  const payload={name,objective,engine:document.getElementById('jobEngine').value,
    template:document.getElementById('jobTemplate').value,priority:Number(document.getElementById('jobPriority').value),
    approval_mode:document.getElementById('approvalMode').value,browser_escalation:Boolean(browser && browser.checked)};
  const response=await api('/api/owner/presets',post(payload));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not save preset.'); return; }
  selectedPreset=name; localStorage.setItem('universalPromptPreset',name);
  sessionStorage.setItem('universalJobDraft',objective); await load();
}
async function deletePromptPreset(){
  const name=(document.getElementById('promptPreset').value || document.getElementById('presetName').value).trim();
  if(!name){ alert('Select a saved preset first.'); return; }
  if(!confirm(`Delete prompt preset "${name}"?`)) return;
  const response=await api('/api/owner/presets/delete',post({name}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not delete preset.'); return; }
  selectedPreset=''; localStorage.removeItem('universalPromptPreset'); await load();
}
function guideCards(){
  const cards=[
    ['Start a work session','Describe the finished result, choose Universal for permission-gated tools or DSH for its coding workflow, then select a procedure and approval mode. Jobs survive worker restarts through leases and checkpoints.','New session','new_session'],
    ['Prompt presets','Save a useful objective, engine, procedure, priority, and approval mode under a reusable name. Loading a preset fills the form without immediately running it.','Open presets','new_session'],
    ['Research and discovery','A research cycle loads operator context, goals, local RAG context, web-search tools, and falsifiable research state. Duplicate active cycles are coalesced.','Research controls','context'],
    ['Loop frequencies','Control how often overnight research, reports, memory consolidation, health checks, permission notifications, and idle job polling run. Changes are validated and applied on restart.','Edit frequencies','context'],
    ['Repository','This is the editable security boundary. Existing files require matching hashes, and tests or commands still follow their grants. Change it under Workspace & context and restart to activate.','Open repository','open_repository'],
    ['Obsidian and RAG','The vault supplies durable notes while hybrid retrieval combines keywords with local nomic embeddings. Vault access does not grant arbitrary filesystem edits.','Open Obsidian','open_obsidian'],
    ['Permission requests','Suggest mode pauses for side effects. Auto-edit permits confined file edits. Execution, external actions, and administration still need exact grants. Windows popups alert you; approval happens here as a single-use scoped grant.','Review work','work'],
    ['Audit and overnight report','Audit contains redacted model decisions, tools, permissions, errors, DSH traces, and verification. The generated overnight report summarizes the durable ledger and can be closed or opened as a file.','Open Audit','audit'],
    ['Models and host health','Ollama is the local route. The Systems view shows configured routes, RAM/GPU pressure, service restarts, Docker, and DSH readiness. Keep model concurrency at one on this machine.','Open Systems','systems'],
    ['Free web retrieval','Web search uses DuckDuckGo by default and page reading uses static HTTPS. It does not execute JavaScript or sign into consumer model websites. Additional compute should use an approved API or MCP connection.','Research controls','context'],
    ['Docker verification','Coding checks run in a restricted, network-disabled container. Docker must be available; the harness does not silently execute a failed sandbox command on the host.','Check Systems','systems'],
    ['Goals and operator context','Operator context influences planning but never grants authority. Goal planning can create up to three bounded goals, and schedules avoid overlapping blocked work.','Edit context','context'],
    ['Self-improvement','The repair cycle scans source, creates an isolated Git-backed copy, edits and tests only that copy, and produces a reviewable patch. It never promotes its own changes.','Repair controls','context'],
    ['DSH integration','Choose DSH for audited headless coding or open DSH Web interactively. Output, stderr, native logs, and before/after Git state are ingested; DSH internal actions are not individually gated by Universal.','New DSH session','new_session'],
    ['MCP connection','The local stdio bridge exposes the live tool catalog to MCP clients and derives its scoped credential from managed secrets. The same gateway permissions still apply.','Open repository docs','open_repository']
  ];
  return cards.map(c=>`<article class="guide-card"><h4>${esc(c[0])}</h4><p>${esc(c[1])}</p><button class="ghost" onclick="guideAction('${c[3]}')">${esc(c[2])}</button></article>`).join('');
}
function loopFrequencyPanel(f){
  const seconds=(name,fallback)=>Number(f[name]||fallback);
  return `<div class="panel"><h3>Loop frequencies <span>saved for restart</span></h3><div class="body"><p class="explain">Shorter intervals react faster but create more model calls, disk writes, heat, and log traffic. Research cycles can be expensive in time even when the model is local.</p><div class="grid"><label class="explain">Research/discovery (minutes)<input id="loopDiscovery" class="input" type="number" min="1" max="1440" step="1" value="${seconds('discovery_seconds',1800)/60}"></label><label class="explain">Overnight report (minutes)<input id="loopReport" class="input" type="number" min="1" max="60" step="1" value="${seconds('overnight_report_seconds',300)/60}"></label><label class="explain">Memory consolidation (minutes)<input id="loopMemory" class="input" type="number" min="15" max="10080" step="15" value="${seconds('memory_consolidation_seconds',21600)/60}"></label><label class="explain">System health check (seconds)<input id="loopSystem" class="input" type="number" min="2" max="3600" step="1" value="${seconds('system_monitor_seconds',15)}"></label><label class="explain">Permission popup check (seconds)<input id="loopApproval" class="input" type="number" min="2" max="300" step="1" value="${seconds('approval_notification_seconds',5)}"></label><label class="explain">Idle job queue check (seconds)<input id="loopAssistant" class="input" type="number" min="1" max="60" step="1" value="${seconds('assistant_poll_seconds',5)}"></label></div><button class="ghost" onclick="saveLoopFrequencies()">Save loop frequencies</button><p class="explain">Saving does not interrupt active work. Restart overnight mode when convenient to apply all six values.</p></div></div>`;
}
function guideAction(action){
  if(action==='new_session'){ activeView='work'; setComposerOpen(true); return; }
  if(action==='open_repository'){ openConfiguredPath('repository'); return; }
  if(action==='open_obsidian'){ openConfiguredPath('obsidian'); return; }
  if(['work','context','systems','audit'].includes(action)){ setView(action); }
}
function auditSummary(record){
  const d = record.data || {};
  return d.decision_summary || d.error || [d.job_id, d.provider, d.tool, d.status].filter(Boolean).join(' · ') || 'details';
}

async function openConfiguredPath(name){
  const response=await api('/api/owner/open-configured-path',post({name}));
  if(!response || response.ok===false) alert(((response||{}).error||{}).message || 'Could not open the configured path.');
}
async function approveJobAction(jobId){
  if(!confirm('Approve exactly this displayed tool and argument set once, then resume the job?')) return;
  const response=await api('/api/owner/jobs/approve-once',post({id:jobId}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Approval failed.'); return; }
  await load();
}
async function approveCapability(jobId,name){
  if(!confirm(`Activate tested capability "${name}"? This makes it available in the live tool catalog.`)) return;
  const approved=await api('/api/capabilities/approve',post({name}));
  if(!approved || approved.ok===false){ alert(((approved||{}).error||{}).message || 'Capability approval failed.'); return; }
  const resolved=await api('/api/owner/jobs/control',post({id:jobId,action:'resolve'}));
  if(!resolved || resolved.ok===false) alert(((resolved||{}).error||{}).message || 'Capability activated, but the job could not be marked complete.');
  await load();
}

async function checkNode(name){ await api('/api/nodes/check', post({name})); load(); }
async function removeNode(id){ await api('/api/nodes/remove', post({id})); load(); }
async function addNode(){
  const name = prompt('Node name'); if(!name) return;
  const status_url = prompt('Status URL (optional)') || '';
  await api('/api/nodes/add', post({name, status_url})); load();
}
async function addCapture(){
  const text = document.getElementById('captureText').value.trim();
  if(!text) return;
  const tag = document.getElementById('captureTag').value.trim();
  await api('/api/capture/add', post({text, tag})); load();
}
async function removeCapture(id){ await api('/api/capture/remove', post({id})); load(); }
async function savePaths(){
  const repository=document.getElementById('repositoryPath').value.trim();
  const obsidian_vault=document.getElementById('vaultPath').value.trim();
  if(!repository || !obsidian_vault){ alert('Choose both an existing Git repository and an existing Obsidian vault.'); return; }
  const response=await api('/api/owner/settings/paths',post({repository,obsidian_vault}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not save folders.'); return; }
  alert('Folders saved. Stop and restart the pilot when you are ready to activate them.');
}
async function installClaudeMcp(){
  if(!confirm('Add or refresh the Universal Assistant entry in Claude Desktop JSON? Existing settings and other MCP servers will be preserved.')) return;
  const response=await api('/api/owner/integrations/claude-mcp',post({}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not update Claude Desktop configuration.'); return; }
  const result=response.result||{};
  alert(`${result.changed?'Claude JSON updated.':'Claude JSON was already correct.'} Fully quit and reopen Claude Desktop.`);
  await load();
}
async function saveDshSettings(){
  const model=document.getElementById('dshModel').value.trim();
  const context_window=Number(document.getElementById('dshContext').value);
  const reasoning_effort=document.getElementById('dshReasoning').value;
  if(!model || !Number.isInteger(context_window)){ alert('Enter an exact Ollama model tag and an integer context window.'); return; }
  const response=await api('/api/owner/settings/dsh',post({model,context_window,reasoning_effort}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not save DSH settings.'); return; }
  alert('DSH settings saved. Stop and restart the pilot to apply the final Ollama model/context layer.');
}
async function saveSecretGroup(entries){
  const pending=entries.map(([name,id])=>[name,(document.getElementById(id)?.value||'').trim()]).filter(item=>item[1]);
  if(!pending.length){ alert('Enter at least one new value. Blank fields keep their existing secret.'); return; }
  for(const [name,value] of pending){
    const response=await api('/api/owner/settings/secrets',post({name,value,action:'set'}));
    if(!response || response.ok===false){ alert(((response||{}).error||{}).message || `Could not save ${name}.`); return; }
  }
  alert('Secret settings saved. Values have been cleared from the form. Restart supervised services when convenient.');
  await load();
}
async function saveCustomSecret(){
  const name=(document.getElementById('customSecretName')?.value||'').trim().toUpperCase();
  const value=(document.getElementById('customSecretValue')?.value||'').trim();
  if(!name || !value){ alert('Enter both an INTEGRATION_ name and a value.'); return; }
  await saveSecretGroup([[name,'customSecretValue']]);
}
async function removeManagedSecret(name){
  if(!confirm(`Remove ${name} from the managed-secret store? Dependent services will stop working after restart.`)) return;
  const response=await api('/api/owner/settings/secrets',post({name,action:'remove'}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not remove secret.'); return; }
  await load();
}
async function saveLoopFrequencies(){
  const number=id=>Number(document.getElementById(id).value);
  const values={
    discovery_seconds:Math.round(number('loopDiscovery')*60),
    overnight_report_seconds:Math.round(number('loopReport')*60),
    memory_consolidation_seconds:Math.round(number('loopMemory')*60),
    system_monitor_seconds:Math.round(number('loopSystem')),
    approval_notification_seconds:Math.round(number('loopApproval')),
    assistant_poll_seconds:Math.round(number('loopAssistant'))
  };
  if(Object.values(values).some(value=>!Number.isInteger(value))){ alert('Every loop frequency must be a number.'); return; }
  const response=await api('/api/owner/settings/loops',post(values));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not save loop frequencies.'); return; }
  alert('Loop frequencies saved. Restart overnight mode when convenient to apply them.');
  await load();
}
async function saveOperatorContext(){
  const content=document.getElementById('operatorContext').value;
  const response=await api('/api/owner/operator-context',post({content}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not save operator context.'); return; }
  alert('Operator context saved and will be loaded into new autonomous jobs.');
  await load();
}
async function queueGoalPlanning(){
  const response=await api('/api/goals/derive',post({focus:'Use the saved operator context and current launch-readiness gaps.'}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not queue goal planning.'); return; }
  alert(`Goal-planning job ${response.result.job_id} is ${response.result.status}.`); await load();
}
async function queueResearchCycle(){
  const browser=document.getElementById('cycleBrowserEscalation');
  const browser_escalation=Boolean(browser && browser.checked);
  if(browser_escalation && !confirm('If local research gets stuck, this cycle may send its objective, bounded conversation, tool schemas, and tool results to an approved third-party browser provider. Continue?')) return;
  const response=await api('/api/jobs/research-cycle',post({focus:'Use operator context and the highest-value unresolved local question.',priority:0.7,auto_repair_on_failure:true,browser_escalation}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not queue research cycle.'); return; }
  alert(`Research cycle ${response.result.job_id} is ${response.result.status}.`); await load();
}
async function runTradingTool(name,args){
  if(!confirm('Collect this research dataset now? This reads external market data and writes one local artifact, but cannot place trades.')) return;
  const request_id=(globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() : `trading-${Date.now()}-${Math.random()}`;
  const response=await api('/api/tool-gateway',post({name,args,request_id}));
  if(!response || response.ok===false){
    alert(((response||{}).error||{}).message || 'Trading data collection failed. Open Audit for the recorded error.');
    return;
  }
  const summary=((response.result||{}).summary)||{};
  alert(`Saved ${summary.record_count==null?'?':summary.record_count} ${summary.dataset||'trading'} records. Open Trading to inspect the artifact.`);
  await load();
}
async function collectStocks(timeframe){
  const min_realized_vol=Number(document.getElementById('tradingVol').value);
  const max_symbols=Number(document.getElementById('tradingMax').value);
  const lookback_days=Number(document.getElementById('tradingLookback').value);
  if(!Number.isFinite(min_realized_vol) || !Number.isInteger(max_symbols) || !Number.isInteger(lookback_days)){ alert('Enter a valid volatility threshold, symbol count, and lookback.'); return; }
  await runTradingTool('collect_sp100_stock_bars',{timeframe,min_realized_vol,max_symbols,lookback_days,min_dollar_volume:50000000,feed:'sip'});
}
async function backtestLatestStock(){
  const latest=(platform.trading||{}).datasets?.find(item=>item.dataset==='sp100_stocks');
  if(!latest){ alert('Collect a stock dataset first.'); return; }
  const cost_bps=Number(document.getElementById('tradingCost').value);
  if(!Number.isFinite(cost_bps)){ alert('Enter a valid cost assumption.'); return; }
  await runTradingTool('backtest_stock_edges',{dataset:latest.path,cost_bps,train_fraction:0.70,minimum_test_trades:20,report_title:'S&P 100 stock edge test'});
}
async function collectKalshi(kind){
  await runTradingTool('collect_kalshi_markets',{kind,max_markets:25,include_orderbooks:true,lookback_hours:48});
}
async function tradingAction(name,args,message){
  if(message && !confirm(message)) return;
  const request_id=(globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() : `trading-${Date.now()}-${Math.random()}`;
  const response=await api('/api/tool-gateway',post({name,args,request_id}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Trading action failed. Open Audit for details.'); return; }
  await load();
  return response.result;
}
async function startKalshiPaper(){
  const result=await tradingAction('start_kalshi_paper',{bankroll_usd:10},'Create a $10 VIRTUAL Kalshi paper bankroll? No deposit or live order will occur.');
  if(result) alert('The $10 virtual bankroll is ready. No real funds were used.');
}
function selectKalshi(encodedTicker,side){
  document.getElementById('kalshiTicker').value=decodeURIComponent(encodedTicker);
  document.getElementById('kalshiSide').value=side;
  document.getElementById('kalshiRationale').focus();
}
async function placeKalshiPaper(){
  const ticker=document.getElementById('kalshiTicker').value.trim();
  const side=document.getElementById('kalshiSide').value;
  const max_spend_usd=Number(document.getElementById('kalshiSpend').value);
  const rationale=document.getElementById('kalshiRationale').value.trim();
  if(!ticker || !rationale || !Number.isFinite(max_spend_usd)){ alert('Ticker, virtual spend, and an explicit rationale are required.'); return; }
  const result=await tradingAction('record_kalshi_paper_fill',{ticker,side,max_spend_usd,rationale,decision_source:'command-center-operator',fee_per_contract_usd:0.02},`Record a VIRTUAL ${side.toUpperCase()} fill for up to $${max_spend_usd.toFixed(2)}? No live order will be placed.`);
  if(result) alert(`Paper position recorded. Virtual cash remaining: $${Number(result.cash_usd||0).toFixed(2)}.`);
}
async function reconcileKalshiPaper(){
  const result=await tradingAction('reconcile_kalshi_paper',{},'Check Kalshi for official settlements and update virtual P&L?');
  if(result) alert(`Reconciled ${result.settled.length} position(s); ${result.still_open} remain open.`);
}
async function queueSelfImprovement(focusOverride=null){
  const focus=focusOverride===null ? (prompt('Optional focus for this isolated self-improvement pass','Find and fix the highest-value reproducible bug or unfinished feature') || '') : focusOverride;
  const response=await api('/api/jobs/self-improvement',post({source_path:'',focus,priority:0.8}));
  if(!response || response.ok===false){ alert(((response||{}).error||{}).message || 'Could not queue self-improvement.'); return; }
  alert(`Self-improvement job ${response.result.job_id} is ${response.result.status}. It can edit only its isolated copy.`); await load();
}
async function addVault(){
  const name = prompt('Link name'); if(!name) return;
  const url = prompt('URL'); if(!url) return;
  const tag = prompt('Tag (optional)') || '';
  await api('/api/vault/add', post({name, url, tag})); load();
}
async function removeVault(id){ await api('/api/vault/remove', post({id})); load(); }
async function toggleAudio(){ await api('/api/audio/toggle', post({})); load(); }
async function queueJob(){
  const objective = document.getElementById('jobObjective').value.trim();
  if(!objective) return;
  const template = document.getElementById('jobTemplate').value;
  const engine = document.getElementById('jobEngine').value;
  const priority = Number(document.getElementById('jobPriority').value);
  const approval_mode = document.getElementById('approvalMode').value;
  const autoRepair = document.getElementById('autoRepairOnFailure');
  const payload = {max_steps:24, max_seconds:3600, engine, approval_mode,
                   auto_repair_on_failure:Boolean(autoRepair && autoRepair.checked)};
  if(template) payload.template = template;
  const browser = document.getElementById('browserEscalation');
  if(engine === 'universal' && browser && browser.checked){
    if(!confirm('This job may send its objective, bounded conversation, tool schemas, and tool results to an approved third-party browser provider if local routing fails. Continue?')) return;
    payload.browser_escalation = true;
  }
  const result = await api('/api/jobs/queue', post({objective, payload, priority}));
  if(result && result.ok === false) alert((result.error || {}).message || 'Could not queue job');
  else sessionStorage.removeItem('universalJobDraft');
  await load();
}

function post(body){ return {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}; }

document.getElementById('search').addEventListener('input', async (e) => {
  const q = e.target.value;
  const box = document.getElementById('results');
  if(!q){ box.innerHTML = ''; return; }
  const r = await api('/api/search?q=' + encodeURIComponent(q));
  const rows = [
    ...r.capture_entries.map(c => `<div class="row"><span class="tag">${esc(c.tag||'—')}</span>${esc(c.text)}</div>`),
    ...r.vault_links.map(l => `<div class="row"><span class="tag">${esc(l.tag||'—')}</span><a class="link" href="${esc(l.url)}" target="_blank">${esc(l.name)}</a></div>`),
    ...r.events.map(ev => `<div class="row"><span class="tag">${esc(ev.tag||'—')}</span>${esc(JSON.stringify(ev.payload))}</div>`),
  ].join('');
  box.innerHTML = `<div class="panel"><h3>Matches</h3><div class="body">${rows || '<p class="hint">Nothing yet.</p>'}</div></div>`;
});

load();
setInterval(() => {
  const active = document.activeElement && document.activeElement.tagName;
  if(active !== 'INPUT' && active !== 'TEXTAREA' && active !== 'SELECT') load();
}, 10000);
</script>
</body></html>"""


@app.route("/")
def index():
    return MAIN_PAGE


if __name__ == "__main__":
    if PASSWORD == "changeme":
        raise SystemExit("Refusing to start: set CORE_PASSWORD to a real value (this process can execute files).")
    if API_KEY == "changeme":
        raise SystemExit("Refusing to start: set CORE_API_KEY (or CORE_PASSWORD) to a real value.")
    init_db()
    print(f"Serving project at {ROOT_DIR}")
    print(f"Database: {DB_PATH}")
    print(f"Tool schema: http://{BIND_HOST}:{PORT}/api/tools")
    app.run(host=BIND_HOST, port=PORT)
