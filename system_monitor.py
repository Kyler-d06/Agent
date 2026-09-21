#!/usr/bin/env python3
"""System/GPU telemetry for the agent platform.

- Uses nvidia-smi (ships with NVIDIA drivers) for GPU telemetry.
- Uses psutil when installed for CPU/RAM/disk/load/process telemetry.
- Can run once or as a daemon that posts threshold events to core_server.
- Read-only: it never changes clocks, power limits, fan curves, or kills processes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL = int(os.environ.get("SYSTEM_MONITOR_INTERVAL", "15"))
GPU_WARN_C = float(os.environ.get("GPU_WARN_C", "78"))
GPU_CRIT_C = float(os.environ.get("GPU_CRIT_C", "84"))
VRAM_WARN_PCT = float(os.environ.get("VRAM_WARN_PCT", "92"))
RAM_WARN_PCT = float(os.environ.get("RAM_WARN_PCT", "90"))
DISK_WARN_PCT = float(os.environ.get("DISK_WARN_PCT", "92"))
CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:5077").rstrip("/")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "[N/A]", "N/A") else None
    except (TypeError, ValueError):
        return None


def gpu_metrics() -> list[dict[str, Any]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    fields = [
        "index", "name", "temperature.gpu", "utilization.gpu",
        "utilization.memory", "memory.used", "memory.total",
        "power.draw", "power.limit", "fan.speed", "pstate",
    ]
    cmd = [exe, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=True)
    except Exception:
        return []
    out = []
    for line in proc.stdout.splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) != len(fields):
            continue
        d = dict(zip(fields, vals))
        used, total = _f(d["memory.used"]), _f(d["memory.total"])
        power, plimit = _f(d["power.draw"]), _f(d["power.limit"])
        out.append({
            "index": int(_f(d["index"]) or 0),
            "name": d["name"],
            "temperature_c": _f(d["temperature.gpu"]),
            "gpu_util_pct": _f(d["utilization.gpu"]),
            "memory_util_pct": _f(d["utilization.memory"]),
            "vram_used_mb": used,
            "vram_total_mb": total,
            "vram_used_pct": round(100 * used / total, 1) if used is not None and total else None,
            "power_w": power,
            "power_limit_w": plimit,
            "power_pct": round(100 * power / plimit, 1) if power is not None and plimit else None,
            "fan_pct": _f(d["fan.speed"]),
            "pstate": d["pstate"],
        })
    return out


def host_metrics() -> dict[str, Any]:
    try:
        import psutil
    except ImportError:
        return {"psutil_available": False}
    vm = psutil.virtual_memory()
    # Measure the volume that holds assistant data. A locked-down service
    # account may not be allowed to query the user-profile directory itself.
    disk = None
    try:
        disk = psutil.disk_usage(str(Path(os.environ.get("CORE_ROOT") or os.getcwd()).resolve()))
    except (OSError, RuntimeError):
        pass
    load = None
    try:
        load = list(os.getloadavg())
    except Exception:
        pass
    return {
        "psutil_available": True,
        "cpu_util_pct": psutil.cpu_percent(interval=0.15),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "ram_used_gb": round((vm.total - vm.available) / (1024 ** 3), 2),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "ram_used_pct": vm.percent,
        "disk_used_pct": disk.percent if disk else None,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2) if disk else None,
        "load_avg": load,
    }


def snapshot() -> dict[str, Any]:
    return {"timestamp": now_iso(), "host": host_metrics(), "gpus": gpu_metrics()}


def health(snapshot_data: dict[str, Any] | None = None) -> dict[str, Any]:
    s = snapshot_data or snapshot()
    alerts = []
    severity = "ok"
    for g in s.get("gpus", []):
        temp = g.get("temperature_c")
        if temp is not None and temp >= GPU_CRIT_C:
            alerts.append({"severity": "critical", "kind": "gpu_temperature", "gpu": g.get("index"), "value": temp, "threshold": GPU_CRIT_C})
            severity = "critical"
        elif temp is not None and temp >= GPU_WARN_C:
            alerts.append({"severity": "warning", "kind": "gpu_temperature", "gpu": g.get("index"), "value": temp, "threshold": GPU_WARN_C})
            if severity == "ok": severity = "warning"
        vram = g.get("vram_used_pct")
        if vram is not None and vram >= VRAM_WARN_PCT:
            alerts.append({"severity": "warning", "kind": "gpu_vram", "gpu": g.get("index"), "value": vram, "threshold": VRAM_WARN_PCT})
            if severity == "ok": severity = "warning"
    h = s.get("host", {})
    if h.get("ram_used_pct") is not None and h["ram_used_pct"] >= RAM_WARN_PCT:
        alerts.append({"severity": "warning", "kind": "ram", "value": h["ram_used_pct"], "threshold": RAM_WARN_PCT})
        if severity == "ok": severity = "warning"
    if h.get("disk_used_pct") is not None and h["disk_used_pct"] >= DISK_WARN_PCT:
        alerts.append({"severity": "warning", "kind": "disk", "value": h["disk_used_pct"], "threshold": DISK_WARN_PCT})
        if severity == "ok": severity = "warning"
    return {"severity": severity, "alerts": alerts, "snapshot": s}


def emit_event(event_type: str, payload: dict[str, Any]) -> bool:
    if not CORE_URL or not CORE_API_KEY:
        return False
    body = json.dumps({"event_type": event_type, "tag": "system", "source": {"type": "system_monitor"}, "payload": payload}).encode()
    req = urllib.request.Request(
        CORE_URL + "/api/events/emit", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": CORE_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status < 400
    except Exception:
        return False


def monitor(interval: int = DEFAULT_INTERVAL) -> None:
    previous_signature = None
    while True:
        report = health()
        signature = tuple((a["severity"], a["kind"], a.get("gpu")) for a in report["alerts"])
        if report["alerts"] and signature != previous_signature:
            emit_event("system.health_alert", report)
        previous_signature = signature
        print(json.dumps(report, indent=2), flush=True)
        time.sleep(max(2, interval))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="Continuously monitor and emit threshold events")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()
    if args.watch:
        monitor(args.interval)
    else:
        print(json.dumps(health(), indent=2))


if __name__ == "__main__":
    main()
