#!/usr/bin/env python3
"""
telegram_agent.py — the reasoning layer. Polls Telegram, sends each message
to an LLM (via 9router or any OpenAI-compatible endpoint) with the full
tool list pulled live from core_server's /api/tools, and dispatches
whatever the model decides to call. Add a tool to core_server, it shows up
here automatically — nothing in this file needs to change.

Setup:
    pip install requests
    export TELEGRAM_BOT_TOKEN=...
    export ALLOWED_CHAT_IDS=123456789          # your chat id ONLY — see below
    export CORE_URL=http://127.0.0.1:5077
    export TELEGRAM_API_KEY=...                 # scoped key derived by the launcher
    python3 telegram_agent.py

ALLOWED_CHAT_IDS is not optional in practice: this bot can execute code and
touch your memory store. Leaving it unset means ANYONE who finds your bot
can drive it. Get your chat id by messaging the bot once and checking
https://api.telegram.org/bot<token>/getUpdates before you set this.

Safety default that stays on regardless of what else changes: tools in
CONFIRM_REQUIRED pause and wait for you to reply "yes" before running.
run_file is in that set because it executes code, and this agent reads
back content scraped from the open web (via web_capture.py -> /api/ingest)
through search_memory — auto-running code based on something read off a
random webpage is the actual failure mode this guards against.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import shlex

import requests
from core_client import CoreClient

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {c for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c}
CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:5077")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")
OWNER_COMMANDS_ENABLED = os.environ.get("TELEGRAM_OWNER_COMMANDS") == "1" and bool(CORE_API_KEY)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "placeholder")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
CONFIRM_REQUIRED = {"run_file", "commit_research", "approve_capability", "deprecate_capability", "set_workflow_enabled", "set_capability_autonomy"}
MAX_TOOL_ITERS = 8
MAX_PARALLEL_READS = max(1, min(int(os.environ.get("TELEGRAM_MAX_PARALLEL_READS", "4")), 8))

EXECUTIVE_SYSTEM_PROMPT = """You are the executive agent for a persistent personal agentic computing platform.
You are not merely a chatbot: translate the user's requests into goals, context retrieval, tool use, reusable workflows, or durable background jobs when appropriate.

Operating principles:
- For nontrivial work, use build_context/search_context/search_memory so you act from the user's actual Obsidian, Git/research, project, and SQLite state rather than guessing.
- Prefer an existing tool or generated capability before creating new code. Use list_capabilities when capability reuse is plausible.
- If a reusable capability is genuinely missing, use request_capability. The capability factory will generate and sandbox-test it; activation remains a human-confirmed step.
- Use persistent goals for objectives that matter beyond the current message. Keep next_action current when useful.
- Use queue_agent_job for substantial work that should survive this Telegram process or be handled by assistant_worker. Do not queue trivial tasks that can be finished interactively.
- Research/discovery is a background department, not the whole assistant. Use its hypothesis/evidence tools when they materially help.
- Reusable event-driven behavior belongs in workflows. Workflows are created disabled and enabling them requires human confirmation.
- Generated capabilities and background workers must not silently expand their own authority. Respect confirmation gates and tool metadata.
- Distinguish observations from actions, and describe important side effects before requesting confirmation.
- Do not invent access, successful executions, files, sources, or results. Inspect them with tools.
- The current platform intentionally has no trading execution subsystem.
- One real tool action is executed per reasoning turn; observe its result before choosing the next action.

When the user asks for something new, think in this order: existing context -> existing capability -> ordinary tool composition -> reusable workflow -> request a new capability.
"""

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
platform_client = CoreClient("telegram")

sessions = {}   # chat_id -> running message list (simple per-chat memory)
pending = {}    # chat_id -> paused tool call awaiting "yes"/"no"
browse_state = {}  # chat_id -> current deterministic owner script browser


def core_headers():
    return platform_client.headers(owner=OWNER_COMMANDS_ENABLED)


def fetch_tools():
    return platform_client.tools()


def to_openai_tools(tools_raw):
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"]
    }} for t in tools_raw]


def call_tool(tool, args, confirmed=False):
    if confirmed and tool.get("effect") == "admin":
        return platform_client.request(tool["method"], tool["path"], data=args, owner=True)
    return platform_client.invoke(tool["name"], args, confirmed=confirmed)


def llm_chat(messages, tools):
    return platform_client.chat(messages, tools, task_type="general")


def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage",
                  json={"chat_id": chat_id, "text": (text or "(empty response)")[:4000]}, timeout=15)


def core_request(path, method="GET", params=None, body=None, timeout=20):
    url = CORE_URL + path
    if method == "GET":
        r = requests.get(url, headers=core_headers(), params=params or {}, timeout=timeout)
    else:
        r = requests.post(url, headers=core_headers(), json=body or {}, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:500]}"}


def _unwrap(d):
    if isinstance(d, dict) and "result" in d:
        return d.get("result")
    return d


def owner_help():
    if not OWNER_COMMANDS_ENABLED:
        return """Universal Assistant pilot

Send normal text to use the local model and permission-gated platform tools.
Administrative slash commands and approval grants are deliberately disabled in
pilot mode; use the local dashboard for those actions."""
    return """Agent + Script Console

Owner commands (these bypass the LLM and only work from ALLOWED_CHAT_IDS):
/nodes — show live execution nodes and hardware tiers
/ls <node> [root] [folder] — browse that node's script root
/pick <n> — enter a numbered folder or inspect a numbered script
/runpick <n> [args...] — run a numbered script from the current folder
/run <node> <root> <path> [args...] — run a script directly (quote paths with spaces)
/jobs <node> — show recent/running jobs
/ideas <node> — script ideas suited to that node's hardware
/grant <node> <root> <path> <private|visible|confirm|autonomous> — agent visibility/authority

Normal text still goes to the executive AI.

Examples:
/ls laptop code
/run laptop code Utilities/status.py
/grant laptop code Utilities/status.py autonomous
/ideas pi
"""


def format_browse(chat_id, node, root, path, payload):
    data = _unwrap(payload) or {}
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    entries = data.get("entries", []) if isinstance(data, dict) else []
    browse_state[chat_id] = {"node": node, "root": root or data.get("root", ""), "path": path, "entries": entries}
    lines = [f"{node}:{root or data.get('root','')} /{path}".rstrip("/"), ""]
    for i, e in enumerate(entries[:40], 1):
        icon = "📁" if e.get("type") == "dir" else ("▶️" if e.get("runnable") else "📄")
        suffix = ""
        if e.get("runnable"):
            ok = "✓" if e.get("compatible") else "⚠"
            suffix = f"  [{e.get('weight','?')} {ok}; agent:{e.get('permission','private')}]"
        lines.append(f"{i}. {icon} {e.get('name')}{suffix}")
    if entries:
        lines += ["", "Use /pick N to open/inspect, or /runpick N [args] to execute a script."]
    else:
        lines.append("(empty or unavailable)")
    return "\n".join(lines)[:4000]


def handle_owner_command(chat_id, text):
    try:
        parts = shlex.split(text, posix=False if os.name == "nt" else True)
    except ValueError as e:
        send_message(chat_id, f"Command parse error: {e}")
        return True
    if not parts:
        return False
    cmd = parts[0].lower()
    if cmd in ("/help", "/start"):
        send_message(chat_id, owner_help()); return True
    if cmd.startswith("/") and not OWNER_COMMANDS_ENABLED:
        send_message(chat_id, "Pilot mode keeps Telegram owner commands disabled. Use the local dashboard for approvals and administration.")
        return True
    if cmd == "/nodes":
        d = _unwrap(core_request("/api/mesh/nodes")) or []
        lines = ["Execution nodes:"]
        for n in d:
            m = n.get("manifest", {}) or {}; prof = m.get("node", {}) if m.get("ok") else {}
            if prof:
                gpu = prof.get("gpu", {}) or {}
                g = f", GPU {gpu.get('name')}" if gpu.get("available") else ""
                lines.append(f"• {n.get('name')}: {prof.get('tier')} | {prof.get('ram_available_gb')} GB free RAM | {prof.get('cpu_count')} CPU{g}")
            else:
                lines.append(f"• {n.get('name')}: offline/unavailable — {m.get('error','unknown')}")
        send_message(chat_id, "\n".join(lines)); return True
    if cmd == "/ls":
        if len(parts) < 2:
            send_message(chat_id, "Usage: /ls <node> [root] [folder]"); return True
        node = parts[1]; root = parts[2] if len(parts) > 2 else ""; path = parts[3] if len(parts) > 3 else ""
        d = core_request("/api/owner/mesh/browse", params={"node":node,"root":root,"path":path})
        send_message(chat_id, format_browse(chat_id,node,root,path,d)); return True
    if cmd == "/pick":
        st = browse_state.get(chat_id)
        if not st or len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "Browse with /ls first, then /pick <number>."); return True
        i=int(parts[1])-1
        if i<0 or i>=len(st["entries"]): send_message(chat_id,"Invalid selection."); return True
        e=st["entries"][i]
        rel = os.path.join(st["path"], e["name"]).replace("\\","/") if st["path"] else e["name"]
        if e.get("type")=="dir":
            d=core_request("/api/owner/mesh/browse",params={"node":st["node"],"root":st["root"],"path":rel})
            send_message(chat_id,format_browse(chat_id,st["node"],st["root"],rel,d)); return True
        send_message(chat_id, f"{e['name']}\nWeight: {e.get('weight','n/a')}\nCompatible here: {e.get('compatible','n/a')}\nAgent permission: {e.get('permission','private')}\nDescription: {e.get('description') or '(none)'}\n\nRun: /runpick {i+1}\nGrant example: /grant {st['node']} {st['root']} \"{rel}\" confirm")
        return True
    if cmd == "/runpick":
        st=browse_state.get(chat_id)
        if not st or len(parts)<2 or not parts[1].isdigit(): send_message(chat_id,"Use /ls, then /runpick <number> [args]."); return True
        i=int(parts[1])-1
        if i<0 or i>=len(st["entries"]): send_message(chat_id,"Invalid selection."); return True
        e=st["entries"][i]
        if not e.get("runnable"): send_message(chat_id,"That entry is not runnable."); return True
        rel=os.path.join(st["path"],e["name"]).replace("\\","/") if st["path"] else e["name"]
        d=core_request("/api/owner/mesh/run",method="POST",body={"node":st["node"],"root":st["root"],"path":rel,"args":parts[2:]})
        send_message(chat_id,json.dumps(_unwrap(d),indent=2)[:4000]); return True
    if cmd == "/run":
        if len(parts)<4: send_message(chat_id,"Usage: /run <node> <root> <path> [args...]"); return True
        d=core_request("/api/owner/mesh/run",method="POST",body={"node":parts[1],"root":parts[2],"path":parts[3],"args":parts[4:]})
        send_message(chat_id,json.dumps(_unwrap(d),indent=2)[:4000]); return True
    if cmd == "/jobs":
        if len(parts)<2: send_message(chat_id,"Usage: /jobs <node>"); return True
        d=core_request("/api/owner/mesh/jobs",params={"node":parts[1]})
        send_message(chat_id,json.dumps(_unwrap(d),indent=2)[:4000]); return True
    if cmd == "/ideas":
        if len(parts)<2: send_message(chat_id,"Usage: /ideas <node>"); return True
        d=_unwrap(core_request("/api/mesh/ideas",params={"node":parts[1]})) or {}
        lines=[f"Ideas for {parts[1]} ({d.get('tier','unknown')}):"]+[f"• {x['name']} — {x['idea']}" for x in d.get('ideas',[])]
        send_message(chat_id,"\n".join(lines)); return True
    if cmd == "/grant":
        if len(parts)<5: send_message(chat_id,"Usage: /grant <node> <root> <path> <private|visible|confirm|autonomous>"); return True
        d=core_request("/api/owner/mesh/permission",method="POST",body={"node":parts[1],"root":parts[2],"path":parts[3],"permission":parts[4].lower()})
        send_message(chat_id,json.dumps(_unwrap(d),indent=2)[:4000]); return True
    return False


def run_agent_loop(chat_id, tools_raw, openai_tools):
    messages = sessions[chat_id]
    for _ in range(MAX_TOOL_ITERS):
        msg = llm_chat(messages, openai_tools)
        messages.append(msg)
        calls = msg.get("tool_calls")
        if not calls:
            send_message(chat_id, msg.get("content"))
            return

        requested = []
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool = next((t for t in tools_raw if t["name"] == name), None)
            requested.append((call, name, args, tool))

        all_reads = bool(requested) and all(tool and tool.get("effect", "read") == "read" and
                                            name not in CONFIRM_REQUIRED and not tool.get("requires_confirmation")
                                            for _, name, _, tool in requested)
        if all_reads:
            def read_one(item):
                call, _, args, tool = item
                try:
                    return call, call_tool(tool, args)
                except Exception as exc:
                    return call, {"ok": False, "result": None, "error": {"message": f"{type(exc).__name__}: {exc}"}}
            batch = requested[:MAX_PARALLEL_READS]
            with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="telegram-read") as pool:
                results = list(pool.map(read_one, batch))
            for item in requested[MAX_PARALLEL_READS:]:
                results.append((item[0], {"ok": False, "result": None,
                                "error": {"message": "skipped: parallel read limit reached; request again next turn"}}))
            for call, result in results:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
            continue

        # Side effects remain deliberately serialized so the model observes the
        # first result before proposing another write, execution, or external call.
        for i, (call, name, args, tool) in enumerate(requested):
            if i > 0:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(
                    {"ok": False, "result": None, "error": {"message": "skipped: side-effecting calls run one per turn"}})})
                continue

            if not tool:
                result = {"ok": False, "result": None, "error": {"message": f"unknown tool '{name}'"}}
            elif name in CONFIRM_REQUIRED or tool.get("requires_confirmation"):
                pending[chat_id] = {"call": call, "tool": tool, "args": args, "messages": messages}
                send_message(chat_id, f"Wants to run `{name}` with {json.dumps(args)}. Reply yes to confirm, no to cancel.")
                return  # pause the whole loop here until the human answers
            else:
                result = call_tool(tool, args)

            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})

    send_message(chat_id, "Stopped after too many tool steps in a row — worth checking in on what it was doing.")


def handle_message(chat_id, text, tools_raw, openai_tools):
    if chat_id in pending:
        p = pending.pop(chat_id)
        if text.strip().lower() in ("yes", "y", "confirm"):
            result = call_tool(p["tool"], p["args"], confirmed=True)
        else:
            result = {"cancelled_by_user": True}
        p["messages"].append({"role": "tool", "tool_call_id": p["call"]["id"], "content": json.dumps(result)})
        run_agent_loop(chat_id, tools_raw, openai_tools)
        return

    sessions.setdefault(chat_id, [{"role": "system", "content": EXECUTIVE_SYSTEM_PROMPT}])
    sessions[chat_id].append({"role": "user", "content": text})
    run_agent_loop(chat_id, tools_raw, openai_tools)


def main():
    if not ALLOWED_CHAT_IDS:
        raise SystemExit("Refusing to start: ALLOWED_CHAT_IDS is empty — set it, or anyone who finds this bot controls it.")

    tools_raw = fetch_tools()
    print(f"Loaded {len(tools_raw)} tools from {CORE_URL}/api/tools: {[t['name'] for t in tools_raw]}")
    print("Agent bot running, listening for Telegram messages...")

    offset = None
    while True:
        try:
            r = requests.get(f"{TELEGRAM_API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            updates = r.json().get("result", [])
        except requests.RequestException as e:
            print("poll error:", e)
            time.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "")
            if not text or not chat_id:
                continue
            if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
                continue
            try:
                # Refresh the registry for each incoming message so capabilities
                # approved since startup become usable without restarting the bot.
                if handle_owner_command(chat_id, text):
                    continue
                tools_raw = fetch_tools()
                openai_tools = to_openai_tools(tools_raw)
                handle_message(chat_id, text, tools_raw, openai_tools)
            except Exception as e:
                send_message(chat_id, f"Error: {e}")


if __name__ == "__main__":
    main()
