#!/usr/bin/env python3
"""Periodically compact redundant active knowledge through the core gateway."""
from __future__ import annotations

import argparse
import os
import time

from core_client import CoreClient


client = CoreClient("discovery")


def run_once():
    health = client.request("GET", "/api/system/health")
    severity = ((health.get("result") or {}).get("severity", "ok") if health.get("ok") else "ok")
    pause_at = os.environ.get("CONSOLIDATION_RESOURCE_PAUSE_LEVEL", "warning").lower()
    levels = {"ok": 0, "warning": 1, "critical": 2}
    if pause_at != "off" and levels.get(severity, 0) >= levels.get(pause_at, 1):
        return {"deferred": True, "reason": f"host resource health is {severity}"}
    return client.invoke("consolidate_memory", {
        "min_cluster_size": int(os.environ.get("CONSOLIDATION_MIN_CLUSTER", "8")),
        "max_cluster_size": int(os.environ.get("CONSOLIDATION_MAX_CLUSTER", "24")),
        "max_clusters": int(os.environ.get("CONSOLIDATION_MAX_CLUSTERS", "2")),
        "max_candidates": int(os.environ.get("CONSOLIDATION_MAX_CANDIDATES", "1000")),
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=21600)
    args = parser.parse_args()
    while True:
        try:
            print(run_once(), flush=True)
        except Exception as exc:
            print({"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:1000]}, flush=True)
        if args.once:
            break
        time.sleep(max(900, args.interval))


if __name__ == "__main__":
    main()
