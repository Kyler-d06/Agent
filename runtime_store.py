"""SQLite task leases, fenced checkpoints and an at-most-once action journal.

An interrupted non-idempotent action is uncertain, not automatically retried.
SQLite is appropriate for a single core with multiple HTTP workers; workers do
not open this database over a network filesystem.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from platform_contracts import canonical, digest


_SENSITIVE_KEYS = {
    "api_key", "authorization", "bot_token", "cookie", "credentials",
    "core_api_key", "core_password", "core_secret", "password",
    "refresh_token", "secret", "session", "session_id", "token",
}
_SECRET_TEXT = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b((?:core_(?:api_key|password|secret)|[a-z0-9_]*(?:api_key|password|secret|access_token|refresh_token))\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def redact(value, *, _depth=0):
    """Remove credential-shaped values before durable observability records."""
    if _depth > 20:
        return "[TRUNCATED: nesting limit]"
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            sensitive = normalized in _SENSITIVE_KEYS or normalized.endswith("_api_key") or normalized.endswith("_password") or normalized.endswith("_secret")
            clean[str(key)] = "[REDACTED]" if sensitive else redact(item, _depth=_depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_TEXT:
            text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
        if len(text) > 100_000:
            text = text[:100_000] + "\n[TRUNCATED: 100000 character audit limit]"
        return text
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_checkpoints (
 job_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tool_executions (
 actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, tool TEXT NOT NULL,
 state TEXT NOT NULL, result_json TEXT, started_at REAL NOT NULL, finished_at REAL,
 PRIMARY KEY(actor,request_id));
CREATE TABLE IF NOT EXISTS permission_grants (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, tool TEXT NOT NULL, constraints_json TEXT NOT NULL,
 expires_at REAL NOT NULL, remaining INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS direct_permission_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, tool TEXT NOT NULL,
 args_json TEXT NOT NULL, request_id TEXT NOT NULL, created_at REAL NOT NULL,
 resolved_at REAL, resolution TEXT, grant_id TEXT,
 UNIQUE(actor,request_id));
CREATE TABLE IF NOT EXISTS platform_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL, job_id TEXT);
CREATE TABLE IF NOT EXISTS memories (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
 confidence REAL NOT NULL, verified INTEGER NOT NULL DEFAULT 0, expires_at REAL,
 supersedes TEXT, tags_json TEXT NOT NULL, embedding_json TEXT, embedding_model TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_sources (
 memory_id TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(memory_id,source));
CREATE TABLE IF NOT EXISTS memory_consolidations (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, kind TEXT NOT NULL,
 input_ids_json TEXT NOT NULL, output_memory_id TEXT NOT NULL,
 source_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, job_id TEXT, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 kind TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS integration_manifests (
 name TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_runs (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, task_type TEXT NOT NULL, ok INTEGER NOT NULL,
 latency_ms REAL NOT NULL, usage_json TEXT NOT NULL, error TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_cooldowns (
 provider TEXT PRIMARY KEY, reason TEXT NOT NULL, until REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_spend (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, task_type TEXT NOT NULL,
 input_tokens INTEGER NOT NULL DEFAULT 0, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
 output_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 1,
 cost_usd REAL NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
 usage_json TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS provider_spend_ts ON provider_spend(ts,provider);
CREATE TABLE IF NOT EXISTS spend_reservations (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, task_type TEXT NOT NULL,
 reserved_usd REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
 expires_at REAL NOT NULL, actual_usd REAL);
CREATE TABLE IF NOT EXISTS service_usage (
 id TEXT PRIMARY KEY, service TEXT NOT NULL, operation TEXT NOT NULL,
 requests INTEGER NOT NULL DEFAULT 1, units_json TEXT NOT NULL DEFAULT '{}',
 cost_usd REAL, pricing_known INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS service_usage_ts ON service_usage(ts,service);
CREATE TABLE IF NOT EXISTS improvement_versions (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, content_json TEXT NOT NULL,
 hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', created_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS improvement_active ON improvement_versions(name) WHERE status='active';
CREATE TABLE IF NOT EXISTS evaluation_suites (
 name TEXT PRIMARY KEY, cases_json TEXT NOT NULL, min_score REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL, suite TEXT NOT NULL, suite_hash TEXT NOT NULL,
 score REAL NOT NULL, passed INTEGER NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_schedules (
 name TEXT PRIMARY KEY, objective TEXT NOT NULL, payload_json TEXT NOT NULL,
 interval_seconds INTEGER NOT NULL, next_run REAL NOT NULL, enabled INTEGER NOT NULL,
 last_job_id TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_presets (
 name TEXT PRIMARY KEY, objective TEXT NOT NULL, engine TEXT NOT NULL,
 template TEXT NOT NULL, priority REAL NOT NULL, approval_mode TEXT NOT NULL,
 browser_escalation INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
"""


class LeaseLost(RuntimeError):
    pass


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RuntimeStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(agent_jobs)")}
            if columns:
                for name, typ in {"lease_token": "TEXT", "lease_until": "REAL", "attempts": "INTEGER DEFAULT 0"}.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE agent_jobs ADD COLUMN {name} {typ}")
            event_columns = {r[1] for r in db.execute("PRAGMA table_info(platform_events)")}
            if "job_id" not in event_columns:
                db.execute("ALTER TABLE platform_events ADD COLUMN job_id TEXT")
                for row in db.execute("SELECT id,data_json FROM platform_events WHERE job_id IS NULL").fetchall():
                    try:
                        job_id = json.loads(row["data_json"]).get("job_id")
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                        job_id = None
                    if job_id:
                        db.execute("UPDATE platform_events SET job_id=? WHERE id=?", (str(job_id), row["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS platform_events_job_id ON platform_events(job_id,id)")
            cap_columns = {r[1] for r in db.execute("PRAGMA table_info(capabilities)")}
            if cap_columns:
                for name in ("builder_job_id", "test_hash"):
                    if name not in cap_columns:
                        db.execute(f"ALTER TABLE capabilities ADD COLUMN {name} TEXT")
            db.execute("INSERT OR IGNORE INTO memory_sources(memory_id,source,created_at) SELECT id,source,created_at FROM memories")

    @contextmanager
    def connect(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, kind, data):
        clean = redact(data)
        job_id = clean.get("job_id") if isinstance(clean, dict) else None
        with self.connect() as db:
            db.execute("INSERT INTO platform_events(ts,kind,data_json,job_id) VALUES(?,?,?,?)",
                       (time.time(), str(kind)[:120], canonical(clean), str(job_id) if job_id else None))

    def request_permission(self, actor, tool, args, request_id):
        """Persist exact direct-MCP arguments for later owner review and one-use approval."""
        args_json = canonical(args)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO direct_permission_requests"
                "(actor,tool,args_json,request_id,created_at) "
                "SELECT ?,?,?,?,? WHERE NOT EXISTS ("
                "SELECT 1 FROM direct_permission_requests "
                "WHERE actor=? AND tool=? AND args_json=? AND resolved_at IS NULL)",
                (str(actor), str(tool), args_json, str(request_id), time.time(),
                 str(actor), str(tool), args_json),
            )

    def pending_permissions(self, limit=100):
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,actor,tool,args_json,request_id,created_at "
                "FROM direct_permission_requests WHERE resolved_at IS NULL "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [{**dict(row), "args": json.loads(row["args_json"])} for row in rows]

    def approve_permission(self, permission_id, expires_at):
        """Atomically resolve one pending request and create its exact one-use grant."""
        if expires_at <= time.time():
            raise ValueError("future expiry required")
        gid = uuid.uuid4().hex
        now = time.time()
        with self.connect(True) as db:
            row = db.execute(
                "SELECT * FROM direct_permission_requests WHERE id=? AND resolved_at IS NULL",
                (int(permission_id),),
            ).fetchone()
            if not row:
                raise ValueError("permission request is no longer pending")
            db.execute(
                "INSERT INTO permission_grants VALUES(?,?,?,?,?,?,?)",
                (gid, row["actor"], row["tool"], row["args_json"], expires_at, 1, now),
            )
            db.execute(
                "UPDATE direct_permission_requests SET resolved_at=?,resolution='approved_once',grant_id=? WHERE id=?",
                (now, gid, row["id"]),
            )
        return {"id": gid, "actor": row["actor"], "tool": row["tool"],
                "args": json.loads(row["args_json"]), "request_id": row["request_id"]}

    def audit(self, *, limit=100, kind="", job_id="", after_id=0):
        limit = max(1, min(int(limit), 500))
        kind = str(kind or "").strip()
        job_id = str(job_id or "").strip()
        after_id = max(0, int(after_id or 0))
        sql = "SELECT id,ts,kind,data_json FROM platform_events"
        clauses = []
        params = []
        if kind:
            clauses.append("kind LIKE ?")
            params.append(kind + "%")
        if job_id:
            clauses.append("job_id=?")
            params.append(job_id)
        if after_id:
            clauses.append("id>?")
            params.append(after_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id " + ("ASC" if after_id else "DESC") + " LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        records = [{**dict(row), "data": json.loads(row["data_json"])} for row in rows]
        return records

    def meter(self, service, operation, *, requests=1, units=None, cost_usd=None):
        with self.connect() as db:
            db.execute("INSERT INTO service_usage VALUES(?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, str(service), str(operation), int(requests), canonical(units or {}),
                        float(cost_usd) if cost_usd is not None else None, int(cost_usd is not None), time.time()))

    def usage_check(self, service, daily_request_limit):
        now = time.time(); day = now - (now % 86400)
        with self.connect() as db:
            used = int(db.execute("SELECT COALESCE(SUM(requests),0) FROM service_usage WHERE service=? AND ts>=?", (service, day)).fetchone()[0])
        limit = max(0, int(daily_request_limit))
        return {"service": service, "used_today": used, "daily_request_limit": limit, "allowed": limit > used}

    def claim(self, worker_id, lease_seconds=120):
        now = time.time()
        schedule_events = []
        with self.connect(True) as db:
            for schedule in db.execute("SELECT * FROM task_schedules WHERE enabled=1 AND next_run<=?", (now,)).fetchall():
                previous = db.execute("SELECT status,result_json FROM agent_jobs WHERE id=?", (schedule["last_job_id"],)).fetchone()
                # Coalesce genuinely active work. A blocked permission request
                # remains singular, but a completed failure/non-progress result
                # must not disable the schedule forever.
                if previous and previous[0] in {"queued", "running", "awaiting_approval"}:
                    continue
                if previous and previous[0] == "blocked":
                    previous_result = json.loads(previous[1] or "{}")
                    if previous_result.get("pending_action"):
                        continue
                jid = "JOB_" + uuid.uuid4().hex
                payload = json.loads(schedule["payload_json"] or "{}")
                late_by = max(0.0, now - float(schedule["next_run"]))
                payload["_schedule_trigger"] = {
                    "name": schedule["name"], "scheduled_for": schedule["next_run"],
                    "claimed_at": now, "late_by_seconds": round(late_by, 3),
                    "wake_catch_up": late_by >= 60,
                }
                db.execute("INSERT INTO agent_jobs(id,kind,status,priority,objective,payload_json,created_at,updated_at) VALUES(?,'agent','queued',0.5,?,?,?,?)",
                           (jid, schedule["objective"], canonical(payload), timestamp(), timestamp()))
                db.execute("UPDATE task_schedules SET last_job_id=?,next_run=?,updated_at=? WHERE name=?", (jid, now + schedule["interval_seconds"], now, schedule["name"]))
                schedule_events.append({"job_id": jid, **payload["_schedule_trigger"]})
            # Legacy running jobs have no checkpoint/lease and require review.
            db.execute("UPDATE agent_jobs SET status='blocked',result_json=?,updated_at=? WHERE status='running' AND lease_until IS NULL",
                       (canonical({"ok": False, "error": "Legacy running job has no lease; owner review required"}), timestamp()))
            db.execute("UPDATE agent_jobs SET status='queued',lease_token=NULL WHERE status='running' AND lease_until<?", (now,))
            row = db.execute("SELECT * FROM agent_jobs WHERE status='queued' ORDER BY priority DESC,created_at LIMIT 1").fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(24)
            db.execute("UPDATE agent_jobs SET status='running',worker_id=?,lease_token=?,lease_until=?,attempts=attempts+1,claimed_at=?,updated_at=? WHERE id=?",
                       (worker_id, token, now + lease_seconds, timestamp(), timestamp(), row["id"]))
            out = dict(db.execute("SELECT * FROM agent_jobs WHERE id=?", (row["id"],)).fetchone())
            out["payload"] = json.loads(out["payload_json"] or "{}")
        for event in schedule_events:
            self.event("schedule.triggered", event)
        self.event("task.claimed", {"job_id": out["id"], "worker_id": worker_id,
                                    "attempt": out.get("attempts"), "kind": out.get("kind")})
        return out

    def _owned(self, db, job_id, token):
        row = db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running" or not token or row["lease_token"] != token or (row["lease_until"] or 0) <= time.time():
            raise LeaseLost("job cancelled, lease expired, or another worker owns it")
        return row

    def heartbeat(self, job_id, token, lease_seconds=120):
        with self.connect(True) as db:
            self._owned(db, job_id, token)
            db.execute("UPDATE agent_jobs SET lease_until=? WHERE id=?", (time.time() + lease_seconds, job_id))
        return {"alive": True}

    def checkpoint(self, job_id, token, state=None):
        with self.connect(True) as db:
            self._owned(db, job_id, token)
            if state is not None:
                if len(canonical(state)) > 4_000_000:
                    raise ValueError("checkpoint exceeds 4 MB")
                db.execute("INSERT INTO task_checkpoints VALUES(?,?,0,?) ON CONFLICT(job_id) DO UPDATE SET state_json=excluded.state_json,revision=revision+1,updated_at=excluded.updated_at",
                           (job_id, canonical(state), time.time()))
            row = db.execute("SELECT * FROM task_checkpoints WHERE job_id=?", (job_id,)).fetchone()
            result = {"state": json.loads(row["state_json"]) if row else {}, "revision": row["revision"] if row else -1}
        if state is not None:
            agent = state.get("agent", {}) if isinstance(state, dict) else {}
            self.event("task.checkpoint", {"job_id": job_id, "revision": result["revision"],
                                           "turns": agent.get("turns"), "pending_calls": len(agent.get("pending", []))})
        return result

    def complete(self, job_id, token, status, result):
        if status not in {"done", "failed", "blocked", "awaiting_approval"}:
            raise ValueError("invalid completion status")
        with self.connect(True) as db:
            row = self._owned(db, job_id, token)
            db.execute("UPDATE agent_jobs SET status=?,result_json=?,updated_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                       (status, canonical(result), timestamp(), job_id))
            if row["kind"] == "workflow":
                run_id = json.loads(row["payload_json"] or "{}").get("workflow_run_id")
                if run_id:
                    db.execute("UPDATE workflow_runs SET status=?,result_json=?,updated_at=? WHERE id=?", (status, canonical(result), timestamp(), run_id))
        self.event("task." + status, {"job_id": job_id, "result": result})
        return {"id": job_id, "status": status}

    def control(self, job_id, action):
        with self.connect(True) as db:
            row = db.execute("SELECT status FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("unknown job")
            if action == "resume":
                if row[0] not in {"blocked", "failed", "awaiting_approval", "cancelled"}:
                    raise ValueError("only stopped jobs can be resumed")
                status = "queued"
            elif action == "resolve":
                if row[0] != "awaiting_approval":
                    raise ValueError("only awaiting-approval jobs can be resolved")
                status = "done"
            elif action == "cancel":
                if row[0] == "done":
                    raise ValueError("completed job cannot be cancelled")
                status = "cancelled"
            else:
                raise ValueError("expected cancel, resume, or resolve")
            if action == "resume":
                db.execute("UPDATE agent_jobs SET status=?,result_json=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                           (status, timestamp(), job_id))
            else:
                db.execute("UPDATE agent_jobs SET status=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                           (status, timestamp(), job_id))
        self.event("task." + action, {"job_id": job_id, "status": status})
        return {"id": job_id, "status": status}

    def schedule(self, name, objective, interval_seconds, payload=None, enabled=True):
        if not name or not objective or not 60 <= interval_seconds <= 365 * 86400:
            raise ValueError("name, objective, and interval between 60 seconds and one year required")
        with self.connect() as db:
            db.execute("INSERT INTO task_schedules VALUES(?,?,?,?,?,?,NULL,?) ON CONFLICT(name) DO UPDATE SET objective=excluded.objective,payload_json=excluded.payload_json,interval_seconds=excluded.interval_seconds,enabled=excluded.enabled,updated_at=excluded.updated_at",
                       (name, objective, canonical(payload or {}), interval_seconds, time.time(), int(enabled), time.time()))
        return {"name": name, "enabled": enabled, "interval_seconds": interval_seconds}

    def save_prompt_preset(self, name, objective, engine="universal", template="", priority=0.5,
                           approval_mode="auto_edit", browser_escalation=False):
        name, objective = str(name or "").strip(), str(objective or "").strip()
        if not name or len(name) > 80 or not objective or len(objective) > 20_000:
            raise ValueError("preset needs a name (80 characters max) and objective (20000 characters max)")
        if engine not in {"universal", "dsh"}:
            raise ValueError("preset engine must be universal or dsh")
        if template not in {"", "coding", "research", "forecast", "impact", "office", "browser", "operations"}:
            raise ValueError("unknown preset template")
        if approval_mode not in {"suggest", "auto_edit", "full_auto"}:
            raise ValueError("unknown preset approval mode")
        priority = float(priority)
        if not 0 <= priority <= 1:
            raise ValueError("preset priority must be between zero and one")
        with self.connect() as db:
            db.execute("INSERT INTO prompt_presets VALUES(?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(name) DO UPDATE SET objective=excluded.objective,engine=excluded.engine,"
                       "template=excluded.template,priority=excluded.priority,approval_mode=excluded.approval_mode,"
                       "browser_escalation=excluded.browser_escalation,updated_at=excluded.updated_at",
                       (name, objective, engine, template, priority, approval_mode,
                        int(bool(browser_escalation)), time.time()))
        self.event("preset.saved", {"name": name, "engine": engine, "template": template})
        return {"name": name, "saved": True}

    def delete_prompt_preset(self, name):
        with self.connect() as db:
            deleted = db.execute("DELETE FROM prompt_presets WHERE name=?", (str(name or "").strip(),)).rowcount
        if not deleted:
            raise ValueError("unknown prompt preset")
        self.event("preset.deleted", {"name": str(name)})
        return {"name": str(name), "deleted": True}

    def begin_action(self, actor, request_id, name, args):
        if not request_id or len(request_id) > 240:
            raise ValueError("bounded request_id required")
        fingerprint = digest({"name": name, "args": args})
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM tool_executions WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("request_id reused for a different action")
                if row["state"] == "done":
                    return json.loads(row["result_json"])
                raise LeaseLost("action outcome uncertain; owner must reconcile before resuming")
            db.execute("INSERT INTO tool_executions(actor,request_id,fingerprint,tool,state,started_at) VALUES(?,?,?,?,'started',?)",
                       (actor, request_id, fingerprint, name, time.time()))
        return None

    def finish_action(self, actor, request_id, result):
        with self.connect() as db:
            db.execute("UPDATE tool_executions SET state='done',result_json=?,finished_at=? WHERE actor=? AND request_id=?",
                       (canonical(result), time.time(), actor, request_id))

    def reconcile(self, actor, request_id, result):
        with self.connect(True) as db:
            row = db.execute("SELECT state FROM tool_executions WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
            if not row or row[0] != "started":
                raise ValueError("only uncertain actions can be reconciled")
            db.execute("UPDATE tool_executions SET state='done',result_json=?,finished_at=? WHERE actor=? AND request_id=?", (canonical(result), time.time(), actor, request_id))
        self.event("action.reconciled", {"actor": actor, "request_id": request_id})

    def grant(self, actor, name, constraints, expires_at, uses=100):
        if expires_at <= time.time() or not 1 <= uses <= 100000:
            raise ValueError("future expiry and positive bounded uses required")
        gid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO permission_grants VALUES(?,?,?,?,?,?,?)", (gid, actor, name, canonical(constraints), expires_at, uses, time.time()))
        return {"id": gid}

    def consume_grant(self, actor, name, args):
        # Exact argument restrictions compose safely; filesystem roots are enforced by adapters.
        with self.connect(True) as db:
            for row in db.execute("SELECT * FROM permission_grants WHERE actor=? AND tool=? AND expires_at>? AND remaining>0 ORDER BY created_at", (actor, name, time.time())).fetchall():
                constraints = json.loads(row["constraints_json"])
                if all(k in args and args[k] == v for k, v in constraints.items()):
                    db.execute("UPDATE permission_grants SET remaining=remaining-1 WHERE id=?", (row["id"],))
                    return True
        return False
