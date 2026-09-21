#!/usr/bin/env python3
"""Durable executive/background worker for the agent platform.

Consumes leased SQLite jobs, checkpoints work, and routes model/tool requests
through the core's shared model gateway and server-enforced permission policy.

Job kinds:
  agent             - general bounded objective execution with safe tools
  workflow          - execute reusable event-triggered workflow steps
  capability_build  - generate + sandbox-test a new tool, stopping before approval

The worker cannot administer permissions or activate capabilities. Execution
and external actions require an owner-issued standing grant.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import traceback
from typing import Any

import requests
import threading
from core_client import CoreClient
from dsh_integration import run_headless as run_dsh_headless
from durable_agent import run as run_durable_agent

client = CoreClient("assistant")

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "placeholder")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT", "")
CORE_ROOT = os.environ.get("CORE_ROOT", "")
WORKER_ID = os.environ.get("ASSISTANT_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
MAX_TOOL_ITERS = int(os.environ.get("ASSISTANT_MAX_TOOL_ITERS", "12"))
CAPABILITY_RETRIES = int(os.environ.get("CAPABILITY_BUILD_RETRIES", "3"))
RESOURCE_PAUSE_LEVEL = os.environ.get("ASSISTANT_RESOURCE_PAUSE_LEVEL", "warning").lower()


def resources_available():
    """Avoid starting new model work while the host is under configured pressure."""
    if RESOURCE_PAUSE_LEVEL == "off":
        return True
    result = core_get("/api/system/health")
    if not result.get("ok"):
        return True  # Core lease limits remain available if telemetry is absent.
    severity = (result.get("result") or {}).get("severity", "ok")
    levels = {"ok": 0, "warning": 1, "critical": 2}
    return levels.get(severity, 0) < levels.get(RESOURCE_PAUSE_LEVEL, 1)

def headers() -> dict:
    return client.headers()


def core_get(path, params=None):
    return client.request("GET", path, params=params)


def core_post(path, data=None):
    return client.request("POST", path, data=data or {})


def fetch_tools():
    return client.tools()


def to_openai_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]


def llm_chat(messages, tools=None, temperature=None):
    return client.chat(messages, tools, temperature, task_type="capability_build")


def tool_is_background_safe(tool):
    # Authority is checked on the core against standing grants for every call.
    return tool.get("effect") != "admin"


def call_tool(tool, args, request_id=None):
    return client.invoke(tool["name"], args, request_id=request_id)


def build_context(objective: str, max_chars: int = 18000) -> str:
    r = core_post("/api/context/build", {"objective": objective, "max_chars": max_chars})
    if r.get("ok"):
        return (r.get("result") or {}).get("context", "")
    return ""


def run_agent(objective, *, role="executive worker", allowed_names=None, extra_context="", state_key="agent",
              max_steps=None):
    bounded_steps = max(1, min(int(max_steps or MAX_TOOL_ITERS), 60))
    return run_durable_agent(client, objective, role=role, allowed_names=allowed_names,
                             extra_context=extra_context, state_key=state_key, max_steps=bounded_steps)


def run_self_improvement(job: dict) -> dict:
    """Deterministically establish the safe copy before asking a small model to edit."""
    payload = job.get("payload") or {}
    whole = client.checkpoint()
    bootstrap = whole.get("self_improvement_bootstrap")
    if not bootstrap:
        tools = {tool["name"]: tool for tool in fetch_tools()}
        required = {"source_bug_scan", "source_copy_create"}
        if not required.issubset(tools):
            return {"ok": False, "error": "self-improvement bootstrap tools are unavailable"}
        source_path = str(payload.get("self_improvement_source") or "")
        scan = call_tool(tools["source_bug_scan"], {"path": source_path, "limit": 100},
                         request_id=f"{job['id']}:bootstrap:scan")
        if not scan.get("ok"):
            return {"ok": False, "error": "source scan failed", "detail": scan}
        copy = call_tool(tools["source_copy_create"], {"path": source_path, "label": "overnight-repair"},
                         request_id=f"{job['id']}:bootstrap:copy")
        if not copy.get("ok"):
            return {"ok": False, "error": "isolated source copy failed", "detail": copy}
        bootstrap = {"scan": scan.get("result"), "copy": copy.get("result")}
        whole["self_improvement_bootstrap"] = bootstrap
        client.checkpoint(whole)
    copy_path = str((bootstrap.get("copy") or {}).get("path") or "")
    if not copy_path.startswith("self_improvement_copies/"):
        return {"ok": False, "error": "bootstrap did not return an isolated copy path"}
    findings = (bootstrap.get("scan") or {}).get("findings") or []
    if not findings:
        return {
            "ok": True,
            "answer": "Static analysis found no reproducible candidate, so the isolated copy was left unchanged. No patch was promoted.",
            "actions": [
                {"tool": "source_bug_scan", "ok": True, "result": bootstrap.get("scan")},
                {"tool": "source_copy_create", "ok": True, "result": bootstrap.get("copy")},
            ],
            "verification": {"status": "passed", "checks": [
                {"name": "reproducible_candidate_required", "passed": True,
                 "feedback": "No static candidate exists; safe no-change completion."},
                {"name": "authoritative_source_unchanged", "passed": True},
            ]},
            "plan": [{"step": "scan authoritative source", "status": "done"},
                     {"step": "leave isolated copy unchanged when no candidate exists", "status": "done"}],
            "no_change": True,
        }
    context = json.dumps({
        "authoritative_source": "read-only",
        "isolated_copy": copy_path,
        "static_scan_findings": findings[:40],
        "mandatory_next_action": (
            "Inspect files inside isolated_copy and use only available tools. Do not target .venv, site-packages, "
            "or any path outside isolated_copy. If no finding is reproducible, report that honestly and make no edit."
        ),
    }, ensure_ascii=False)
    allowed = {
        "workspace_list", "workspace_read", "workspace_patch", "workspace_test", "workspace_diff",
        "export_patch", "register_artifact", "verify_artifact", "update_plan", "build_context",
        "recall_memory", "source_bug_scan",
    }
    return run_agent(job.get("objective") or "", allowed_names=allowed, extra_context=context,
                     max_steps=payload.get("max_steps"))


AUTO_REPAIR_FAILURES = {"model_stuck_repeating", "step_budget_exhausted"}


def maybe_queue_auto_repair(job: dict, result: dict) -> dict:
    """Turn deterministic agent-control failures into isolated repair work.

    The enqueue itself goes through the tool gateway, so suggest mode pauses for
    an owner grant. The repair endpoint always creates a copy-bound job and
    never promotes its patch.
    """
    payload = job.get("payload") or {}
    code = str(result.get("failure_code") or "")
    if (not payload.get("auto_repair_on_failure") or payload.get("self_improvement")
            or code not in AUTO_REPAIR_FAILURES):
        return result
    diagnostics = result.get("diagnostics") or {}
    # A normal job that used tools and merely ran out of a deliberately small
    # budget is not evidence of a harness defect. Research cycles remain
    # eligible because their completion contract requires a report.
    if (code == "step_budget_exhausted" and not payload.get("research_cycle")
            and diagnostics.get("successful_tool_calls", 0) > 0):
        return result
    focus = (
        f"Automatically diagnose failure {code} from job {job.get('id')}. "
        f"The job objective was: {str(job.get('objective') or '')[:2000]}. "
        f"Observed diagnostics: {json.dumps(diagnostics, sort_keys=True)[:2000]}. "
        "Reproduce the control-flow defect with a regression test, make the smallest safe fix in the isolated copy, "
        "and export a patch for operator review. Do not promote or deploy it."
    )
    request_args = {"source_path": "", "focus": focus, "priority": min(1.0, float(job.get("priority") or 0.5) + 0.1),
                    "trigger_job_id": job.get("id"), "failure_code": code}
    queued = client.invoke("queue_self_improvement", request_args,
                           request_id=f"{job['id']}:auto_repair:{code}")
    result.setdefault("actions", []).append({"tool": "queue_self_improvement", "args": request_args,
                                               "ok": bool(queued.get("ok")), "result": queued.get("result"),
                                               "error": queued.get("error")})
    recovery = {"attempted": True, "failure_code": code, "permission_mode": payload.get("approval_mode"),
                "queued": bool(queued.get("ok")), "result": queued.get("result"), "error": queued.get("error")}
    result["automatic_recovery"] = recovery
    try:
        core_post("/api/runtime/jobs/event", {"id": job["id"], "lease_token": job["lease_token"],
                                               "kind": "agent.auto_repair", "data": recovery})
    except Exception:
        pass
    return result


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(text[start:end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("model did not return a valid JSON object")


def normalize_capability_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    if not name or not name[0].isalpha():
        name = "tool_" + name
    return name[:60]


def factory_prompt_selector():
    """Optional Obsidian prompt evolution hook; absence of PyYAML/prompts is fine."""
    if not OBSIDIAN_VAULT:
        return None, ""
    try:
        from prompt_selector import best_prompt, best_prompt_path
        return best_prompt_path(OBSIDIAN_VAULT, "capability_build"), best_prompt(OBSIDIAN_VAULT, "capability_build") or ""
    except Exception:
        return None, ""


def bump_factory_prompt(path, passed: bool):
    if not path:
        return
    try:
        from prompt_selector import bump_success_rate
        bump_success_rate(path, passed)
    except Exception:
        pass


def capability_generation_prompt(objective: str, preferred_name: str = "", failure: str = "") -> str:
    correction = f"\nPREVIOUS TEST FAILURE:\n{failure[:6000]}\nFix the smallest relevant issue; do not redesign unrelated logic.\n" if failure else ""
    return f"""Design a small reusable Python capability for this objective:
{objective}

Preferred capability name: {preferred_name or '(choose a concise snake_case name)'}
{correction}
Return JSON ONLY with exactly these fields:
{{
  "name": "snake_case_name",
  "description": "what the reusable tool does",
  "input_schema": {{"type":"object","properties":{{...}},"required":[...]}},
  "network_enabled": false,
  "requires_confirmation": false,
  "autonomous_allowed": false,
  "risk": "low",
  "code": "complete Python source",
  "test_code": "complete Python source"
}}

Capability contract:
- tool.py MUST define run(args: dict) and return a JSON-serializable value.
- Use the Python standard library only in V1.
- test_tool.py must load /app/tool.py (or tool.py when run locally) and exercise run() with deterministic assertions.
- No shelling out, no credentials, no hidden downloads, no access outside the capability directory.
- Network must remain false unless the objective inherently requires network access.
- autonomous_allowed should default false; human approval decides activation separately.
- Keep the capability generic enough to reuse for similar future requests.
"""


def build_capability(job: dict) -> dict:
    objective = job.get("objective") or ""
    payload = job.get("payload") or {}
    whole = client.checkpoint()
    state = whole.get("factory", {"phase": "generate", "attempts": 0, "failure": "", "name": ""})
    def save():
        whole["factory"] = state
        client.checkpoint(whole)
    while state["attempts"] < CAPABILITY_RETRIES or state["phase"] != "generate":
        if state["phase"] == "done":
            return state["result"]
        if state["phase"] == "generate":
            state["attempts"] += 1
            save()
            prompt = capability_generation_prompt(objective, state["name"] or payload.get("preferred_name", ""), state["failure"])
            msg = llm_chat([{"role": "system", "content": "Return strict JSON capability artifacts only."}, {"role": "user", "content": prompt}], tools=None, temperature=0.2)
            try:
                spec = parse_json_object(msg.get("content") or "")
            except (ValueError, TypeError) as e:
                state["failure"] = str(e)
                save()
                continue
            if not state["name"]:
                preferred = payload.get("preferred_name") or spec.get("name", "capability")
                state["name"] = normalize_capability_name(preferred)[:40] + "_" + job["id"][-10:].lower()
            state["spec"] = spec
            state["phase"] = "stage" if state.get("proposed") else "propose"
            save()
        spec = state["spec"]
        name = state["name"]
        if state["phase"] == "propose":
            prop = core_post("/api/capabilities/propose", {
                "name": name, "description": spec.get("description") or objective[:300], "objective": objective,
                "input_schema": spec.get("input_schema") or {"type": "object", "properties": {}},
                "network_enabled": bool(spec.get("network_enabled", False)),
                "requires_confirmation": bool(spec.get("requires_confirmation", False)), "autonomous_allowed": False,
                "risk": spec.get("risk", "low"),
            })
            if not prop.get("ok"):
                return {"ok": False, "stage": "propose", "error": prop}
            state.update(proposed=True, phase="stage")
            save()
        if state["phase"] == "stage":
            staged = core_post("/api/capabilities/stage", {"name": name, "code": spec.get("code") or "", "test_code": spec.get("test_code") or ""})
            if not staged.get("ok"):
                state.update(phase="generate", failure=json.dumps(staged))
                save()
                continue
            state["phase"] = "test"
            save()
        tested = core_post("/api/capabilities/test", {"name": name})
        if tested.get("ok"):
            result = {"ok": True, "name": name, "attempts": state["attempts"], "status": "tested",
                      "next_action": f"Approve capability '{name}' to activate cap_{name}. Independent improvement suites provide stronger validation than these generated tests."}
            state.update(phase="done", result=result)
            save()
            return result
        state.update(phase="generate", failure=json.dumps(tested))
        save()
    return {"ok": False, "name": state["name"], "attempts": state["attempts"], "error": state["failure"] or "build failed"}


def dot_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def render_value(value: Any, runtime: dict) -> Any:
    if isinstance(value, dict):
        return {k: render_value(v, runtime) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, runtime) for v in value]
    if not isinstance(value, str):
        return value

    def repl(m):
        v = dot_get(runtime, m.group(1))
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return "" if v is None else str(v)

    return _TEMPLATE.sub(repl, value)


def run_workflow(job: dict) -> dict:
    whole = client.checkpoint()
    runtime = whole.get("workflow")
    if runtime is None:
        payload = job.get("payload") or {}
        wf_resp = core_get("/api/runtime/workflow", {"id": payload.get("workflow_id")})
        ev_resp = core_get("/api/runtime/event", {"id": payload.get("event_id")})
        if not wf_resp.get("ok"):
            return {"ok": False, "error": wf_resp}
        runtime = {"event": ev_resp.get("result") or {}, "workflow": wf_resp["result"], "steps": [], "index": 0}
        whole["workflow"] = runtime
        client.checkpoint(whole)
    workflow = runtime["workflow"]
    tools = {t["name"]: t for t in fetch_tools()}
    while runtime["index"] < len(workflow.get("steps") or []):
        index = runtime["index"]
        step = render_value(workflow["steps"][index], runtime)
        typ = step.get("type", "tool")
        if typ == "tool":
            name = step.get("tool")
            if name not in tools:
                result = {"ok": False, "error": {"message": f"unknown tool {name}"}}
            else:
                result = call_tool(tools[name], step.get("args") or {}, request_id=f"{job['id']}:workflow:{index}")
        elif typ == "agent":
            result = run_agent(step.get("objective") or "", role=step.get("role", "workflow worker"),
                               allowed_names=step.get("allowed_tools"), extra_context=json.dumps(runtime, default=str)[-12000:],
                               state_key=f"workflow_agent_{index}")
        else:
            return {"ok": False, "error": f"unknown step type {typ}"}
        error = result.get("error") or {}
        if result.get("blocked") or (isinstance(error, dict) and error.get("code") in {"approval_required", "uncertain"}):
            return {"ok": False, "blocked": True, "workflow": workflow.get("name"), "pending_step": index, "result": result}
        if step.get("stop_on_error", True) and not result.get("ok", True):
            return {"ok": False, "workflow": workflow.get("name"), "step": index, "result": result}
        runtime["steps"].append({"index": index, "type": typ, "result": result})
        runtime["index"] += 1
        whole = client.checkpoint()  # preserve nested agent checkpoints
        whole["workflow"] = runtime
        client.checkpoint(whole)
    return {"ok": True, "workflow": workflow.get("name"), "steps": runtime["steps"]}


def run_dsh_job(job: dict, cancelled=None) -> dict:
    payload = job.get("payload") or {}
    whole = client.checkpoint()
    state = whole.get("dsh") or {"phase": "starting", "started_at": time.time()}
    whole["dsh"] = state
    client.checkpoint(whole)

    def record(kind, data):
        try:
            response = core_post("/api/runtime/jobs/event", {"id": job["id"], "lease_token": job["lease_token"],
                                                               "kind": kind, "data": data})
            if not response.get("ok"):
                print(json.dumps({"job": job["id"], "dsh_event_rejected": response}), flush=True)
        except Exception as exc:
            print(f"[DSH audit event failed] {type(exc).__name__}: {exc}", flush=True)

    operator_objective = str(job.get("objective") or "").strip()
    execution_objective = f"""HEADLESS CODING EXECUTION

Complete the operator's request as an implementation task in the supplied Git workspace. Inspect the existing source and tests first, make the smallest coherent code changes, and run relevant tests. Do not stop at a plan or ask broad clarification when the repository can answer the question. If external login or owner approval is genuinely required, implement every safe local prerequisite you can and finish with the exact remaining operator action. Never claim that a native Windows application can be controlled by Playwright; Playwright browser routes target websites.

OPERATOR REQUEST:
{operator_objective}

SUCCESS CONTRACT:
- Produce attributable repository edits or a precise evidence-backed explanation of the concrete blocker.
- Preserve existing unrelated work.
- Do not expose credentials or bypass service policies.
- End with changed files, tests run, and remaining operator actions.
"""
    result = run_dsh_headless(execution_objective, payload.get("workspace") or CORE_ROOT,
                              timeout_seconds=payload.get("max_seconds", 3600), event=record,
                              cancelled=cancelled, require_clean=not bool(payload.get("allow_dirty", False)))
    result.setdefault("actions", [{"tool": "dsh_headless", "ok": bool(result.get("ok")),
                                    "result": {"returncode": result.get("returncode"),
                                               "git_status": result.get("git_status"),
                                               "native_session_logs": result.get("native_session_logs", [])}}])
    whole = client.checkpoint()
    whole["dsh"] = {"phase": "complete" if result.get("ok") else "blocked" if result.get("blocked") else "failed",
                    "started_at": state["started_at"], "finished_at": time.time(),
                    "returncode": result.get("returncode"), "git_status": result.get("git_status"),
                    "native_session_logs": result.get("native_session_logs", [])}
    client.checkpoint(whole)
    return result


def process_job(job: dict, cancelled=None) -> tuple[str, dict]:
    kind = job.get("kind")
    if kind == "agent":
        if (job.get("payload") or {}).get("engine") == "dsh":
            result = run_dsh_job(job, cancelled)
        elif (job.get("payload") or {}).get("self_improvement"):
            result = run_self_improvement(job)
        else:
            result = run_agent(job.get("objective") or "", max_steps=(job.get("payload") or {}).get("max_steps"))
        if not result.get("ok"):
            result = maybe_queue_auto_repair(job, result)
        return ("done" if result.get("ok") else "blocked" if result.get("blocked") else "failed"), result
    if kind == "capability_build":
        result = build_capability(job)
        if result.get("ok"):
            return "awaiting_approval", result
        return "failed", result
    if kind == "workflow":
        result = run_workflow(job)
        if result.get("blocked"):
            return "blocked", result
        return ("done" if result.get("ok") else "blocked" if result.get("blocked") else "failed"), result
    return "failed", {"ok": False, "error": f"unknown job kind: {kind}"}


def claim_job() -> dict | None:
    r = core_post("/api/runtime/jobs/claim", {"worker_id": WORKER_ID})
    return r.get("result") if r.get("ok") else None


def complete_job(job_id: str, status: str, result: dict):
    return core_post("/api/runtime/jobs/complete", {"id": job_id, "lease_token": client.job["lease_token"], "status": status, "result": result})


def run_once() -> bool:
    if not resources_available():
        return False
    job = claim_job()
    if not job:
        return False
    client.job = job
    stop = threading.Event()
    lease_failed = threading.Event()
    def renew():
        while not stop.wait(30):
            try:
                result = core_post("/api/runtime/jobs/heartbeat", {"id": job["id"], "lease_token": job["lease_token"]})
                if not result.get("ok"):
                    lease_failed.set()
                    return
            except requests.RequestException:
                lease_failed.set()
                return
    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        status, result = process_job(job, lease_failed.is_set)
    except Exception as e:
        status, result = "failed", {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "traceback": traceback.format_exc(limit=20)}
    try:
        if not lease_failed.is_set():
            response = complete_job(job["id"], status, result)
            if not response.get("ok"):
                print(json.dumps({"job": job["id"], "completion_rejected": response}), flush=True)
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        client.job = None
    print(json.dumps({"job": job["id"], "status": status, "result": result}, ensure_ascii=False, default=str), flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="claim at most one job and exit")
    ap.add_argument("--interval", type=float, default=5.0, help="idle poll interval in seconds")
    args = ap.parse_args()
    if args.once:
        run_once()
        return
    while True:
        try:
            worked = run_once()
            if not worked:
                time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[worker error] {type(e).__name__}: {e}", flush=True)
            time.sleep(max(2.0, args.interval))


if __name__ == "__main__":
    main()
