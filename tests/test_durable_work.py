import json
import time
import uuid

import pytest

from durable_agent import run, verify
from platform_contracts import canonical
from test_platform import queue


class InProcessClient:
    """HTTP-equivalent client with injected model replies and a real SQLite core."""
    def __init__(self, core, job, replies):
        self.core, self.u, self.job = core, core.universal_platform, job
        self.replies = iter(replies)
        self.model_calls = 0

    def checkpoint(self, state=None):
        return self.u.store.checkpoint(self.job["id"], self.job["lease_token"], state)["state"]

    def tools(self):
        return self.u.catalog()

    def request(self, method, path, **kwargs):
        with self.core.app.test_client() as c:
            return c.open(path, method=method, headers={"X-API-Key": "test-owner-key"}, json=kwargs.get("data"), query_string=kwargs.get("params")).get_json()

    def invoke(self, name, args, request_id=None):
        return self.u.invoke("assistant", name, args, request_id or uuid.uuid4().hex, self.job["id"], self.job["lease_token"])

    def chat(self, *args, **kwargs):
        self.model_calls += 1
        return next(self.replies)


def call(name, args, id="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": id, "type": "function", "function": {"name": name, "arguments": canonical(args)}}]}


def test_permission_pause_resumes_pending_call_without_regenerating(core):
    queue(core, "Write the requested file")
    job = core.universal_platform.store.claim("test-worker")
    client = InProcessClient(core, job, [call("workspace_write", {"path": "answer.txt", "content": "done"})])
    first = run(client, "Write the requested file")
    assert first["blocked"]
    assert client.model_calls == 1
    core.universal_platform.store.grant("assistant", "workspace_write", {"path": "answer.txt"}, time.time() + 60, uses=1)
    resumed = InProcessClient(core, job, [{"role": "assistant", "content": "Created answer.txt"}])
    result = run(resumed, "Write the requested file")
    assert result["ok"]
    assert resumed.model_calls == 1
    assert len(result["actions"]) == 1
    assert result["verification"]["status"] == "unverified"
    queue(core, "Report the current local date and time. Do not create or modify files.", {"template": "office"})
    clock_job = core.universal_platform.store.claim("clock-worker")
    clock_client = InProcessClient(core, clock_job, [{"role": "assistant", "content": "The verified host time is shown."}])
    clock_result = run(clock_client, clock_job["objective"])
    assert clock_result["ok"] and clock_result["actions"][0]["tool"] == "get_current_datetime"
    assert clock_result["actions"][0]["result"]["local_date"]
    assert not any(c["name"] == "artifact_hashes" for c in clock_result["verification"]["checks"])


def test_restart_after_effect_before_checkpoint_does_not_repeat_write(core):
    queue(core, "Write a file")
    u = core.universal_platform
    job = u.store.claim("first")
    u.store.grant("assistant", "workspace_write", {}, time.time() + 60, uses=1)
    client = InProcessClient(core, job, [call("workspace_write", {"path": "once.txt", "content": "only once"})])
    original = client.invoke
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("simulated process crash after durable effect")
    client.invoke = crash
    with pytest.raises(SystemExit):
        run(client, "Write a file")
    with u.store.connect() as db:
        db.execute("UPDATE agent_jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    recovered_job = u.store.claim("second")
    recovered = InProcessClient(core, recovered_job, [{"role": "assistant", "content": "File written"}])
    assert run(recovered, "Write a file")["ok"]
    with u.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tool_executions WHERE tool='workspace_write'").fetchone()[0] == 1


def test_research_task_produces_a_real_verified_report(core, monkeypatch):
    queue(core, "Produce a cited report", {"template": "research"})
    job = core.universal_platform.store.claim("research")
    args = {"path": "report.md", "title": "Research", "body": "A finding with a source.", "sources": [{"title": "Original source", "url": "https://example.test/paper"}], "job_id": job["id"]}
    monkeypatch.setitem(core.app.view_functions, "research_read", lambda: core.ok({"url": "https://example.test/paper", "text": "A finding in the original source."}))
    client = InProcessClient(core, job, [call("read_page", {"url": "https://example.test/paper"}, "read1"), call("write_research_report", args), {"role": "assistant", "content": "Report created."}])
    result = run(client, "Produce a cited report")
    assert result["ok"]
    assert result["verification"]["status"] == "verified"
    report = core.universal_platform.work.workspace_read("report.md")
    assert "https://example.test/paper" in report["content"]


def test_coding_verification_rejects_stale_tests(core):
    queue(core, "Fix code", {"template": "coding"})
    u = core.universal_platform
    job = u.store.claim("coding")
    client = InProcessClient(core, job, [])
    saved = u.work.workspace_write("code.py", "x = 1\n")
    source_hash = u.work.workspace_fingerprint()["sha256"]
    artifact = u.work.register_artifact("code.py", "code")
    state = {"turns": 2, "plan": [{"step": "fix and verify", "status": "done"}], "actions": [
        {"tool": "workspace_test", "ok": True, "result": {"verified": True, "source_sha256": source_hash, "path": ""}},
        {"tool": "register_artifact", "ok": True, "result": artifact},
    ]}
    assert verify(client, job["payload"], state, "agent")["passed"]
    u.work.workspace_write("code.py", "x = 2\n", saved["sha256"])
    state["turns"] += 1
    result = verify(client, job["payload"], state, "agent")
    assert not result["passed"]
    assert not next(c for c in result["checks"] if c["name"] == "sandbox_tests_on_current_source")["passed"]
    read_only_state = {"turns": 1, "actions": [{"tool": "get_current_datetime", "ok": True, "result": {"local_iso": "now"}}],
                       "messages": [{"role": "system", "content": "test"},
                                    {"role": "user", "content": "Report the date. Do not create or modify files."}]}
    read_only = verify(client, {"template": "office"}, read_only_state, "clock")
    assert read_only["passed"] and not any(c["name"] == "artifact_hashes" for c in read_only["checks"])


def test_schedule_coalesces_and_does_not_overlap_blocked_task(core):
    store = core.universal_platform.store
    store.schedule("research", "Read sources", 60, {"template": "research"})
    job = store.claim("worker")
    assert job["objective"] == "Read sources"
    store.complete(job["id"], job["lease_token"], "blocked", {
        "pending_action": {"tool": "workspace_test", "args": {"path": "copy"}}
    })
    with store.connect() as db:
        db.execute("UPDATE task_schedules SET next_run=0")
    assert store.claim("worker") is None
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0] == 1


def test_schedule_retries_after_non_permission_block(core):
    store = core.universal_platform.store
    store.schedule("research", "Read sources", 60, {"template": "research"})
    first = store.claim("worker")
    store.complete(first["id"], first["lease_token"], "blocked", {
        "failure_code": "step_budget_exhausted", "answer": "bounded cycle ended"
    })
    with store.connect() as db:
        db.execute("UPDATE task_schedules SET next_run=0")
    second = store.claim("worker")
    assert second is not None and second["id"] != first["id"]


def test_schedule_records_wake_catch_up_metadata(core):
    store = core.universal_platform.store
    store.schedule("overnight", "Run after wake", 60, {"template": "research"})
    with store.connect() as db:
        db.execute("UPDATE task_schedules SET next_run=? WHERE name='overnight'", (time.time() - 300,))
    job = store.claim("wake-worker")
    trigger = job["payload"]["_schedule_trigger"]
    assert trigger["name"] == "overnight"
    assert trigger["wake_catch_up"] is True and trigger["late_by_seconds"] >= 299
    with store.connect() as db:
        event = db.execute("SELECT data_json FROM platform_events WHERE kind='schedule.triggered' ORDER BY id DESC LIMIT 1").fetchone()
    assert event is not None and job["id"] in event[0]


def test_prompt_presets_round_trip_and_validate(core):
    store = core.universal_platform.store
    saved = store.save_prompt_preset("Night research", "Investigate the highest-value open question",
                                     template="research", priority=0.9, approval_mode="full_auto")
    assert saved == {"name": "Night research", "saved": True}
    preset = core.universal_platform.status()["prompt_presets"][0]
    assert preset["name"] == "Night research" and preset["template"] == "research"
    assert preset["browser_escalation"] is False
    assert store.delete_prompt_preset("Night research")["deleted"] is True


def test_sandbox_receives_filtered_snapshot(core, monkeypatch):
    import work_tools
    u = core.universal_platform
    (u.work.root / ".env").write_text("SECRET=private")
    u.work.workspace_write("test_math.py", "import unittest\n")
    seen = {}
    def fake_run(command, **kwargs):
        mount = command[command.index("-v") + 1]
        from pathlib import Path
        snapshot = Path(mount.rsplit(":/app:", 1)[0])
        seen["files"] = [p.name for p in snapshot.iterdir()]
        assert "--network" in command and "--read-only" in command and "--pids-limit=128" in command
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "Ran 1 test in 0.01s\nOK"}
    monkeypatch.setattr(work_tools, "run_container", fake_run)
    result = u.work.workspace_test()
    assert result["verified"]
    assert "test_math.py" in seen["files"]
    assert ".env" not in seen["files"]
    assert "core.db" not in seen["files"]


def test_workflow_resumes_after_permission_pause_without_repeating_steps(core, monkeypatch):
    import importlib.util
    from pathlib import Path
    u = core.universal_platform
    steps = [{"type": "tool", "tool": "capture_note", "args": {"text": "workflow ran once"}},
             {"type": "tool", "tool": "workspace_write", "args": {"path": "workflow.txt", "content": "done"}}]
    with u.store.connect() as db:
        db.execute("INSERT INTO workflows(id,name,steps_json,enabled) VALUES('wf-test','test workflow',?,0)", (canonical(steps),))
    with core.app.app_context():
        core.agent_platform._queue_job("workflow", "Workflow", {"workflow_id": "wf-test"})
    job = u.store.claim("workflow-worker")
    spec = importlib.util.spec_from_file_location("test_assistant_worker", Path(__file__).resolve().parents[1] / "assistant_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    client = InProcessClient(core, job, [])
    monkeypatch.setattr(worker, "client", client)
    first = worker.run_workflow(job)
    assert first["blocked"]
    assert client.checkpoint()["workflow"]["index"] == 1
    u.store.grant("assistant", "workspace_write", {"path": "workflow.txt"}, time.time() + 60, uses=1)
    assert worker.run_workflow(job)["ok"]
    assert client.checkpoint()["workflow"]["index"] == 2
    with u.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM capture_entries WHERE text='workflow ran once'").fetchone()[0] == 1


def test_dsh_engine_uses_bounded_adapter_and_checkpoints_trace(core, monkeypatch):
    import importlib.util
    from pathlib import Path
    jid = queue(core, "Inspect the repository with DSH", {"engine": "dsh", "max_seconds": 90})
    job = core.universal_platform.store.claim("dsh-worker")
    assert job["id"] == jid
    spec = importlib.util.spec_from_file_location("test_dsh_assistant_worker", Path(__file__).resolve().parents[1] / "assistant_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker.client = InProcessClient(core, job, [])
    events = []
    monkeypatch.setattr(worker, "core_post", lambda path, data: (events.append((path, data)) or {"ok": True}))

    def fake_dsh(objective, workspace, **options):
        assert "HEADLESS CODING EXECUTION" in objective
        assert "Inspect the repository with DSH" in objective
        assert "changed files, tests run" in objective
        assert options["timeout_seconds"] == 90 and options["require_clean"] is True
        options["event"]("dsh.trace", {"channel": "stderr", "text": "bounded trace"})
        return {"ok": True, "engine": "dsh", "answer": "done", "returncode": 0,
                "git_status": "", "native_session_logs": ["C:/private/session.v3.jsonl.zstd"],
                "verification": {"status": "observed", "checks": []}}

    monkeypatch.setattr(worker, "run_dsh_headless", fake_dsh)
    status, result = worker.process_job(job, lambda: False)
    assert status == "done" and result["answer"] == "done"
    assert events[0][1]["kind"] == "dsh.trace"
    assert result["actions"][0]["result"]["native_session_logs"]
    assert worker.client.checkpoint()["dsh"]["phase"] == "complete"
