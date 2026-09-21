"""Owner controls for the assistant platform. JSON files make actions reviewable."""
import argparse
import json
import os
import time
from pathlib import Path

from core_client import CoreClient


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("spend")
    sub.add_parser("tools")
    sub.add_parser("jobs")
    p = sub.add_parser("audit")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--kind", default="", help="Event kind prefix, such as model. or tool.")
    p.add_argument("--job-id", default="", help="Only events correlated to this job")
    p.add_argument("--after-id", type=int, default=0, help="Only newer events, returned oldest first")
    p = sub.add_parser("queue")
    p.add_argument("objective")
    p.add_argument("--template", choices=["coding", "research", "forecast", "impact", "office", "browser", "operations"])
    p.add_argument("--max-steps", type=int, default=24)
    p.add_argument("--max-seconds", type=int, default=3600)
    p.add_argument("--verification", help="JSON array of read-only completion checks")
    p.add_argument("--engine", choices=["universal", "dsh"], default="universal")
    p.add_argument("--allow-browser-escalation", action="store_true",
                   help="Owner disclosure: allow this job to use configured approved-browser routes")
    p = sub.add_parser("grant")
    p.add_argument("actor", choices=["assistant", "discovery", "telegram", "mcp"])
    p.add_argument("tool")
    p.add_argument("--constraints", help="JSON object of required exact argument values")
    p.add_argument("--hours", type=float, default=1)
    p.add_argument("--uses", type=int, default=100)
    for name in ("cancel", "resume", "revoke", "rollback", "disable-integration"):
        sub.add_parser(name).add_argument("id")
    for name in ("install", "mcp-discover", "eval-suite", "propose", "correct-memory", "reconcile", "schedule"):
        sub.add_parser(name).add_argument("file")
    for name in ("evaluate", "promote"):
        p = sub.add_parser(name)
        p.add_argument("version_id")
        p.add_argument("suite")
    p = sub.add_parser("key")
    p.add_argument("actor", choices=["assistant", "discovery", "telegram", "mcp"])
    p.add_argument("--output", required=True, help="Write a scoped key to a private file; never prints the key")
    args = parser.parse_args()
    if not os.environ.get("CORE_API_KEY"):
        parser.error("CORE_API_KEY must be configured for owner controls")
    client = CoreClient()
    method, path, data = "POST", "", None
    cmd = args.command
    if cmd == "key":
        from platform_contracts import actor_key
        target = Path(args.output)
        with target.open("x", encoding="utf-8") as f:
            f.write(actor_key(client.owner_key, args.actor))
        print(json.dumps({"written": str(target), "actor": args.actor}))
        return
    if cmd in {"status", "spend", "tools", "jobs"}:
        method = "GET"
        path = {"status": "/api/platform/status", "spend": "/api/owner/spending", "tools": "/api/tools", "jobs": "/api/jobs"}[cmd]
    elif cmd == "audit":
        from urllib.parse import urlencode
        method = "GET"
        path = "/api/owner/audit?" + urlencode({"limit": args.limit, "kind": args.kind,
                                                 "job_id": args.job_id, "after_id": args.after_id})
    elif cmd == "queue":
        path = "/api/jobs/queue"
        payload = {"max_steps": args.max_steps, "max_seconds": args.max_seconds, "engine": args.engine}
        if args.allow_browser_escalation:
            payload["browser_escalation"] = True
        if args.template:
            payload["template"] = args.template
        if args.verification:
            payload["verification"] = read_json(args.verification)
        data = {"objective": args.objective, "payload": payload}
    elif cmd == "grant":
        path = "/api/owner/grants"
        data = {"actor": args.actor, "tool": args.tool, "constraints": read_json(args.constraints) if args.constraints else {},
                "expires_at": time.time() + args.hours * 3600, "uses": args.uses}
    elif cmd in {"cancel", "resume"}:
        path, data = "/api/owner/jobs/control", {"id": args.id, "action": cmd}
    elif cmd == "revoke":
        path, data = "/api/owner/grants/revoke", {"id": args.id}
    elif cmd == "rollback":
        path, data = "/api/owner/improvements/rollback", {"version_id": args.id}
    elif cmd == "disable-integration":
        path, data = "/api/owner/integrations/disable", {"name": args.id}
    elif cmd in {"evaluate", "promote"}:
        path = "/api/owner/evaluations/run" if cmd == "evaluate" else "/api/owner/improvements/promote"
        data = {"version_id": args.version_id, "suite": args.suite}
    elif cmd == "propose":
        import uuid
        path, data = "/api/tool-gateway", {"name": "propose_improvement", "args": read_json(args.file), "request_id": uuid.uuid4().hex}
    else:
        path = {"install": "/api/owner/integrations", "mcp-discover": "/api/owner/mcp/discover", "eval-suite": "/api/owner/evaluations/suite",
                "correct-memory": "/api/owner/memory/correct", "reconcile": "/api/owner/actions/reconcile", "schedule": "/api/owner/schedules"}[cmd]
        data = read_json(args.file)
    result = client.request(method, path, data=data, owner=True, timeout=650)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if isinstance(result, dict) and result.get("ok") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
