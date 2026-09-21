#!/usr/bin/env python3
"""Surface durable permission requests as native Windows attention prompts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import webbrowser
from pathlib import Path

from core_client import CoreClient


def permission_request(job: dict) -> dict | None:
    result = job.get("result") or {}
    pending = result.get("pending_action")
    if isinstance(pending, dict) and pending.get("tool"):
        args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
        return {
            "kind": "tool",
            "job_id": job.get("id"),
            "title": "Universal Assistant needs permission",
            "message": f"Job: {job.get('objective', '')[:240]}\n\nTool: {pending['tool']}\nArguments: {json.dumps(args, ensure_ascii=False)[:900]}",
        }
    if job.get("status") == "awaiting_approval" and result.get("status") == "tested" and result.get("name"):
        return {
            "kind": "capability",
            "job_id": job.get("id"),
            "title": "Universal Assistant capability is ready",
            "message": f"Capability '{result['name']}' passed its generated tests and is waiting for your activation decision.",
        }
    return None


def request_key(request: dict) -> str:
    payload = json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def show_prompt(request: dict) -> bool:
    """Return True when the operator asks to open the Command Center."""
    if os.name != "nt":
        print(f"[permission notification] {request['title']}: {request['message']}", flush=True)
        return False
    import ctypes
    yes_no = 0x00000004
    icon_warning = 0x00000030
    topmost = 0x00040000
    message = request["message"] + "\n\nOpen the Command Center now?"
    return ctypes.windll.user32.MessageBoxW(None, message, request["title"], yes_no | icon_warning | topmost) == 6


class ApprovalNotifier:
    def __init__(self, client=None, state_file: str | Path | None = None):
        self.client = client or CoreClient("approval_notifier")
        configured = state_file or os.environ.get("APPROVAL_NOTIFIER_STATE_FILE")
        self.state_file = Path(configured or "approval-notifications.json").resolve()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.seen = set(data.get("seen", []))
        except (OSError, ValueError, TypeError):
            self.seen = set()

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps({"seen": list(self.seen)[-500:]}, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_file)

    def poll_once(self) -> int:
        response = self.client.request("GET", "/api/jobs", params={"limit": 100}, owner=True, timeout=15)
        if not response.get("ok"):
            raise RuntimeError((response.get("error") or {}).get("message") or "job list unavailable")
        pending = []
        for job in response.get("result") or []:
            if job.get("status") not in {"blocked", "awaiting_approval"}:
                continue
            request = permission_request(job)
            if request and request_key(request) not in self.seen:
                pending.append(request)
        if not pending:
            return 0
        # Avoid a startup storm: alert for the newest request and mark the current
        # backlog seen. Newly created requests still receive their own prompt.
        selected = pending[0]
        for request in pending:
            self.seen.add(request_key(request))
        self._save()
        if show_prompt(selected):
            webbrowser.open(self.client.base + "/")
        return len(pending)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    notifier = ApprovalNotifier()
    while True:
        try:
            notifier.poll_once()
        except Exception as exc:
            print(f"[approval notifier] {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            return
        time.sleep(max(2, args.interval))


if __name__ == "__main__":
    main()
