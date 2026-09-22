import json
import re
import shutil
import subprocess

import pytest

import approval_notifier


def test_permission_request_extracts_exact_tool_action():
    request = approval_notifier.permission_request({
        "id": "JOB-one", "status": "blocked", "objective": "edit the file",
        "result": {"pending_action": {"tool": "workspace_write", "args": {"path": "a.txt"}}},
    })
    assert request["kind"] == "tool"
    assert "workspace_write" in request["message"] and "a.txt" in request["message"]


def test_notifier_deduplicates_and_opens_command_center(tmp_path, monkeypatch):
    class Client:
        base = "http://127.0.0.1:5077"

        def request(self, *_args, **_kwargs):
            return {"ok": True, "result": [{
                "id": "JOB-one", "status": "blocked", "objective": "edit",
                "result": {"pending_action": {"tool": "workspace_write", "args": {"path": "a.txt"}}},
            }]}

    opened = []
    monkeypatch.setattr(approval_notifier, "show_prompt", lambda _request: True)
    monkeypatch.setattr(approval_notifier.webbrowser, "open", opened.append)
    notifier = approval_notifier.ApprovalNotifier(Client(), tmp_path / "seen.json")

    assert notifier.poll_once() == 1
    assert notifier.poll_once() == 0
    assert opened == ["http://127.0.0.1:5077/"]


def test_dashboard_approve_once_grants_exact_action_and_resumes(core, owner):
    with core.app.app_context():
        job_id = core.agent_platform._queue_job("agent", "write it", {"approval_mode": "suggest"})
        pending = {"pending_action": {"tool": "workspace_write", "args": {"path": "answer.txt", "content": "ok"}}}
        db = core.get_db()
        db.execute("UPDATE agent_jobs SET status='blocked',result_json=? WHERE id=?", (json.dumps(pending), job_id))
        db.commit()

    with core.app.test_client() as client:
        response = client.post("/api/owner/jobs/approve-once", headers=owner, json={"id": job_id})
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True and body["result"]["job"]["status"] == "queued"

    with core.app.app_context():
        row = core.get_db().execute("SELECT tool,constraints_json,remaining FROM permission_grants").fetchone()
        assert row["tool"] == "workspace_write"
        assert json.loads(row["constraints_json"]) == {"path": "answer.txt", "content": "ok"}
        assert row["remaining"] == 1


def test_dashboard_opens_only_configured_vault(core, owner, monkeypatch):
    opened = []
    monkeypatch.setattr(core.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(core.os, "name", "nt")

    with core.app.test_client() as client:
        response = client.post("/api/owner/open-configured-path", headers=owner, json={"name": "obsidian"})
        assert response.status_code == 200
        denied = client.post("/api/owner/open-configured-path", headers=owner, json={"name": "arbitrary"})
        assert denied.status_code == 400

    assert opened == [str(core.Path(core.OBSIDIAN_VAULT).resolve())]


def test_rendered_command_center_javascript_parses(core):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for JavaScript syntax validation")
    script = re.search(r"<script>([\s\S]*)</script>", core.MAIN_PAGE).group(1)
    checked = subprocess.run([node, "--check", "-"], input=script, text=True, encoding="utf-8",
                             capture_output=True, timeout=10)
    assert checked.returncode == 0, checked.stderr
    assert "Open Obsidian vault" in core.MAIN_PAGE
    assert "Approve once & resume" in core.MAIN_PAGE


def test_command_center_saves_validated_loop_frequencies(core, owner):
    values = {
        "assistant_poll_seconds": 8,
        "approval_notification_seconds": 10,
        "system_monitor_seconds": 30,
        "memory_consolidation_seconds": 3600,
        "discovery_seconds": 900,
        "overnight_report_seconds": 180,
    }
    with core.app.test_client() as client:
        saved = client.post("/api/owner/settings/loops", headers=owner, json=values)
        assert saved.status_code == 200
        assert saved.get_json()["result"]["restart_required"] is True
        state = client.get("/api/state", headers=owner).get_json()
        assert state["configuration"]["loop_frequencies"] == values
        invalid = client.post("/api/owner/settings/loops", headers=owner,
                              json={**values, "discovery_seconds": 30})
        assert invalid.status_code == 400
