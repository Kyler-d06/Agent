#!/usr/bin/env python3
"""Write a deterministic operator report from the durable runtime ledger."""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from core_client import CoreClient


def _stamp(value):
    try:
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError, OSError):
        return str(value or "unknown")


def _one_line(value, limit=500):
    return " ".join(str(value or "").split())[:limit]


def _job_explanation(job):
    if job.get("status") in {"queued", "running"}:
        return "In progress; the previous stopped result is not treated as the current outcome."
    result = job.get("result") or {}
    if result.get("error"):
        return _one_line(result["error"])
    if result.get("answer"):
        return _one_line(result["answer"])
    verification = result.get("verification") or {}
    if verification.get("status"):
        return "verification: " + str(verification["status"])
    return "No result details were recorded."


def build_report(platform, jobs, discovery_tail=""):
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    maintenance = platform.get("maintenance") or {}
    readiness = maintenance.get("readiness") or {}
    services = maintenance.get("services") or []
    counts = Counter(str(job.get("status") or "unknown") for job in jobs)
    healthy = bool(services) and all(item.get("status") == "running" for item in services)
    lines = [
        "# Universal Assistant operations report", "",
        f"Generated: {generated}", "",
        f"Overall: {'RUNNING' if healthy else 'ATTENTION REQUIRED'}",
        f"Mode: {'overnight' if readiness.get('overnight_mode') else 'pilot/full'}",
        f"Jobs shown: {len(jobs)} — done {counts['done']}, blocked {counts['blocked']}, "
        f"failed {counts['failed']}, running {counts['running']}, queued {counts['queued']}", "",
        "## Services", "",
    ]
    for item in services:
        lines.append(f"- {item.get('name')}: {item.get('status')} (restarts: {item.get('restarts', 0)})")
    lines.extend(["", "## Readiness", "",
                  f"- Ollama: {'ready' if readiness.get('ollama_api_ready') else 'not ready'}",
                  f"- Docker sandbox: {'ready' if readiness.get('docker_daemon_ready') else 'not ready'}",
                  f"- DSH: {'ready' if readiness.get('dsh_configured') else 'not configured'}",
                  f"- Free web retrieval: {'ready' if readiness.get('free_web_retrieval_ready') else 'not ready'}",
                  f"- Web search backend: {readiness.get('web_search_backend', 'ddg')} (static HTTPS; no browser automation)", "",
                  "## Recent jobs", ""])
    for job in jobs[:20]:
        lines.extend([
            f"### {job.get('id')} — {str(job.get('status') or 'unknown').upper()}", "",
            f"Updated: {_stamp(job.get('updated_at'))}",
            f"Request: {_one_line(job.get('objective'), 800)}",
            f"Outcome: {_job_explanation(job)}", "",
        ])
    if discovery_tail.strip():
        lines.extend(["## Latest discovery output", "", "```text", discovery_tail.strip()[-6000:], "```", ""])
    lines.extend(["## Operator attention", ""])
    attention = []
    if not healthy:
        attention.append("One or more supervised services are not running.")
    if not readiness.get("ollama_api_ready"):
        attention.append("Ollama is unavailable, so local-model jobs cannot run.")
    if not readiness.get("docker_daemon_ready"):
        attention.append("Docker is unavailable, so isolated verification cannot run.")
    if not readiness.get("free_web_retrieval_ready"):
        attention.append("Free web retrieval is unavailable; research cannot search or read public static pages.")
    for job in jobs[:5]:
        if job.get("status") in {"blocked", "failed", "awaiting_approval"}:
            attention.append(f"{job.get('id')}: {_job_explanation(job)}")
    lines.extend([f"- {item}" for item in attention] or ["- No immediate operator action is required."])
    lines.extend(["", "This report is generated from the durable job/event ledger and service status; it does not expose hidden chain-of-thought.", ""])
    return "\n".join(lines)


def write_report(client=None):
    client = client or CoreClient("report")
    platform_envelope = client.request("GET", "/api/platform/status", owner=True)
    jobs_envelope = client.request("GET", "/api/jobs", params={"limit": 50}, owner=True)
    if not platform_envelope.get("ok") or not jobs_envelope.get("ok"):
        raise RuntimeError(f"report inputs unavailable: {platform_envelope.get('error') or jobs_envelope.get('error')}")
    status_path = Path(os.environ["MAINTENANCE_STATUS_FILE"]).resolve()
    discovery_path = status_path.parent / "logs" / "discovery.log"
    discovery_tail = ""
    if discovery_path.is_file():
        with discovery_path.open("rb") as handle:
            handle.seek(max(0, discovery_path.stat().st_size - 8000))
            discovery_tail = handle.read().decode("utf-8", errors="replace")
    report = build_report(platform_envelope["result"], jobs_envelope["result"], discovery_tail)
    target = Path(os.environ.get("OVERNIGHT_REPORT_FILE") or (Path(os.environ["CORE_DB"]).resolve().parent / "OVERNIGHT_REPORT.md"))
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(report, encoding="utf-8")
    os.replace(temporary, target)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            print(json.dumps({"report": str(write_report()), "updated_at": time.time()}), flush=True)
        except Exception as exc:
            print(f"[overnight report failed] {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return
        time.sleep(max(60, args.interval))


if __name__ == "__main__":
    main()
