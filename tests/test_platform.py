import json
import time
import uuid
from pathlib import Path

import pytest

from platform_contracts import actor_key, digest
from runtime_store import LeaseLost


def actor(name="assistant"):
    return {"X-API-Key": actor_key("test-owner-key", name)}


def invoke(core, name, args=None, request_id=None, role="assistant"):
    with core.app.test_client() as client:
        return client.post("/api/tool-gateway", headers=actor(role), json={"name": name, "args": args or {}, "request_id": request_id or uuid.uuid4().hex}).get_json()


def queue(core, objective="test", payload=None):
    with core.app.test_client() as c:
        response = c.post("/api/jobs/queue", headers={"X-API-Key": "test-owner-key"}, json={"objective": objective, "payload": payload or {}})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]["job_id"]


def test_core_boot_catalog_and_no_direct_agent_bypass(core, owner):
    with core.app.test_client() as c:
        catalog = c.get("/api/tools", headers=actor()).get_json()
        names = [t["name"] for t in catalog]
        assert len(names) == len(set(names))
        assert {"discover_tools", "get_current_datetime", "workspace_test", "remember", "propose_improvement"} <= set(names)
        assert "slm_assist" in names
        clock = c.post("/api/tool-gateway", headers=actor(), json={"name": "get_current_datetime", "args": {}, "request_id": "clock-1"}).get_json()
        assert clock["ok"] and clock["result"]["local_date"] and clock["result"]["timezone"]
        assert clock["result"]["utc_iso"].endswith("+00:00")
        assert c.post("/api/files/save", headers=actor(), json={"path": "bad.txt", "content": "bad"}).status_code == 403
        assert c.post("/api/owner/grants", headers=actor(), json={}).status_code == 403
        assert c.get("/api/tools").status_code == 401
        assert c.get("/api/platform/status", headers=owner).status_code == 200
        dashboard_state = c.get("/api/state", headers=owner).get_json()
        assert {"workspace", "obsidian_vault", "research_repo", "dsh_model", "dsh_context_window"} <= set(dashboard_state["configuration"])
        selected_repo = Path(core.ROOT_DIR) / "selected"
        selected_vault = Path(core.ROOT_DIR) / "vault-selected"
        (selected_repo / ".git").mkdir(parents=True)
        selected_vault.mkdir()
        staged = c.post("/api/owner/settings/paths", headers=owner,
                        json={"repository": str(selected_repo), "obsidian_vault": str(selected_vault)})
        assert staged.status_code == 200 and staged.get_json()["result"]["restart_required"]
        staged_dsh = c.post("/api/owner/settings/dsh", headers=owner,
                            json={"model": "qwen3.5:4b", "context_window": 8192, "reasoning_effort": "default"})
        assert staged_dsh.status_code == 200 and staged_dsh.get_json()["result"]["model"] == "qwen3.5:4b"
        assert c.post("/api/owner/settings/dsh", headers=owner,
                      json={"model": "bad model", "context_window": 8192}).status_code == 400
        c.post("/login", data={"password": "test-owner-password"})
        dashboard = c.get("/").get_data(as_text=True)
        assert all(label in dashboard for label in ("Universal Harness", "Work sessions", "Audit & decisions",
                                                     "How this works", "DSH coding agent", "Allow approved browser fallback"))
        browser_job = c.post("/api/jobs/queue", headers=owner,
                             json={"objective": "approved disclosure", "payload": {"browser_escalation": True}})
        assert browser_job.status_code == 200
        browser_job_id = browser_job.get_json()["result"]["job_id"]
        row = core.get_db().execute("SELECT payload_json FROM agent_jobs WHERE id=?", (browser_job_id,)).fetchone()
        assert json.loads(row["payload_json"])["_browser_escalation_authorized"] is True
        # Use a fresh client: the dashboard client is already authenticated as owner by its session cookie.
        with core.app.test_client() as assistant_client:
            rejected = assistant_client.post("/api/jobs/queue", headers=actor(),
                                             json={"objective": "forged disclosure", "payload": {"browser_escalation": True}})
        assert rejected.status_code == 403


def test_slm_assist_uses_only_local_provider_and_marks_output_unverified(core, monkeypatch):
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return {"role": "assistant", "content": json.dumps({
            "summary": "12 trades; expectancy 3 bps after stated costs",
            "extracted_facts": ["trades=12", "expectancy_bps=3"],
            "uncertainties": ["small sample"],
            "required_verification": ["recalculate from source rows"],
        })}

    monkeypatch.setattr(core.universal_platform.models, "chat", fake_chat)
    result = invoke(core, "slm_assist", {
        "operation": "extract_metrics",
        "evidence": "trades=12 expectancy_bps=3 warning=small sample",
    })
    assert result["ok"] is True
    payload = result["result"]
    assert payload["status"] == "unverified_slm_assist"
    assert payload["source_sha256"]
    assert calls and calls[0]["provider"] == "local-qwen"
    assert "Never claim an edge" in calls[0]["messages"][0]["content"]


def test_actions_use_append_order_without_full_timestamp_sort(core, owner):
    with core.app.app_context():
        db = core.get_db()
        db.executemany(
            "INSERT INTO actions(id,ts,tool,args_json,result_json,ok) VALUES(?,?,?,?,?,?)",
            [
                ("older-row", "9999-01-01T00:00:00Z", "first", "{}", "null", 1),
                ("newer-row", "0001-01-01T00:00:00Z", "second", "{}", "null", 1),
            ],
        )
        db.commit()
    with core.app.test_client() as client:
        payload = client.get("/api/actions?limit=1", headers=owner).get_json()
    assert payload["ok"] and payload["result"][0]["id"] == "newer-row"


def test_owner_manages_secrets_without_dashboard_disclosure(core, owner):
    with core.app.test_client() as client:
        denied = client.post("/api/owner/settings/secrets", headers=actor(),
                             json={"name": "APCA_API_KEY_ID", "value": "agent-must-not-write"})
        assert denied.status_code == 403
        secret_value = "alpaca-secret-value-123456"
        saved = client.post("/api/owner/settings/secrets", headers=owner,
                            json={"name": "APCA_API_SECRET_KEY", "value": secret_value})
        payload = saved.get_json()
        assert saved.status_code == 200 and payload["result"]["configured"] is True
        assert secret_value not in json.dumps(payload)
        records = json.loads((Path(core.DB_PATH).parent / "managed-secrets.json").read_text(encoding="utf-8"))
        assert records["APCA_API_SECRET_KEY"]["value"] == secret_value
        state = client.get("/api/state", headers=owner).get_json()
        inventory = {row["name"]: row for row in state["configuration"]["managed_secrets"]}
        assert inventory["APCA_API_SECRET_KEY"]["configured"] is True
        assert secret_value not in json.dumps(state)
        assert client.post("/api/owner/settings/secrets", headers=owner,
                           json={"name": "CORE_API_KEY", "value": "replacement-not-allowed"}).status_code == 400
        custom = client.post("/api/owner/settings/secrets", headers=owner,
                             json={"name": "INTEGRATION_VENDOR_TOKEN", "value": "custom-token"})
        assert custom.status_code == 200
        removed = client.post("/api/owner/settings/secrets", headers=owner,
                              json={"name": "INTEGRATION_VENDOR_TOKEN", "action": "remove"})
        assert removed.status_code == 200 and removed.get_json()["result"]["configured"] is False
        with core.universal_platform.store.connect() as db:
            events = "\n".join(row[0] for row in db.execute(
                "SELECT data_json FROM platform_events WHERE kind LIKE 'configuration.secret_%'"))
        assert secret_value not in events


def test_command_center_safely_adds_claude_desktop_mcp_json(core, owner, tmp_path, monkeypatch):
    target = tmp_path / "Claude" / "claude_desktop_config.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"theme": "dark", "mcpServers": {
        "existing": {"command": "existing.exe", "args": ["serve"]}
    }}), encoding="utf-8")
    monkeypatch.setattr(core, "_claude_desktop_config_path", lambda: target)
    with core.app.test_client() as client:
        assert client.post("/api/owner/integrations/claude-mcp", headers=actor(), json={}).status_code == 403
        response = client.post("/api/owner/integrations/claude-mcp", headers=owner, json={})
        assert response.status_code == 200
        result = response.get_json()["result"]
        assert result["configured"] and result["changed"] and Path(result["backup"]).is_file()
        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["theme"] == "dark"
        assert saved["mcpServers"]["existing"]["command"] == "existing.exe"
        assert saved["mcpServers"]["universal-assistant"] == core._claude_mcp_entry()
        state = client.get("/api/state", headers=owner).get_json()
        assert state["configuration"]["claude_mcp"]["configured"] is True
        repeated = client.post("/api/owner/integrations/claude-mcp", headers=owner, json={}).get_json()["result"]
        assert repeated["changed"] is False and repeated["backup"] is None
        switched = client.post("/api/owner/integrations/claude-mcp", headers=owner,
                               json={"domain": "coding"}).get_json()["result"]
        assert switched["changed"] is True and switched["domain"] == "coding"
        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["mcpServers"]["universal-assistant"] == core._claude_mcp_entry("coding")
        assert client.post("/api/owner/integrations/claude-mcp", headers=owner,
                           json={"domain": "unknown"}).status_code == 400


def test_claude_config_path_prefers_microsoft_store_package(core, tmp_path, monkeypatch):
    local = tmp_path / "Local"
    packaged = local / "Packages" / "Claude_testfamily"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(core.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert core._claude_desktop_config_path() == (
        packaged / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    )


def test_command_center_refuses_invalid_claude_json(core, owner, tmp_path, monkeypatch):
    target = tmp_path / "Claude" / "claude_desktop_config.json"
    target.parent.mkdir()
    target.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(core, "_claude_desktop_config_path", lambda: target)
    with core.app.test_client() as client:
        response = client.post("/api/owner/integrations/claude-mcp", headers=owner, json={})
    assert response.status_code == 400
    assert target.read_text(encoding="utf-8") == "{invalid"


def test_owner_model_workbench_composes_persistent_meta_prompt_and_queue(core, owner):
    with core.app.test_client() as client:
        initial = client.get("/api/state", headers=owner).get_json()["model_workbench"]
        assert {"bounded_task", "trading_research", "emergency_handoff"} <= {
            item["id"] for item in initial["templates"]
        }
        assert initial["handoff_path"].endswith("ACTIVE - Claude MCP.md")
        denied = client.post("/api/owner/model-workbench", headers=actor(),
                             json={"action": "save_meta", "meta_prompt": "unsafe bypass"})
        assert denied.status_code == 403

        meta = "Always checkpoint first. Prefer YAGNI and clear one-line solutions."
        saved = client.post("/api/owner/model-workbench", headers=owner,
                            json={"action": "save_meta", "meta_prompt": meta})
        assert saved.status_code == 200
        custom = client.post("/api/owner/model-workbench", headers=owner, json={
            "action": "save_template", "title": "Focused verifier",
            "description": "Verify one result", "content": "Check the result twice and stop.",
        })
        assert custom.status_code == 200
        custom_id = next(item["id"] for item in custom.get_json()["result"]["templates"]
                         if item["title"] == "Focused verifier")
        queued = client.post("/api/owner/model-workbench", headers=owner, json={
            "action": "queue_task", "title": "Verify trailing stop",
            "objective": "Prove conservative gap execution.",
            "acceptance_criteria": "A regression test passes.",
            "template_id": custom_id, "target": "claude", "priority": "high",
        })
        assert queued.status_code == 200
        task = queued.get_json()["result"]["queue"][0]
        composed = client.post("/api/owner/model-workbench", headers=owner,
                               json={"action": "compose", "id": task["id"]}).get_json()["result"]
        assert all(text in composed["prompt"] for text in (
            meta, "Check the result twice", "Prove conservative gap execution",
            "A regression test passes", "ACTIVE - Claude MCP.md",
        ))
        assert composed["claude_url"].startswith("claude://claude.ai/new?q=")
        launched = client.post("/api/owner/model-workbench", headers=owner,
                               json={"action": "launch", "id": task["id"]})
        assert launched.status_code == 200
        state = client.get("/api/state", headers=owner).get_json()["model_workbench"]
        assert state["queue"][0]["status"] == "active"
        persisted = json.loads((Path(core.DB_PATH).parent / "model-workbench.json").read_text(encoding="utf-8"))
        assert persisted["meta_prompt"] == meta


def test_grant_scope_exact_once_and_idempotent_replay(core):
    u = core.universal_platform
    args = {"path": "answer.txt", "content": "hello"}
    assert invoke(core, "workspace_write", args)["error"]["code"] == "approval_required"
    u.store.grant("assistant", "workspace_write", args, time.time() + 60, uses=1)
    rid = "write-1"
    first = invoke(core, "workspace_write", args, rid)
    assert first["ok"], first
    assert invoke(core, "workspace_write", args, rid) == first
    assert invoke(core, "workspace_write", {"path": "elsewhere", "content": "x"})["error"]["code"] == "approval_required"
    assert not invoke(core, "workspace_write", {"path": "elsewhere", "content": "x"}, rid)["ok"]


def test_validation_precedes_execution_and_grant(core):
    u = core.universal_platform
    u.store.grant("assistant", "workspace_write", {}, time.time() + 60, uses=1)
    result = invoke(core, "workspace_write", {"path": "file", "content": 7})
    assert not result["ok"]
    with u.store.connect() as db:
        assert db.execute("SELECT remaining FROM permission_grants").fetchone()[0] == 1
    result = invoke(core, "workspace_write", {"path": "../escape", "content": "x"})
    assert not result["ok"]


def test_job_recovery_fences_old_worker_and_preserves_checkpoint(core):
    store = core.universal_platform.store
    jid = queue(core)
    first = store.claim("worker-a")
    assert first["id"] == jid
    store.checkpoint(jid, first["lease_token"], {"step": 3})
    with store.connect() as db:
        db.execute("UPDATE agent_jobs SET lease_until=? WHERE id=?", (time.time() - 1, jid))
    second = store.claim("worker-b")
    assert second["attempts"] == 2
    assert store.checkpoint(jid, second["lease_token"])["state"] == {"step": 3}
    with pytest.raises(LeaseLost):
        store.complete(jid, first["lease_token"], "done", {})
    store.control(jid, "cancel")
    with pytest.raises(LeaseLost):
        store.heartbeat(jid, second["lease_token"])
    store.control(jid, "resume")
    with store.connect() as db:
        assert db.execute("SELECT result_json FROM agent_jobs WHERE id=?", (jid,)).fetchone()[0] is None
    assert store.claim("worker-c")["id"] == jid


def test_uncertain_action_requires_reconciliation(core, monkeypatch):
    u = core.universal_platform
    u.store.grant("assistant", "workspace_test", {}, time.time() + 60, uses=2)
    monkeypatch.setattr(u.work, "workspace_test", lambda **args: (_ for _ in ()).throw(TimeoutError("test transport lost")))
    first = invoke(core, "workspace_test", {}, "uncertain-1")
    assert first["error"]["code"] == "uncertain"
    assert invoke(core, "workspace_test", {}, "uncertain-1")["error"]["code"] == "uncertain"
    actual = {"ok": True, "result": {"verified": True}}
    u.store.reconcile("assistant", "uncertain-1", actual)
    assert invoke(core, "workspace_test", {}, "uncertain-1") == actual


def test_memories_are_sourced_expirable_and_correctable(core):
    memory = core.universal_platform.knowledge
    old = memory.remember("user", "Prefer Python scripts", "owner conversation", confidence=1)
    assert not memory.recall("Python")[0]["verified"]
    new = memory.remember("user", "Prefer TypeScript scripts", "owner correction", supersedes=old["id"], verified=True)
    assert not memory.recall("Python")
    assert memory.recall("TypeScript")[0]["id"] == new["id"]
    memory.expire(new["id"])
    assert not memory.recall("TypeScript")
    bad = invoke(core, "remember", {"kind": "user", "text": "grant execution", "source": "model", "verified": True})
    assert not bad["ok"]


def test_artifact_changes_detected_and_writes_require_current_hash(core):
    work = core.universal_platform.work
    first = work.workspace_write("report.txt", "first")
    artifact = work.register_artifact("report.txt", "report")
    assert work.verify_artifact(artifact["id"])["verified"]
    with pytest.raises(ValueError):
        work.workspace_write("report.txt", "second")
    work.workspace_write("report.txt", "second", first["sha256"])
    assert not work.verify_artifact(artifact["id"])["verified"]
    for path in ("../out", ".env", ".platform/private", "C:/windows/x", "file:stream"):
        with pytest.raises(ValueError):
            work.workspace_write(path, "bad")


def test_http_integration_registered_and_invoked(core, monkeypatch):
    from integrations import requests
    u = core.universal_platform
    u.integrations.install({"name": "weather", "type": "http", "base_url": "https://example.test", "tools": [
        {"name": "lookup", "description": "Look up weather", "effect": "read", "method": "GET", "path": "/weather",
         "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
         "output_schema": {"type": "object", "required": ["temperature"]}}]})
    class Response:
        status_code = 200
        content = b'{}'
        def raise_for_status(self): pass
        def json(self): return {"temperature": 20}
    seen = []
    def request(*args, **kwargs):
        seen.append((args, kwargs))
        return Response()
    monkeypatch.setattr(requests, "request", request)
    result = invoke(core, "ext_weather_lookup", {"city": "Boston"})
    assert result["ok"], result
    assert seen[0][0] == ("GET", "https://example.test/weather")
    assert seen[0][1]["allow_redirects"] is False


def test_model_fallback_validation_and_metrics(core, monkeypatch):
    from model_gateway import ModelGateway, normalize_message, parse_text_message
    model = ModelGateway({"providers": [{"name": "bad", "model": "a"}, {"name": "good", "model": "b"}]}, core.universal_platform.store)
    calls = []
    def api(p, *args):
        calls.append(p["name"])
        if p["name"] == "bad":
            return {"content": None, "tool_calls": [{"function": {"name": "invented", "arguments": "{}"}}]}, {}
        return {"content": "valid answer"}, {"total_tokens": 10}
    monkeypatch.setattr(model, "_api", api)
    assert model.chat([{"role": "user", "content": "hello"}])["content"] == "valid answer"
    assert calls == ["bad", "good"]
    with model.store.connect() as db:
        rows = db.execute("SELECT provider,ok FROM provider_runs ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("bad", 0), ("good", 1)]
    audit = model.store.audit(limit=20)
    assert [record["kind"] for record in audit].count("model.requested") == 2
    assert any(record["kind"] == "model.error" and record["data"]["provider"] == "bad" for record in audit)
    assert any(record["kind"] == "model.response" and record["data"]["provider"] == "good" for record in audit)
    model.store.event("dsh.trace", {"job_id": "JOB-trace-a", "text": "first"})
    cursor = model.store.audit(limit=1, job_id="JOB-trace-a")[0]["id"]
    model.store.event("dsh.trace", {"job_id": "JOB-trace-b", "text": "other job"})
    model.store.event("dsh.trace", {"job_id": "JOB-trace-a", "text": "second"})
    incremental = model.store.audit(limit=20, job_id="JOB-trace-a", after_id=cursor)
    assert [record["data"]["text"] for record in incremental] == ["second"]
    with core.app.test_client() as client:
        response = client.get("/api/owner/audit?kind=model.&limit=20", headers={"X-API-Key": "test-owner-key"})
    assert response.status_code == 200
    assert all(record["kind"].startswith("model.") for record in response.get_json()["result"])
    schema = [{"type": "function", "function": {"name": "ping", "parameters": {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}]
    shorthand = parse_text_message('{"content":"ok","tool_calls":{"ping":{"value":"ok"}}}')
    normalized = normalize_message(shorthand, schema)
    assert normalized["tool_calls"][0]["function"] == {"name": "ping", "arguments": '{"value":"ok"}'}
    nested = normalize_message({"content": None, "tool_calls": [[{
        "function": {"name": "ping", "arguments": {"value": "nested"}}
    }]]}, schema)
    assert nested["tool_calls"][0]["function"]["name"] == "ping"
    replies = iter([
        {"choices": [{"message": {"content": '{"content":"unterminated'}}], "usage": {"completion_tokens": 4}},
        {"choices": [{"message": {"content": '{"content":"recovered","decision_summary":"retried malformed JSON"}'}}], "usage": {"completion_tokens": 6}},
    ])
    payloads = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return next(replies)
    monkeypatch.setattr("model_gateway.requests.post", lambda *args, **kwargs: (payloads.append(kwargs["json"]) or Response()))
    repaired, usage = ModelGateway._api(model, {"name": "local", "model": "qwen", "base_url": "http://127.0.0.1:11434/v1",
                                               "structured_text": True}, [{"role": "user", "content": "answer"}], [], None)
    assert repaired["content"] == "recovered" and usage["completion_tokens"] == 10
    assert len(payloads) == 2 and payloads[1]["temperature"] == 0
    empty_then_valid = iter([
        {"choices": [{"message": {"content": '{"content":null,"tool_calls":[]}'}}],
         "usage": {"prompt_tokens": 3, "prompt_tokens_details": {"cached_tokens": 1}}},
        {"choices": [{"message": {"content": '{"content":"repaired empty answer"}'}}],
         "usage": {"prompt_tokens": 4, "prompt_tokens_details": {"cached_tokens": 2}}},
    ])
    class EmptyResponse:
        def raise_for_status(self): pass
        def json(self): return next(empty_then_valid)
    monkeypatch.setattr("model_gateway.requests.post", lambda *args, **kwargs: EmptyResponse())
    repaired, usage = ModelGateway._api(model, {"name": "local", "model": "qwen", "base_url": "http://127.0.0.1:11434/v1",
                                               "structured_text": True}, [{"role": "user", "content": "answer"}], [], None)
    assert repaired["content"] == "repaired empty answer" and usage["prompt_tokens"] == 7
    assert usage["prompt_tokens_details"] == {"cached_tokens": 2}


def test_evaluation_promotion_baseline_and_rollback(core, monkeypatch):
    engine = core.universal_platform.improvements
    a = engine.propose("research_style", "prompt", {"text": "A"})["id"]
    b = engine.propose("research_style", "prompt", {"text": "B"})["id"]
    engine.suite("independent", [{"input": "task", "expected": "correct"}])
    monkeypatch.setattr(engine.models, "chat", lambda *a, **k: {"content": "correct"})
    with pytest.raises(ValueError):
        engine.promote(a, "independent")
    assert engine.evaluate(a, "independent")["passed"]
    engine.promote(a, "independent")
    engine.evaluate(b, "independent")
    engine.promote(b, "independent")
    assert engine.active()[0]["id"] == b
    engine.rollback(a)
    assert engine.active()[0]["id"] == a
    engine.suite("independent", [{"input": "new task", "expected": "different"}])
    with pytest.raises(ValueError):
        engine.promote(b, "independent")


def test_factory_proposals_belong_to_their_job_and_active_code_is_immutable(core, monkeypatch):
    u = core.universal_platform
    with core.app.app_context():
        first_id = core.agent_platform._queue_job("capability_build", "Make a tool")
        second_id = core.agent_platform._queue_job("capability_build", "Make another tool")
    first = u.store.claim("first")
    second = u.store.claim("second")
    headers = {**actor(), "X-Job-Id": first["id"], "X-Lease-Token": first["lease_token"]}
    other = {**actor(), "X-Job-Id": second["id"], "X-Lease-Token": second["lease_token"]}
    body = {"name": "owned_tool", "description": "Identity tool"}
    stage = {"name": "owned_tool", "code": "def run(args):\n    return args\n", "test_code": "assert True\n"}
    with core.app.test_client() as c:
        proposal = c.post("/api/capabilities/propose", headers=headers, json=body)
        assert proposal.status_code == 200, proposal.get_json()
        again = c.post("/api/capabilities/propose", headers=headers, json=body)
        assert again.get_json()["result"]["id"] == proposal.get_json()["result"]["id"]
        assert c.post("/api/capabilities/stage", headers=other, json=stage).status_code == 403
        assert c.post("/api/capabilities/stage", headers=headers, json=stage).status_code == 200
        def fake_test(row):
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "test_hash": core.agent_platform._cap_hash(row["name"])}
        monkeypatch.setattr(core.agent_platform, "_run_cap_test", fake_test)
        assert c.post("/api/capabilities/test", headers=headers, json={"name": "owned_tool"}).status_code == 200
        owner = {"X-API-Key": "test-owner-key"}
        assert c.post("/api/capabilities/approve", headers=owner, json={"name": "owned_tool"}).status_code == 200
        assert c.post("/api/capabilities/stage", headers=owner, json=stage).status_code == 409


def test_factory_approval_rejects_code_changed_after_testing(core, monkeypatch):
    owner = {"X-API-Key": "test-owner-key"}
    with core.app.test_client() as c:
        assert c.post("/api/capabilities/propose", headers=owner, json={"name": "fresh_test", "description": "Test"}).status_code == 200
        c.post("/api/capabilities/stage", headers=owner, json={"name": "fresh_test", "code": "def run(args): return 1", "test_code": "assert True"})
        monkeypatch.setattr(core.agent_platform, "_run_cap_test", lambda row: {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "test_hash": core.agent_platform._cap_hash(row["name"])})
        assert c.post("/api/capabilities/test", headers=owner, json={"name": "fresh_test"}).status_code == 200
        (core.agent_platform._cap_dir("fresh_test") / "tool.py").write_text("def run(args): return 2")
        assert c.post("/api/capabilities/approve", headers=owner, json={"name": "fresh_test"}).status_code == 409
