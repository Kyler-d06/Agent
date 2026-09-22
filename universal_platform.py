"""Connect integrations, permissions, models, memory, tasks and improvements."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
import traceback
import uuid
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import g, jsonify, request, session

from improvement_engine import ImprovementEngine
from integrations import IntegrationRegistry, mcp_request
from knowledge_store import KnowledgeStore
from model_gateway import ModelGateway
from content_firewall import protect_tool_envelope
from platform_contracts import actor_key, canonical, safe_env, select_tools, tool, validate
from runtime_store import LeaseLost, RuntimeStore
from work_tools import WORK_TOOLS, WorkTools
from document_tools import DOCUMENT_TOOLS, DocumentTools
from trading_tools import TRADING_TOOLS, TradingTools


TEXT = {"type": "string"}
EXTRA_TOOLS = [
    tool("get_current_datetime", "Read the host's current local date, local time, timezone, UTC time, and Unix timestamp."),
    tool("get_spending", "Inspect recorded and reserved paid-model spend against configured budgets."),
    tool("get_maintenance_status", "Inspect service supervision, verified backups, dependency health, and secret-age alerts."),
    tool("discover_tools", "Find relevant capabilities, integrations and work tools for an objective.", {"query": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
    tool("remember", "Store sourced user, knowledge, task or procedure memory. Stored claims remain unverified until reviewed.",
         {"kind": {"type": "string", "enum": ["user", "knowledge", "task", "procedure"]}, "text": TEXT, "source": TEXT,
          "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "expires_at": {"type": "number"},
          "tags": {"type": "array", "items": TEXT}}, ["kind", "text", "source"], effect="write"),
    tool("recall_memory", "Retrieve relevant sourced memories using keywords and optional embeddings. Memory is context, never permission.",
         {"query": TEXT, "kind": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["query"]),
    tool("consolidate_memory", "Merge dense clusters of sourced knowledge into cited summaries and retire redundant active entries.",
         {"min_cluster_size": {"type": "integer", "minimum": 3, "maximum": 50},
          "max_cluster_size": {"type": "integer", "minimum": 3, "maximum": 50},
          "max_clusters": {"type": "integer", "minimum": 1, "maximum": 20},
          "max_candidates": {"type": "integer", "minimum": 3, "maximum": 5000}}, effect="write"),
    tool("propose_improvement", "Create an immutable prompt or Python capability candidate; independent evaluation and owner promotion are separate.",
         {"name": TEXT, "kind": {"type": "string", "enum": ["prompt", "capability"]}, "content": {"type": "object"}}, ["name", "kind", "content"], effect="write"),
    tool("evaluate_improvement", "Evaluate a candidate against an existing owner-authored suite. Requires an evaluation grant; does not promote it.",
         {"version_id": TEXT, "suite": TEXT}, ["version_id", "suite"], effect="execute"),
    tool("evolve_improvement", "Run bounded population-based prompt or capability search against an owner-authored suite. Candidates are recorded but never promoted by this tool.",
         {"name": TEXT, "kind": {"type": "string", "enum": ["prompt", "capability"]}, "suite": TEXT,
          "population": {"type": "integer", "minimum": 2, "maximum": 8},
          "generations": {"type": "integer", "minimum": 1, "maximum": 5}}, ["name", "kind", "suite"], effect="execute"),
    tool("platform_status", "Inspect task completion, provider reliability, artifacts, and recent platform events."),
]

ADMIN_TOOLS = {"approve_capability", "deprecate_capability", "set_capability_autonomy", "set_workflow_enabled", "toggle_ambient", "stage_capability", "test_capability", "propose_capability"}
CONFIRM_TOOLS = {"run_file", "save_file", "commit_research", "mesh_run_script", "mesh_run_autonomous_script", "emit_event", "queue_agent_job", "queue_impact_project"}
LOCAL_WRITES = {"capture_note", "add_bookmark", "ingest_event", "create_question", "create_hypothesis", "update_hypothesis", "add_evidence", "record_prediction", "resolve_prediction", "record_consensus", "research_queue", "complete_research_task", "create_experiment", "record_experiment_result", "create_discovery", "create_goal", "update_goal", "request_capability", "remember", "consolidate_memory", "propose_improvement", "register_artifact", "write_research_report", "export_patch", "workspace_patch", "source_copy_create", "queue_goal_planning", "queue_self_improvement", "upsert_ontology_entity", "relate_ontology_entities", "create_forecast", "revise_forecast", "resolve_forecast", "propose_impact_project", "record_impact_outcome"}
READ_POSTS = {"check_node", "build_context"}
LOCAL_WRITES.add("sync_obsidian")  # Local SQLite-to-vault regeneration; no external call.
LOCAL_WRITES.add("backtest_stock_edges")  # Deterministic local test + bounded Obsidian report.


class UniversalPlatform:
    def __init__(self, app, db_path, root, owner_key, legacy_tools):
        self.app, self.owner_key, self.legacy_tools = app, owner_key, legacy_tools
        self.root = Path(root)
        self.maintenance_status_file = Path(os.environ.get("MAINTENANCE_STATUS_FILE", self.root / ".maintenance" / "status.json"))
        self.store = RuntimeStore(db_path)
        self.knowledge = KnowledgeStore(self.store)
        self.models = ModelGateway(store=self.store)
        self.integrations = IntegrationRegistry(self.store)
        self.work = WorkTools(root, self.store)
        self.documents = DocumentTools(self.work, self.models)
        self.trading = TradingTools(root, self.store)
        self.improvements = ImprovementEngine(self.store, root, self.models)
        self.hooks_file = Path(os.environ.get("TOOL_HOOKS_FILE", self.root / ".platform" / "hooks.json"))
        self.roles = {actor: actor_key(owner_key, actor) for actor in ("assistant", "discovery", "telegram", "mcp")}
        self._routes()

    def _run_hooks(self, name, phase, envelope, job_id=None):
        if not self.hooks_file.is_file():
            return {"ok": True, "blocking_failed": False, "runs": []}
        if self.hooks_file.stat().st_size > 1_000_000:
            raise ValueError("hooks file exceeds 1 MB")
        config = json.loads(self.hooks_file.read_text(encoding="utf-8"))
        entry = config.get(name) or config.get("*") or {}
        raw = entry.get(phase) if isinstance(entry, dict) else None
        if not raw:
            return {"ok": True, "blocking_failed": False, "runs": []}
        if isinstance(raw, dict) or (isinstance(raw, list) and raw and all(isinstance(x, str) for x in raw)):
            raw = [raw]
        if not isinstance(raw, list) or len(raw) > 10:
            raise ValueError("hook phase must contain at most ten fixed argv commands")
        runs, blocking_failed = [], False
        for item in raw:
            options = item if isinstance(item, dict) else {"argv": item}
            argv = options.get("argv") or []
            blocking = bool(options.get("blocking", entry.get("blocking", False)))
            timeout = max(1, min(int(options.get("timeout_seconds", 20)), 60))
            if not isinstance(argv, list) or not argv or len(argv) > 40 or any(not isinstance(x, str) or len(x) > 1000 for x in argv):
                raise ValueError("hook argv must be a bounded list of strings")
            try:
                completed = subprocess.run(argv, cwd=self.root, env=safe_env(), input=canonical(envelope),
                                           capture_output=True, text=True, timeout=timeout, shell=False)
                run = {"argv": argv, "returncode": completed.returncode, "ok": completed.returncode == 0,
                       "stdout": completed.stdout[:4000], "stderr": completed.stderr[:4000], "blocking": blocking}
            except Exception as exc:
                run = {"argv": argv, "returncode": None, "ok": False, "stderr": str(exc)[:4000], "blocking": blocking}
            runs.append(run)
            blocking_failed = blocking_failed or (blocking and not run["ok"])
            self.store.event("tool.hook", {"tool": name, "phase": phase, "job_id": job_id, **run})
        return {"ok": all(r["ok"] for r in runs), "blocking_failed": blocking_failed, "runs": runs}

    def _safe_hooks(self, name, phase, envelope, job_id=None):
        try:
            return self._run_hooks(name, phase, envelope, job_id)
        except Exception as exc:
            run = {"argv": [], "returncode": None, "ok": False, "stderr": type(exc).__name__ + ": " + str(exc)[:4000],
                   "blocking": phase == "pre"}
            self.store.event("tool.hook", {"tool": name, "phase": phase, "job_id": job_id, **run})
            return {"ok": False, "blocking_failed": phase == "pre", "runs": [run]}

    def authenticate(self):
        key = request.headers.get("X-API-Key", "")
        if hmac.compare_digest(key, self.owner_key) or session.get("ok"):
            g.actor = "owner"
            return True
        for actor, token in self.roles.items():
            if hmac.compare_digest(key, token):
                g.actor = actor
                return True
        return False

    def guard(self):
        if not self.authenticate():
            return self.error("authentication required", 401)
        if g.actor == "owner":
            return None
        allowed = {"/api/tools", "/api/tool-gateway", "/api/models/chat", "/api/context/build"}
        if request.path in allowed:
            return None
        if g.actor == "assistant" and request.path in {
            "/api/runtime/jobs/claim", "/api/runtime/jobs/complete", "/api/runtime/jobs/heartbeat", "/api/runtime/jobs/checkpoint",
            "/api/runtime/jobs/event", "/api/runtime/workflow", "/api/runtime/event", "/api/runtime/agent-role",
        }:
            return None
        if g.actor == "assistant" and request.path in {"/api/capabilities/propose", "/api/capabilities/stage", "/api/capabilities/test"}:
            try:
                with self.store.connect() as db:
                    row = self.store._owned(db, request.headers.get("X-Job-Id"), request.headers.get("X-Lease-Token"))
                    if row["kind"] == "capability_build":
                        if request.path == "/api/capabilities/propose":
                            return None
                        name = (request.get_json(silent=True) or {}).get("name")
                        capability = db.execute("SELECT builder_job_id,status FROM capabilities WHERE name=?", (name,)).fetchone()
                        if capability and capability["builder_job_id"] == row["id"] and capability["status"] != "active":
                            return None
            except LeaseLost:
                pass
        return self.error("agent calls must use the permission gateway; owner administration is separate", 403)

    @staticmethod
    def success(value):
        return jsonify({"ok": True, "result": value, "error": None})

    @staticmethod
    def error(message, status=400, code=None):
        return jsonify({"ok": False, "result": None, "error": {"message": message, "code": code}}), status

    def catalog(self):
        catalog = self.legacy_tools() + WORK_TOOLS + DOCUMENT_TOOLS + TRADING_TOOLS + EXTRA_TOOLS + self.integrations.tools() + self.improvements.tools()
        out = []
        for original in catalog:
            t = dict(original)
            name = t["name"]
            if name in ADMIN_TOOLS:
                effect = "admin"
            elif name in CONFIRM_TOOLS or t.get("generated"):
                effect = "execute"
            else:
                effect = t.get("effect") or ("read" if t.get("method") == "GET" or name in READ_POSTS else "write" if name in LOCAL_WRITES else "external")
            t["effect"] = effect
            t["requires_confirmation"] = bool(t.get("requires_confirmation")) or (effect in {"admin", "external", "execute"} and not (t.get("generated") and t.get("autonomous_allowed"))) or name in {"workspace_write", "workspace_patch"}
            out.append(t)
        return out

    def _job_policy(self, job_id):
        if not job_id:
            return {}
        with self.store.connect() as db:
            row = db.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        try:
            return json.loads(row[0] or "{}") if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _authorized_copy_prefix(self, actor, job_id, path):
        if not job_id or not isinstance(path, str):
            return False
        with self.store.connect() as db:
            rows = db.execute("SELECT result_json FROM tool_executions WHERE actor=? AND tool='source_copy_create' "
                              "AND state='done' AND request_id LIKE ?", (actor, job_id + ":%")).fetchall()
        normalized = path.replace("\\", "/").strip("/")
        for row in rows:
            try:
                result = json.loads(row[0] or "{}")
                prefix = str((result.get("result") or {}).get("path") or "").replace("\\", "/").strip("/")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if prefix and (normalized == prefix or normalized.startswith(prefix + "/")):
                return True
        return False

    def _authorize(self, actor, spec, args, job_id=None):
        if actor == "owner":
            return True
        if spec["effect"] == "admin":
            return False  # administrative changes are never delegable
        policy = self._job_policy(job_id)
        mode = policy.get("approval_mode")
        if mode not in {None, "suggest", "auto_edit", "full_auto"}:
            return False
        if policy.get("goal_planning"):
            if spec["effect"] == "write" and spec["name"] != "create_goal":
                return False
            if spec["name"] == "create_goal":
                with self.store.connect() as db:
                    created = db.execute("SELECT COUNT(*) FROM tool_executions WHERE actor=? AND tool='create_goal' "
                                         "AND state='done' AND request_id LIKE ?", (actor, job_id + ":%")).fetchone()[0]
                if created >= 3:
                    return False
        if policy.get("self_improvement"):
            if spec["name"] == "source_copy_create":
                requested = str(args.get("path") or "").replace("\\", "/").strip("/")
                configured = str(policy.get("self_improvement_source") or "").replace("\\", "/").strip("/")
                if requested != configured:
                    return False
            if spec["name"] in {"workspace_write", "workspace_patch", "workspace_test", "workspace_diff", "register_artifact"}:
                if not self._authorized_copy_prefix(actor, job_id, args.get("path", "")):
                    return False
            if spec["name"] == "export_patch":
                if not (self._authorized_copy_prefix(actor, job_id, args.get("path", "")) and
                        self._authorized_copy_prefix(actor, job_id, args.get("output", ""))):
                    return False
            # workspace_test is the sole unattended execute permission granted to
            # isolated self-improvement. WorkTools runs it in a read-only,
            # network-disabled Docker container with CPU/RAM/PID/time limits, and
            # the copy-prefix check above prevents testing the authoritative tree.
            # Promotion, deployment, host execution and external effects remain
            # grant-gated.
            if mode in {"auto_edit", "full_auto"} and spec["name"] == "workspace_test":
                return True
            # Confinement is an additional boundary, not a permission bypass.
            # Continue through the selected suggest/auto-edit/full-auto policy.
        if mode == "suggest" and spec["effect"] in {"write", "execute", "external"}:
            return self.store.consume_grant(actor, spec["name"], args)
        if mode in {"auto_edit", "full_auto"} and spec["name"] in {"workspace_write", "workspace_patch", "document_create"}:
            return True
        if spec.get("generated") and spec.get("autonomous_allowed") and not spec.get("requires_confirmation"):
            return True
        if not spec.get("requires_confirmation") and (spec["effect"] == "read" or spec["name"] in LOCAL_WRITES):
            return True
        # Intentional: DocumentTools confines creation to the workspace and uses
        # exclusive (no-clobber) creation. Editing existing files still needs a grant.
        if spec["name"] == "document_create":
            return True
        return self.store.consume_grant(actor, spec["name"], args)

    def invoke(self, actor, name, args, request_id, job_id=None, lease_token=None):
        spec = next((t for t in self.catalog() if t["name"] == name), None)
        if not spec:
            self.store.event("tool.rejected", {"actor": actor, "tool": name, "args": args,
                                               "request_id": request_id, "job_id": job_id,
                                               "error": "unknown tool"})
            return {"ok": False, "error": {"message": "unknown tool", "code": "unknown_tool"}}
        validate(spec["input_schema"], args)
        if job_id:
            with self.store.connect() as db:
                self.store._owned(db, job_id, lease_token)
        # Check cached result before consuming a one-use grant on replay.
        from platform_contracts import digest
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM tool_executions WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
        if previous:
            if previous["fingerprint"] != digest({"name": name, "args": args}):
                raise ValueError("request_id reused for a different action")
            if previous["state"] == "done":
                replay = json.loads(previous["result_json"])
                self.store.event("tool.replayed", {"actor": actor, "tool": name, "args": args,
                                                   "result": replay, "request_id": request_id, "job_id": job_id})
                return replay
            raise LeaseLost("previous action outcome is uncertain; reconciliation required")
        if not self._authorize(actor, spec, args, job_id):
            if not job_id:
                self.store.request_permission(actor, name, args, request_id)
            self.store.event("permission.required", {"actor": actor, "tool": name, "args": args,
                                                     "request_id": request_id, "job_id": job_id})
            return {"ok": False, "error": {"message": "owner grant required for this tool and argument scope", "code": "approval_required"}, "tool": name, "args": args}
        cached = self.store.begin_action(actor, request_id, name, args)
        if cached is not None:
            return cached
        started = time.monotonic()
        self.store.event("tool.requested", {"actor": actor, "tool": name, "effect": spec["effect"],
                                           "args": args, "request_id": request_id, "job_id": job_id})
        pre_hooks = self._safe_hooks(name, "pre", {"actor": actor, "tool": name, "args": args,
                                                   "request_id": request_id, "job_id": job_id}, job_id)
        if pre_hooks["blocking_failed"]:
            result = {"ok": False, "result": None,
                      "error": {"code": "hook_blocked", "message": "a blocking pre-tool hook failed"},
                      "hooks": pre_hooks["runs"]}
            self.store.finish_action(actor, request_id, result)
            self.store.event("tool.completed", {"actor": actor, "tool": name, "args": args, "result": result,
                                                "ok": False, "request_id": request_id, "job_id": job_id})
            return result
        try:
            if name == "discover_tools":
                value = select_tools(self.catalog(), args["query"], args.get("limit", 24))
            elif name == "remember":
                value = self.knowledge.remember(**args)
            elif name == "recall_memory":
                value = self.knowledge.recall(**args)
            elif name == "consolidate_memory":
                value = self.knowledge.consolidate(self.models, **args)
            elif name == "propose_improvement":
                value = self.improvements.propose(**args)
            elif name == "evaluate_improvement":
                value = self.improvements.evaluate(**args)
            elif name == "evolve_improvement":
                value = self.improvements.evolve(**args, promote=False)
            elif name == "platform_status":
                value = self.status()
            elif name == "get_spending":
                value = self.models.spending()
            elif name == "get_current_datetime":
                stamp = time.time()
                local = datetime.now().astimezone()
                value = {"local_iso": local.isoformat(), "local_date": local.date().isoformat(),
                         "local_time": local.timetz().isoformat(), "timezone": str(local.tzinfo),
                         "utc_iso": datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(),
                         "unix_timestamp": stamp}
            elif name == "get_maintenance_status":
                if not self.maintenance_status_file.is_file():
                    value = {"running": False, "message": "maintenance daemon has not written status"}
                elif self.maintenance_status_file.stat().st_size > 1_000_000:
                    raise ValueError("maintenance status file is unexpectedly large")
                else:
                    value = json.loads(self.maintenance_status_file.read_text(encoding="utf-8"))
            elif name in {t["name"] for t in WORK_TOOLS}:
                value = self.work.invoke(name, args)
            elif name in {t["name"] for t in DOCUMENT_TOOLS}:
                value = self.documents.invoke(name, args)
            elif name in {t["name"] for t in TRADING_TOOLS}:
                value = self.trading.invoke(name, args)
            elif name.startswith("ext_"):
                value = self.integrations.invoke(name, args)
                price = spec.get("pricing", {}).get("request_usd") if isinstance(spec.get("pricing"), dict) else None
                self.store.meter(name.split("_", 2)[1], name, cost_usd=price)
            elif name.startswith("skill_"):
                value = self.improvements.run_capability(self.improvements.version(spec["version_id"]), args)
            else:
                # Registry-derived local endpoint, not an arbitrary model-supplied URL.
                with self.app.test_client() as client:
                    response = client.open(spec["path"], method=spec["method"], headers={"X-API-Key": self.owner_key},
                                           query_string=args if spec["method"] == "GET" else None,
                                           json=args if spec["method"] != "GET" else None)
                    value = response.get_json()
                    if response.status_code >= 400:
                        raise RuntimeError(canonical(value))
                if isinstance(value, dict) and "ok" in value:
                    result = value
                else:
                    result = {"ok": True, "result": value, "error": None}
                post_hooks = self._safe_hooks(name, "post", {"actor": actor, "tool": name, "args": args,
                                                             "result": result, "request_id": request_id,
                                                             "job_id": job_id}, job_id)
                if post_hooks["runs"]:
                    result["hooks"] = post_hooks["runs"]
                result = protect_tool_envelope(name, spec, result)
                self.store.finish_action(actor, request_id, result)
                self.store.event("tool.completed", {"actor": actor, "tool": name, "args": args,
                                                    "result": result, "ok": result["ok"],
                                                    "elapsed_ms": (time.monotonic() - started) * 1000,
                                                    "request_id": request_id, "job_id": job_id})
                return result
            result = {"ok": not (isinstance(value, dict) and value.get("ok") is False), "result": value, "error": None}
        except Exception as exc:
            # External/execute failures may follow a committed side effect. Keep
            # the journal started; an owner reconciles rather than retrying blindly.
            if spec["effect"] in {"external", "execute"}:
                result = {"ok": False, "error": {"code": "uncertain", "message": str(exc)[:2000]}}
                self.store.event("action.uncertain", {"actor": actor, "tool": name, "args": args,
                                                      "request_id": request_id, "job_id": job_id,
                                                      "error": str(exc)[:4000],
                                                      "traceback": traceback.format_exc(limit=20)})
                return result
            result = {"ok": False, "error": {"code": "tool_failed", "message": str(exc)[:2000]}}
            failure_traceback = traceback.format_exc(limit=20)
        else:
            failure_traceback = None
        post_hooks = self._safe_hooks(name, "post", {"actor": actor, "tool": name, "args": args,
                                                     "result": result, "request_id": request_id,
                                                     "job_id": job_id}, job_id)
        if post_hooks["runs"]:
            result["hooks"] = post_hooks["runs"]
        result = protect_tool_envelope(name, spec, result)
        self.store.finish_action(actor, request_id, result)
        self.store.event("tool.completed", {"actor": actor, "tool": name, "args": args,
                                            "result": result, "ok": result["ok"],
                                            "elapsed_ms": (time.monotonic() - started) * 1000,
                                            "request_id": request_id, "job_id": job_id,
                                            "traceback": failure_traceback})
        return result

    def status(self):
        with self.store.connect() as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM agent_jobs GROUP BY status")}
            providers = [dict(r) for r in db.execute("SELECT provider,COUNT(*) runs,AVG(ok) success_rate,AVG(latency_ms) latency_ms FROM provider_runs GROUP BY provider")]
            events = [{**dict(r), "data": json.loads(r["data_json"])} for r in db.execute("SELECT * FROM platform_events ORDER BY id DESC LIMIT 30")]
            artifacts = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            versions = [dict(r) for r in db.execute("SELECT id,name,kind,status,created_at FROM improvement_versions ORDER BY created_at DESC LIMIT 30")]
            evaluations = [dict(r) for r in db.execute("SELECT id,version_id,suite,score,passed,created_at FROM evaluations ORDER BY created_at DESC LIMIT 30")]
            schedules = [dict(r) for r in db.execute("SELECT name,enabled,next_run,last_job_id FROM task_schedules ORDER BY name")]
            prompt_presets = [{**dict(r), "browser_escalation": bool(r["browser_escalation"])}
                              for r in db.execute("SELECT * FROM prompt_presets ORDER BY name")]
            outcomes = []
            for row in db.execute("SELECT id,status,result_json FROM agent_jobs WHERE status IN ('done','failed','blocked') ORDER BY updated_at DESC LIMIT 100"):
                result = json.loads(row["result_json"] or "{}")
                outcomes.append({"id": row["id"], "status": row["status"], "verification": (result.get("verification") or {}).get("status", "unverified")})
        maintenance = {"running": False}
        try:
            if self.maintenance_status_file.is_file():
                maintenance = json.loads(self.maintenance_status_file.read_text(encoding="utf-8"))
                maintenance["running"] = time.time() - float(maintenance.get("timestamp", 0)) < 180
        except Exception as exc:
            maintenance = {"running": False, "error": str(exc)}
        return {"jobs": counts, "providers": providers, "model_routes": self.models.describe(),
                "spending": self.models.spending(), "maintenance": maintenance, "artifacts": artifacts, "events": events,
                "improvements": versions, "evaluations": evaluations, "schedules": schedules,
                "prompt_presets": prompt_presets, "recent_outcomes": outcomes,
                "trading": self.trading.trading_data_status()}

    def _routes(self):
        app = self.app
        def route(path, methods=("POST",), owner=False):
            def register(fn):
                from functools import wraps
                @wraps(fn)
                def wrapped(**kwargs):
                    if owner and getattr(g, "actor", None) != "owner":
                        return self.error("owner access required", 403)
                    try:
                        return self.success(fn(request.get_json(silent=True) or dict(request.args), **kwargs))
                    except LeaseLost as e:
                        self.store.event("request.error", {"actor": getattr(g, "actor", None), "path": request.path,
                                                          "method": request.method, "error_type": type(e).__name__,
                                                          "error": str(e), "job_id": request.headers.get("X-Job-Id")})
                        return self.error(str(e), 409, "lease_or_action_uncertain")
                    except Exception as e:
                        self.store.event("request.error", {"actor": getattr(g, "actor", None), "path": request.path,
                                                          "method": request.method, "input": request.get_json(silent=True),
                                                          "error_type": type(e).__name__, "error": str(e)[:4000],
                                                          "traceback": traceback.format_exc(limit=20),
                                                          "job_id": request.headers.get("X-Job-Id")})
                        from jsonschema import ValidationError, SchemaError
                        if isinstance(e, (ValueError, KeyError, TypeError, ValidationError, SchemaError)):
                            return self.error(str(e)[:2000], 400)
                        return self.error(type(e).__name__ + ": " + str(e)[:1000], 502)
                app.add_url_rule(path, "universal_" + fn.__name__, wrapped, methods=list(methods))
                return wrapped
            return register

        @app.route("/api/tool-gateway", methods=["POST"])
        def tool_gateway():
            d = request.get_json(silent=True) or {}
            try:
                result = self.invoke(g.actor, d["name"], d.get("args", {}), d["request_id"], d.get("job_id"), d.get("lease_token"))
                return jsonify(result)
            except LeaseLost as e:
                self.store.event("request.error", {"actor": getattr(g, "actor", None), "path": request.path,
                                                  "method": request.method, "error_type": type(e).__name__,
                                                  "error": str(e), "job_id": d.get("job_id")})
                return self.error(str(e), 409, "uncertain")
            except Exception as e:
                self.store.event("request.error", {"actor": getattr(g, "actor", None), "path": request.path,
                                                  "method": request.method, "input": d,
                                                  "error_type": type(e).__name__, "error": str(e)[:4000],
                                                  "traceback": traceback.format_exc(limit=20),
                                                  "job_id": d.get("job_id")})
                return self.error(str(e)[:2000], 400)

        @route("/api/models/chat")
        def model_chat(d):
            browser_escalation = False
            if request.headers.get("X-Job-Id"):
                with self.store.connect() as db:
                    owned_job = self.store._owned(db, request.headers["X-Job-Id"], request.headers.get("X-Lease-Token"))
                    payload = json.loads(owned_job["payload_json"] or "{}")
                    browser_escalation = bool(payload.get("_browser_escalation_authorized"))
            if len(canonical(d)) > 2_000_000:
                raise ValueError("model request exceeds 2 MB")
            messages = list(d["messages"])
            for version in self.improvements.active("prompt"):
                content = version["content"]
                if d.get("task_type", "general") in content.get("task_types", []):
                    messages.insert(0, {"role": "system", "content": content["text"]})
            return self.models.chat(messages, d.get("tools"), d.get("temperature"), d.get("task_type", "general"),
                                    provider=d.get("provider"), allow_browser=browser_escalation,
                                    trace={"job_id": request.headers.get("X-Job-Id"), "actor": g.actor},
                                    prefer_fallback=bool(d.get("prefer_fallback", False)))

        @route("/api/owner/audit", ("GET",), owner=True)
        def audit(d):
            return self.store.audit(limit=d.get("limit", 100), kind=d.get("kind", ""), job_id=d.get("job_id", ""),
                                    after_id=d.get("after_id", 0))

        @route("/api/runtime/jobs/event")
        def runtime_job_event(d):
            job_id = str(d.get("id") or "")
            with self.store.connect() as db:
                self.store._owned(db, job_id, d.get("lease_token"))
            kind = str(d.get("kind") or "")
            if not kind.startswith(("dsh.", "agent.")) or len(kind) > 120:
                raise ValueError("runtime job events must use the dsh.* or agent.* namespace")
            data = d.get("data") or {}
            if not isinstance(data, dict) or len(canonical(data)) > 100_000:
                raise ValueError("runtime job event data must be an object no larger than 100 KB")
            self.store.event(kind, {**data, "job_id": job_id, "actor": g.actor})
            return {"recorded": True}

        @route("/api/platform/status", ("GET",))
        def status(d):
            return self.status()

        @route("/api/owner/spending", ("GET",), owner=True)
        def spending(d):
            return self.models.spending()

        @route("/api/owner/grants", owner=True)
        def grant(d):
            return self.store.grant(d["actor"], d["tool"], d.get("constraints", {}), d.get("expires_at", time.time() + 3600), d.get("uses", 100))

        @route("/api/owner/usage", owner=True)
        def record_usage(d):
            if not d.get("service") or not d.get("operation"):
                raise ValueError("service and operation required")
            self.store.meter(d["service"], d["operation"], requests=d.get("requests", 1),
                             units=d.get("units"), cost_usd=d.get("cost_usd"))
            return {"recorded": True}

        @route("/api/owner/usage/check", ("GET",), owner=True)
        def usage_check(d):
            return self.store.usage_check(d.get("service", ""), d.get("daily_request_limit", 0))

        @route("/api/owner/grants/revoke", owner=True)
        def revoke(d):
            with self.store.connect() as db:
                cur = db.execute("UPDATE permission_grants SET remaining=0 WHERE id=?", (d["id"],))
            return {"revoked": cur.rowcount == 1}

        @route("/api/owner/integrations", owner=True)
        def integration_install(d):
            return self.integrations.install(d)

        @route("/api/owner/integrations/disable", owner=True)
        def integration_disable(d):
            with self.store.connect() as db:
                db.execute("UPDATE integration_manifests SET enabled=0 WHERE name=?", (d["name"],))
            return {"name": d["name"], "enabled": False}

        @route("/api/owner/mcp/discover", owner=True)
        def mcp_discover(d):
            return asyncio.run(mcp_request(d, "list"))

        @route("/api/owner/jobs/control", owner=True)
        def job_control(d):
            return self.store.control(d["id"], d["action"])

        @route("/api/owner/schedules", owner=True)
        def schedule(d):
            return self.store.schedule(**d)

        @route("/api/owner/presets", owner=True)
        def preset_save(d):
            return self.store.save_prompt_preset(**d)

        @route("/api/owner/presets/delete", owner=True)
        def preset_delete(d):
            return self.store.delete_prompt_preset(d.get("name"))

        @route("/api/owner/jobs/budget", owner=True)
        def job_budget(d):
            with self.store.connect(True) as db:
                row = db.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (d["id"],)).fetchone()
                if not row:
                    raise ValueError("unknown job")
                payload = json.loads(row[0] or "{}")
                for key, ceiling in (("max_steps", 100), ("max_seconds", 86400)):
                    if key in d:
                        if not 1 <= int(d[key]) <= ceiling:
                            raise ValueError("invalid budget")
                        payload[key] = int(d[key])
                db.execute("UPDATE agent_jobs SET payload_json=? WHERE id=?", (canonical(payload), d["id"]))
            return {"id": d["id"], "payload": payload}

        @route("/api/owner/actions/reconcile", owner=True)
        def reconcile(d):
            self.store.reconcile(d["actor"], d["request_id"], d["result"])
            return {"reconciled": True}

        @route("/api/runtime/jobs/heartbeat")
        def heartbeat(d):
            return self.store.heartbeat(d["id"], d["lease_token"])

        @route("/api/runtime/jobs/checkpoint")
        def checkpoint(d):
            return self.store.checkpoint(d["id"], d["lease_token"], d.get("state"))

        @route("/api/owner/memory/correct", owner=True)
        def memory_correct(d):
            return self.knowledge.remember(**d)

        @route("/api/owner/memory/expire", owner=True)
        def memory_expire(d):
            return self.knowledge.expire(d["id"])

        @route("/api/owner/evaluations/suite", owner=True)
        def eval_suite(d):
            return self.improvements.suite(**d)

        @route("/api/owner/evaluations/run", owner=True)
        def eval_run(d):
            return self.improvements.evaluate(**d)

        @route("/api/owner/improvements/promote", owner=True)
        def promote(d):
            return self.improvements.promote(**d)

        @route("/api/owner/improvements/evolve", owner=True)
        def evolve(d):
            return self.improvements.evolve(**d)

        @route("/api/owner/improvements/rollback", owner=True)
        def rollback(d):
            return self.improvements.rollback(d["version_id"])
