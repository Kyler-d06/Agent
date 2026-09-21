#!/usr/bin/env python3
"""Autonomous hypothesis/research worker for core_server.py.

The worker is intentionally a thin orchestration layer. It discovers tools
from core_server's /api/tools, gives a bounded safe subset to the configured
OpenAI-compatible model/harness, and lets the model perform one research
cycle at a time. Point LLM_BASE_URL at your local harness/router; that harness
can decide whether to use a local model or a permitted Playwright-backed web
AI provider.

Default autonomous policy is research-only: no file writes, code execution,
audio control, or git commits. Those remain available to your interactive
Telegram agent/human approval flow.

Setup:
  pip install requests
  export CORE_URL=http://127.0.0.1:5077
  export CORE_API_KEY=...
  export LLM_BASE_URL=http://127.0.0.1:20128/v1
  export LLM_MODEL=your-local-or-router-model
  python3 discovery_worker.py --once
  python3 discovery_worker.py --interval 1800
"""
import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from core_client import CoreClient
from platform_contracts import validate

platform_client = CoreClient("discovery")

CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "placeholder")
LLM_MODEL = os.environ.get("LLM_MODEL", "local-model")
MAX_TOOL_ITERS = int(os.environ.get("DISCOVERY_MAX_TOOL_ITERS", "8"))
MAX_PARALLEL_READS = max(1, min(int(os.environ.get("DISCOVERY_MAX_PARALLEL_READS", "2")), 8))
MAX_TOOL_RESULT_CHARS = max(2000, min(int(os.environ.get("DISCOVERY_MAX_TOOL_RESULT_CHARS", "3500")), 20000))
EMPTY_RESPONSE_RETRIES = max(0, min(int(os.environ.get("DISCOVERY_EMPTY_RESPONSE_RETRIES", "2")), 4))
RESOURCE_PAUSE_LEVEL = os.environ.get("DISCOVERY_RESOURCE_PAUSE_LEVEL", "warning").lower()
ALLOW_RESEARCH_WRITES = os.environ.get("DISCOVERY_ALLOW_RESEARCH_WRITES", "false").lower() in {"1", "true", "yes"}
_CAPABILITY_DENIAL = re.compile(
    r"\b(?:i cannot|i can't|unable to|do not have (?:access|the ability)|cannot access|can't access)\b"
    r".{0,160}\b(?:tool|filesystem|file system|local (?:file|host)|execute|code)\b",
    re.IGNORECASE | re.DOTALL,
)
_INCOMPLETE_INTENT = re.compile(
    r"^\s*(?:let me|i(?:'ll| will| need to| am going to)|next(?:,|\s)|first(?:,|\s))\b",
    re.IGNORECASE,
)

# Safe autonomous research surface. Deliberately excludes run_file, save_file,
# toggle_ambient and commit_research. Experiments can be planned/recorded, but
# executable code remains in the human-reviewed path.
AUTONOMOUS_TOOLS = {
    "search_memory", "web_search", "read_page", "search_context", "read_context_file", "build_context",
    "create_question", "create_hypothesis", "get_hypothesis", "update_hypothesis",
    "add_evidence", "record_prediction", "resolve_prediction", "record_consensus",
    "research_queue", "complete_research_task", "create_experiment",
    "record_experiment_result", "rank_hypotheses", "create_discovery",
    "sync_obsidian", "discovery_brief", "list_actions",
    "workspace_list", "workspace_read", "source_bug_scan",
}

SYSTEM_PROMPT = """You are the autonomous Discovery Engine for a persistent research lab.
Your purpose is to find important, non-obvious truths by generating falsifiable
hypotheses and trying hard to disprove them. You are not rewarded for being
contrarian; you are rewarded for calibration, source quality, information gain,
and discovering models that survive adversarial testing.

Research rules:
1. Start by reading discovery_brief and building task-specific context (SQLite memory,
   Obsidian, and research repo) before creating duplicate hypotheses.
2. Distinguish observation, inference, hypothesis, and prediction.
3. Establish the consensus view before claiming a view is non-consensus.
4. Every serious hypothesis needs a falsification criterion, strongest known
   counterargument, and a next decisive test.
5. Actively search for evidence AGAINST a hypothesis. Do not only confirm it.
6. Prefer primary sources, official data, papers, filings, repositories, and
   direct measurements. Search snippets are leads, not evidence; read sources.
7. Record both supporting and contradicting evidence with conservative source
   reliability/independence scores. Do not inflate confidence manually to
   compensate for weak evidence.
8. Turn strong hypotheses into concrete predictions when possible so later
   outcomes can test calibration.
9. Queue expensive or unresolved work instead of pretending it is complete.
10. Promote a discovery only when evidence is meaningfully independent and the
    remaining counterargument is explicitly recorded.
11. Never fabricate a source, experiment result, observation, or consensus.
12. One cycle should make measurable epistemic progress: resolve an uncertainty,
    strengthen/weaken a hypothesis, generate a better test, or retire a bad idea.
13. If the focus refers to "this system" or the Universal Harness, require local
    repository/context evidence before making claims about it. Public results that
    merely share a generic product name are unrelated. If local context is absent,
    report the configuration gap and do not create a hypothesis about a namesake.
14. An empty keyword search does not prove a repository or file is absent. Treat
    successful workspace_list, source_bug_scan, workspace_read, and build_context
    results as authoritative local evidence and cite their returned paths.
15. Do not revisit a prior hypothesis unless this cycle can add a new source,
    observation, counterexample, experiment result, or materially narrower test.
16. In scout mode, your output is a candidate report only. You cannot create or
    update canonical hypotheses, evidence, predictions, tasks, or discoveries.
    State "insufficient evidence" when direct sources do not support a claim.
17. search_context is a keyword search, not a regular-expression engine. Use
    short literal terms. Paths returned by workspace_list belong to
    research_repo; never request them from obsidian unless an Obsidian result
    returned that exact path.
18. Treat a successful root source_bug_scan as covering its child source files.
    Do not repeat it on a child without a new, specific signal from another
    source. Zero scanner findings are not proof of correctness; they mean the
    scout must either cite another direct observation or report insufficient
    evidence.

At the end, return a concise cycle report containing: what changed, strongest
hypothesis investigated, strongest counterevidence found, confidence movement,
new predictions/tasks, and the most valuable next question.
"""


def core_headers():
    return {"X-API-Key": CORE_API_KEY, "Content-Type": "application/json"}


def fetch_tools():
    tools = [t for t in platform_client.tools() if t["name"] in AUTONOMOUS_TOOLS]
    # The small local model is a source-gathering scout by default. Canonical
    # research writes require an explicitly enabled, separately verified route.
    if not ALLOW_RESEARCH_WRITES:
        tools = [t for t in tools if t.get("effect", "read") == "read"]
    return tools


def openai_tools(tools):
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"]
    }} for t in tools]


def call_tool(tool, args, confirmed=False):
    if confirmed and tool.get("effect") == "admin":
        return platform_client.request(tool["method"], tool["path"], data=args, owner=True)
    return platform_client.invoke(tool["name"], args, confirmed=confirmed)


def llm_chat(messages, tools, prefer_fallback=False):
    return platform_client.chat(messages, tools, task_type="research", prefer_fallback=prefer_fallback)


def resilient_llm_chat(messages, tools, prefer_fallback=False):
    """Repair transient reasoning-only Ollama turns without losing the cycle."""
    for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
        try:
            if prefer_fallback:
                return llm_chat(messages, tools, prefer_fallback=True)
            return llm_chat(messages, tools)
        except RuntimeError as exc:
            if "empty provider response" not in str(exc) or attempt >= EMPTY_RESPONSE_RETRIES:
                raise
            messages.append({
                "role": "user",
                "content": (
                    "Your last model turn contained no usable content or tool call. Continue now without narrating intent: "
                    "either call one available tool needed for the evidence plan, or return the concise final cycle report."
                ),
            })


def bounded_tool_result(result, limit=None):
    """Keep one retrieved page from consuming a small model's whole context."""
    limit = MAX_TOOL_RESULT_CHARS if limit is None else max(500, int(limit))
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    compact = {
        "ok": result.get("ok") if isinstance(result, dict) else None,
        "error": result.get("error") if isinstance(result, dict) else None,
        "result": {
            "truncated_for_model_context": True,
            "original_characters": len(encoded),
            "preview": encoded[:max(100, limit - 500)],
            "instruction": "Use the preview as a lead; request a narrower source or query if more evidence is required.",
        },
    }
    if isinstance(result, dict) and result.get("security"):
        compact["security"] = result["security"]
    return json.dumps(compact, ensure_ascii=False)[:limit]


def incomplete_cycle_report(content):
    """Reject short transition prose that a small model mistakes for a final report."""
    text = str(content or "").strip()
    if not text:
        return True
    return len(text) < 1200 and (bool(_INCOMPLETE_INTENT.search(text)) or text.endswith(":") or text.endswith("..."))


def safe_scout_report(reason, bootstrap_results):
    """Return a useful fail-closed report instead of exposing weak model prose."""
    scan = (bootstrap_results.get("source_bug_scan") or {}).get("result") or {}
    inventory = (bootstrap_results.get("workspace_list") or {}).get("result") or []
    details = []
    if isinstance(scan, dict) and scan.get("files_scanned") is not None:
        summary = scan.get("summary") or {}
        details.append(
            f"the bounded static scan covered {scan.get('files_scanned')} files / "
            f"{scan.get('bytes_scanned', 0)} bytes and reported "
            f"{summary.get('errors', 0)} errors and {summary.get('warnings', 0)} warnings"
        )
    if isinstance(inventory, list):
        file_count = sum(1 for item in inventory if isinstance(item, dict) and not item.get("directory"))
        details.append(f"the workspace inventory exposed {file_count} files")
    observed = "; ".join(details) if details else "the available bounded checks did not yield a verified finding"
    return (
        "[discovery scout report]\n"
        "Status: insufficient evidence.\n"
        f"Observed: {observed}.\n"
        f"Stopped safely: {reason}.\n"
        "Confidence movement: none. New predictions/tasks: none. "
        "No canonical research state was changed.\n"
        "Next question: which specific failing test, traceback, or reproducible behavior should be inspected?"
    )


def run_cycle(focus=""):
    if RESOURCE_PAUSE_LEVEL != "off":
        state = platform_client.request("GET", "/api/system/health")
        severity = ((state.get("result") or {}).get("severity", "ok") if state.get("ok") else "ok")
        levels = {"ok": 0, "warning": 1, "critical": 2}
        if levels.get(severity, 0) >= levels.get(RESOURCE_PAUSE_LEVEL, 1):
            return f"Discovery cycle deferred because host resource health is {severity}."
    raw = fetch_tools()
    by_name = {t["name"]: t for t in raw}
    now = datetime.now(timezone.utc).isoformat()
    user_prompt = (
        f"Run one autonomous discovery/research cycle at {now}. "
        + (f"Focus on this domain/question: {focus}. " if focus else "Choose the highest-value unresolved question yourself. ")
        + "Use tools to inspect prior state and gather evidence; do not merely propose what could be researched."
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
    reflection_requested = False
    recovery_attempted = False
    prefer_fallback_next = False
    last_content = ""
    repeated_content = 0
    model_tool_calls = 0
    tool_signatures = {}
    failed_tool_calls = 0
    bootstrap_results = {}

    # Establish real prior state before relying on a small model to initiate
    # tools. These calls still use the normal scoped discovery credential and
    # are logged by the core tool gateway.
    bootstrap = []
    bootstrap_specs = [
        ("discovery_brief", {}),
        ("build_context", {"objective": user_prompt, "max_chars": 16000}),
    ]
    if re.search(r"\b(?:system|harness|code|source|test|defect|failure|repository|repo|software|bug)\b", focus, re.I):
        bootstrap_specs.extend([
            ("workspace_list", {"path": ""}),
            ("source_bug_scan", {"path": "", "limit": 100}),
        ])
    for index, (name, args) in enumerate(bootstrap_specs):
        tool = by_name.get(name)
        if not tool:
            continue
        call = {"id": f"host_discovery_{index}_{name}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, sort_keys=True)}}
        bootstrap.append((call, tool, args))
    if bootstrap:
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [item[0] for item in bootstrap]})
        for call, tool, args in bootstrap:
            try:
                result = call_tool(tool, args)
            except Exception as exc:
                result = {"ok": False, "result": None,
                          "error": {"message": f"{type(exc).__name__}: {exc}"}}
            bootstrap_results[name] = result
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": bounded_tool_result(result)})
        messages.append({"role": "user", "content":
            "Review the host bootstrap evidence above before choosing another tool. "
            "workspace_list paths are research_repo paths. Do not repeat the root source_bug_scan "
            "unless another source identifies a specific unscanned defect. If the evidence does not "
            "directly establish a reproducible defect, finish with insufficient evidence."})

    for _ in range(MAX_TOOL_ITERS):
        advertised = [t for t in raw if reflection_requested or t["name"] != "create_discovery"]
        tools = openai_tools(advertised)
        msg = resilient_llm_chat(messages, tools, prefer_fallback=prefer_fallback_next)
        prefer_fallback_next = False
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            normalized = " ".join(str(msg.get("content") or "").lower().split())[:20000]
            repeated_content = repeated_content + 1 if normalized and normalized == last_content else 1
            last_content = normalized
            denied = bool(_CAPABILITY_DENIAL.search(msg.get("content") or ""))
            incomplete = incomplete_cycle_report(msg.get("content"))
            if not reflection_requested:
                reflection_requested = True
                messages.append({"role": "user", "content":
                    "Mandatory evidence reflection before concluding: list every material claim you intend to make and the exact evidence, source, experiment, prediction, or record ID supporting it. Flag or remove every claim without direct support. After this pass you may finalize or create a discovery."})
                continue
            if denied or incomplete or repeated_content >= 2 or model_tool_calls == 0:
                if not recovery_attempted:
                    recovery_attempted = True
                    prefer_fallback_next = True
                    messages.append({"role": "user", "content":
                        "HOST RECOVERY: tools are available and the host executes them. Do not describe a manual plan or claim that access is unavailable. Call exactly one supplied tool now to make measurable evidence progress; use the already returned discovery brief and local context as your starting evidence."})
                    continue
                if not ALLOW_RESEARCH_WRITES:
                    return safe_scout_report("the local model did not complete a reliable evidence report", bootstrap_results)
                return ("[discovery cycle blocked: model_stuck_repeating] The model produced no executable research "
                        "action after one corrective fallback. The next scheduled cycle may retry; inspect model events for details.")
            return msg.get("content") or "(cycle completed without textual report)"

        model_tool_calls += len(calls)
        repeated_content = 0
        last_content = ""
        requested = []
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool = by_name.get(name)
            validation_error = None
            signature = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            tool_signatures[signature] = tool_signatures.get(signature, 0) + 1
            if tool_signatures[signature] > 1:
                validation_error = f"repeated identical tool request blocked: {name}"
            if tool:
                try:
                    validate(tool["input_schema"], args)
                except Exception as exc:
                    validation_error = f"invalid arguments for {name}: {exc.message if hasattr(exc, 'message') else exc}"
            requested.append((call, name, args, tool, validation_error))
        all_reads = bool(requested) and all(item[3] and item[3].get("effect", "read") == "read" for item in requested)

        def execute(item):
            call, name, args, tool, validation_error = item
            if not tool:
                return call, {"ok": False, "result": None, "error": {"message": f"tool not allowed: {name}"}}
            if validation_error:
                return call, {"ok": False, "result": None, "error": {"message": validation_error, "code": "invalid_tool_arguments"}}
            try:
                return call, call_tool(tool, args)
            except Exception as exc:
                return call, {"ok": False, "result": None, "error": {"message": f"{type(exc).__name__}: {exc}"}}

        if all_reads:
            batch = requested[:MAX_PARALLEL_READS]
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="discovery-read") as pool:
                results = list(pool.map(execute, batch))
            for item in requested[MAX_PARALLEL_READS:]:
                results.append((item[0], {"ok": False, "result": None,
                                "error": {"message": "skipped: parallel read limit reached; request again after reviewing results"}}))
        else:
            results = [execute(requested[0])]
            for item in requested[1:]:
                results.append((item[0], {"ok": False, "result": None,
                                "error": {"message": "skipped: side-effecting calls run one per reasoning turn"}}))
        for call, result in results:
            if call["function"]["name"] == "create_discovery" and not reflection_requested:
                result = {"ok": False, "result": None,
                          "error": {"message": "evidence reflection is required before create_discovery"}}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": bounded_tool_result(result)})
            if not result.get("ok"):
                failed_tool_calls += 1
        if failed_tool_calls >= 3:
            if not ALLOW_RESEARCH_WRITES:
                return safe_scout_report("three tool requests failed validation or repeated", bootstrap_results)
            return ("[discovery cycle blocked: repeated_tool_failures] Three tool calls failed or repeated. "
                    "No canonical research state was changed; inspect the action ledger before retrying.")

    if not ALLOW_RESEARCH_WRITES:
        return safe_scout_report(f"the bounded {MAX_TOOL_ITERS}-turn tool budget was exhausted", bootstrap_results)
    return f"Cycle stopped at bounded tool limit ({MAX_TOOL_ITERS}); inspect action ledger and discovery brief."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run exactly one cycle")
    ap.add_argument("--interval", type=int, default=0, help="seconds between cycles")
    ap.add_argument("--focus", default=os.environ.get("DISCOVERY_FOCUS", ""), help="optional domain or research question")
    args = ap.parse_args()
    if not args.once and args.interval <= 0:
        args.once = True

    while True:
        try:
            print(run_cycle(args.focus), flush=True)
        except Exception as e:
            print(f"[discovery cycle failed] {type(e).__name__}: {e}", flush=True)
        if args.once:
            break
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    main()
