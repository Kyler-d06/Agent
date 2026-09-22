import importlib
import json
import sqlite3
import time

import pytest

from agent_platform import PLATFORM_SCHEMA
from intelligence_platform import SCHEMA
from news_store import initialize, fingerprint
from run_regressions import run
from run_regressions import run_prompt
from evaluation_prompts import DISCOVERY_QUALITY_PROMPT
from test_platform import invoke


def test_fresh_schema_has_runtime_columns_without_store():
    db = sqlite3.connect(":memory:")
    db.executescript(PLATFORM_SCHEMA)
    assert {"builder_job_id", "test_hash"} <= {r[1] for r in db.execute("PRAGMA table_info(capabilities)")}
    assert {"lease_token", "lease_until", "attempts"} <= {r[1] for r in db.execute("PRAGMA table_info(agent_jobs)")}


def test_vault_gateway_permissions_and_confinement(core):
    assert invoke(core, "sync_obsidian", role="discovery")["ok"]
    assert invoke(core, "calculate", {"expression": "max(1, 2)"})["result"] == "2"
    args = {"note_path": "tasks.md", "content": "- [ ] test"}
    assert invoke(core, "vault_write_note", args)["error"]["code"] == "approval_required"
    core.universal_platform.store.grant("assistant", "vault_write_note", args, time.time() + 60)
    assert invoke(core, "vault_write_note", args)["ok"]
    assert invoke(core, "vault_read_note", {"note_path": "tasks.md"})["result"] == "- [ ] test"
    for path in ("../core.db", str(core.vault_platform.vault.root / "tasks.md"), ".obsidian/config.json"):
        assert not invoke(core, "vault_read_file", {"file_path": path})["ok"]
    with pytest.raises(ValueError):
        core.vault_platform.vault.write_note("../escape.md", "no")


def test_real_calculator_evaluation_and_promotion(tmp_path):
    result = run(tmp_path)
    assert result["passed"] and result["score"] == 1
    assert len(result["cases"]) >= 18
    assert result["promotion"]["status"] == "active"
    db = sqlite3.connect(tmp_path / "evaluations.db")
    assert db.execute("SELECT count(*) FROM evaluations WHERE passed=1").fetchone()[0] == 1
    assert run(tmp_path)["version_id"] == result["version_id"]


def test_owner_authored_rsi_suites_are_structural_and_bounded():
    root = __import__("pathlib").Path(__file__).parents[1] / "evaluations"
    for name in ("discovery_pipeline.json", "capability_factory.json"):
        spec = json.loads((root / name).read_text(encoding="utf-8"))
        assert 20 <= len(spec["cases"]) <= 30
        assert all(case["matcher"] == "schema" and case["expected"]["type"] == "object" for case in spec["cases"])


def test_discovery_prompt_runs_through_engine_and_promotes(tmp_path):
    class Models:
        def chat(self, messages, **kwargs):
            case = json.loads(messages[-1]["content"])
            return {"content": json.dumps({
                "case_id": case["case_id"], "hypothesis": "A measurable relationship exists under the stated conditions.",
                "falsification_criterion": "Lower confidence if the held-out estimate includes zero at the preregistered precision.",
                "strongest_counterevidence": "The available sample has an unresolved selection bias.",
                "next_decisive_test": "Run a held-out comparison against the strongest baseline and a null alternative.",
                "confidence": 0.4, "evidence_classification": ["observation", "inference", "hypothesis"],
                "source_plan": ["primary dataset", "independent replication"], "uncertainties": ["selection bias"]
            })}
    result = run_prompt(tmp_path, "discovery_pipeline.json", "discovery_quality", DISCOVERY_QUALITY_PROMPT,
                        ["research"], models=Models())
    assert result["passed"] and result["score"] == 1 and len(result["cases"]) == 24
    assert result["promotion"]["status"] == "active"


def test_news_cross_feed_dedup_retains_sources_and_corrections(core, owner):
    with core.app.test_client() as client:
        story = {"feed": "one", "external_id": "1", "text": "The port authority reports that 12 ships arrived at the terminal today.", "url": "https://example.org/a"}
        first = client.post("/api/world/ingest", headers=owner, json=story).get_json()["result"]
        again = client.post("/api/world/ingest", headers=owner, json=story).get_json()["result"]
        assert again["duplicate"] and again["id"] == first["id"]
        second = client.post("/api/world/ingest", headers=owner, json={**story, "feed": "two", "external_id": "2"}).get_json()["result"]
        assert second["duplicate"] and second["id"] == first["id"]
        sources = client.get("/api/world/item/sources", headers=owner, query_string={"id": first["id"]}).get_json()["result"]["sources"]
        assert len(sources) == 2
        corrected = client.post("/api/world/ingest", headers=owner, json={**story, "text": story["text"].replace("12", "13")}).get_json()["result"]
        assert corrected["id"] != first["id"] and not corrected.get("duplicate")


def test_old_news_migration_deletes_duplicates_and_preserves_old_ids():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    text = "An identical syndicated story with sufficient text to identify an exact duplicate."
    for iid, feed in (("a", "one"), ("b", "two")):
        db.execute("INSERT INTO world_items(id,feed,external_id,text) VALUES(?,?,?,?)", (iid, feed, iid, text))
    db.commit()
    assert initialize(db) == 1
    assert db.execute("SELECT count(*) FROM world_items").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM world_item_sources").fetchone()[0] == 2
    assert db.execute("SELECT world_item_id FROM world_item_aliases WHERE original_id='b'").fetchone()[0] == "a"
    assert initialize(db) == 0


def test_news_normalization_preserves_comparisons_and_negation():
    prefix = "The port authority has issued its daily shipping forecast: "
    assert fingerprint(prefix + "volume < 5 and price > 6") != fingerprint(prefix + "volume < 9 and price > 6")
    assert fingerprint(prefix + "ships will arrive") != fingerprint(prefix + "ships will not arrive")
    assert fingerprint(prefix + "<b>12 ships</b> arrive") == fingerprint(prefix + "12 ships arrive")


def test_builtin_failed_evaluation_blocks_promotion(tmp_path, monkeypatch):
    import tools
    from improvement_engine import ImprovementEngine
    from runtime_store import RuntimeStore
    engine = ImprovementEngine(RuntimeStore(tmp_path / "failed.db"), tmp_path, None)
    engine.suite("calculator_check", [{"input": {"expression": "2+2"}, "expected": "4"}])
    version = engine.register_builtin("calculate")
    monkeypatch.setattr(engine, "_run_builtin", lambda *args: "5")
    assert not engine.evaluate(version["id"], "calculator_check")["passed"]
    with pytest.raises(ValueError, match="must pass"):
        engine.promote(version["id"], "calculator_check")


def test_builtin_entrypoint_drift_invalidates_promotion(tmp_path, monkeypatch):
    import tools
    from improvement_engine import ImprovementEngine
    from runtime_store import RuntimeStore
    engine = ImprovementEngine(RuntimeStore(tmp_path / "drift.db"), tmp_path, None)
    engine.suite("calculator_check", [{"input": {"expression": "2+2"}, "expected": "4"}])
    version = engine.register_builtin("calculate")
    assert engine.evaluate(version["id"], "calculator_check")["passed"]
    monkeypatch.setattr(tools, "calculate", lambda **args: "5")
    with pytest.raises(ValueError, match="source changed"):
        engine.promote(version["id"], "calculator_check")


def test_x_cursor_resumes_backlog_before_advancing(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "test")
    monkeypatch.setenv("X_BEARER_TOKEN", "test")
    monkeypatch.setenv("ALLOW_PAID_X", "1")
    monkeypatch.setenv("X_DAILY_REQUEST_LIMIT", "10")
    collector = importlib.import_module("world_sources")
    state, requests_seen, ingested = {"last_cursor": "100"}, [], []
    monkeypatch.setattr(collector, "feed_state", lambda name: dict(state))
    monkeypatch.setattr(collector, "set_cursor", lambda name, cursor: state.update(last_cursor=cursor))
    monkeypatch.setattr(collector, "ingest", lambda feed, **args: ingested.append(args["external_id"]))
    monkeypatch.setattr(collector, "post_json", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(collector, "get_json", lambda path, params=None: {"result": {"allowed": True}})
    class Response:
        status_code = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body
    responses = iter([
        {"data": [{"id": "300", "text": "newer"}], "meta": {"next_token": "older"}},
        {"data": [{"id": "200", "text": "older"}], "meta": {}},
    ])
    def get(*args, **kwargs):
        requests_seen.append(dict(kwargs["params"]))
        return Response(next(responses))
    monkeypatch.setattr(collector.requests, "get", get)
    feed = {"name": "test", "locator": "from:test", "config": {"max_pages": 1}}
    assert collector.poll_x(feed) == 1
    assert json.loads(state["last_cursor"])["since_id"] == "100"
    assert collector.poll_x(feed) == 1
    assert requests_seen[1]["since_id"] == "100" and requests_seen[1]["next_token"] == "older"
    assert state["last_cursor"] == "300" and ingested == ["300", "200"]
def test_discovery_fetch_tools_never_exposes_non_autonomous_live_catalog(core, monkeypatch, owner):
    import discovery_worker
    with core.app.test_client() as client:
        catalog = client.get("/api/tools", headers=owner).get_json()
    monkeypatch.setattr(discovery_worker.platform_client, "tools", lambda: catalog)
    monkeypatch.setattr(discovery_worker, "ALLOW_RESEARCH_WRITES", False)
    names = {tool["name"] for tool in discovery_worker.fetch_tools()}
    assert names <= discovery_worker.AUTONOMOUS_TOOLS and not names & {"run_file", "save_file", "commit_research"} and not any(name.startswith("cap_") for name in names)
    assert not names & {"create_hypothesis", "update_hypothesis", "add_evidence", "create_discovery"}


def test_discovery_writer_tools_require_explicit_operator_enablement(core, monkeypatch, owner):
    import discovery_worker
    with core.app.test_client() as client:
        catalog = client.get("/api/tools", headers=owner).get_json()
    monkeypatch.setattr(discovery_worker.platform_client, "tools", lambda: catalog)
    monkeypatch.setattr(discovery_worker, "ALLOW_RESEARCH_WRITES", True)
    names = {tool["name"] for tool in discovery_worker.fetch_tools()}
    assert {"create_hypothesis", "add_evidence", "create_discovery"} <= names


def test_research_records_require_falsifiability_and_real_source_references(core, owner):
    with core.app.test_client() as client:
        incomplete = client.post("/api/discovery/hypotheses", headers=owner, json={"claim": "Unsupported claim"})
        assert incomplete.status_code == 400
        created = client.post("/api/discovery/hypotheses", headers=owner, json={
            "claim": "A bounded test claim", "falsification_criterion": "A counterexample is observed",
            "strongest_counterargument": "The observation may be measurement error",
            "next_test": "Repeat the measurement independently", "sync_obsidian": False,
        })
        hid = created.get_json()["result"]["id"]
        bad = client.post("/api/discovery/evidence", headers=owner, json={
            "hypothesis_id": hid, "stance": "support", "summary": "A title is not a URL",
            "source_url": "CVE title only",
        })
        assert bad.status_code == 400
        good = client.post("/api/discovery/evidence", headers=owner, json={
            "hypothesis_id": hid, "stance": "neutral", "summary": "Direct local source inspected",
            "source_url": "workspace://core_app.py", "source_type": "local_source",
        })
        assert good.status_code == 200


def test_discovery_bounds_large_tool_results_without_losing_failure_state():
    import discovery_worker

    content = discovery_worker.bounded_tool_result(
        {"ok": False, "error": {"message": "source failed"}, "result": {"text": "x" * 10000}},
        limit=1200,
    )
    parsed = json.loads(content)
    assert len(content) <= 1200
    assert parsed["ok"] is False
    assert parsed["error"]["message"] == "source failed"
    assert parsed["result"]["truncated_for_model_context"] is True


def test_discovery_retries_reasoning_only_empty_turn(monkeypatch):
    import discovery_worker

    replies = iter([RuntimeError("empty provider response"), {"role": "assistant", "content": "recovered"}])
    calls = []

    def chat(messages, tools):
        calls.append((list(messages), tools))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(discovery_worker, "llm_chat", chat)
    messages = [{"role": "user", "content": "research"}]
    result = discovery_worker.resilient_llm_chat(messages, [])
    assert result["content"] == "recovered" and len(calls) == 2
    assert "no usable content" in messages[-1]["content"]


def test_discovery_bootstraps_evidence_and_bounds_non_progress(monkeypatch):
    import discovery_worker

    catalog = [
        {"name": "discovery_brief", "description": "brief", "input_schema": {"type": "object"},
         "effect": "read", "method": "POST", "path": "/api/tools/discovery_brief"},
        {"name": "build_context", "description": "context", "input_schema": {"type": "object"},
         "effect": "read", "method": "POST", "path": "/api/tools/build_context"},
    ]
    invoked = []
    fallback = []

    monkeypatch.setattr(discovery_worker, "RESOURCE_PAUSE_LEVEL", "off")
    monkeypatch.setattr(discovery_worker, "MAX_TOOL_ITERS", 5)
    monkeypatch.setattr(discovery_worker.platform_client, "tools", lambda: catalog)

    def invoke(name, args, confirmed=False):
        invoked.append((name, args))
        return {"ok": True, "result": {"name": name}}

    def chat(messages, tools, **kwargs):
        fallback.append(bool(kwargs.get("prefer_fallback")))
        return {"role": "assistant", "content": "I would inspect the repository next."}

    monkeypatch.setattr(discovery_worker.platform_client, "invoke", invoke)
    monkeypatch.setattr(discovery_worker.platform_client, "chat", chat)

    result = discovery_worker.run_cycle("find a reproducible defect")

    assert [name for name, _ in invoked] == ["discovery_brief", "build_context"]
    assert result.startswith("[discovery scout report]")
    assert "Status: insufficient evidence" in result
    assert "No canonical research state was changed" in result
    assert fallback == [False, False, True]


def test_discovery_defect_focus_bootstraps_inventory_and_root_scan(monkeypatch):
    import discovery_worker

    catalog = [
        {"name": name, "description": name, "input_schema": {"type": "object"},
         "effect": "read", "method": "POST", "path": "/api/tool-gateway"}
        for name in ("discovery_brief", "build_context", "workspace_list", "source_bug_scan")
    ]
    invoked = []
    monkeypatch.setattr(discovery_worker, "RESOURCE_PAUSE_LEVEL", "off")
    monkeypatch.setattr(discovery_worker, "MAX_TOOL_ITERS", 1)
    monkeypatch.setattr(discovery_worker, "ALLOW_RESEARCH_WRITES", False)
    monkeypatch.setattr(discovery_worker.platform_client, "tools", lambda: catalog)

    def invoke(name, args, confirmed=False):
        invoked.append((name, args))
        if name == "workspace_list":
            return {"ok": True, "result": [{"path": "core_app.py", "directory": False}]}
        if name == "source_bug_scan":
            return {"ok": True, "result": {"files_scanned": 64, "bytes_scanned": 934628,
                                                "summary": {"errors": 0, "warnings": 0}}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(discovery_worker.platform_client, "invoke", invoke)
    monkeypatch.setattr(discovery_worker.platform_client, "chat",
                        lambda messages, tools, **kwargs: {"role": "assistant", "content": "Let me inspect more:"})

    result = discovery_worker.run_cycle("inspect source tests for one reproducible defect")

    assert [name for name, _ in invoked[:4]] == [
        "discovery_brief", "build_context", "workspace_list", "source_bug_scan"
    ]
    assert "64 files / 934628 bytes" in result
    assert "Status: insufficient evidence" in result


def test_discovery_recovers_from_unfinished_report_after_tool_use(monkeypatch):
    import discovery_worker

    catalog = [
        {"name": name, "description": name, "input_schema": {"type": "object"},
         "effect": "read", "method": "POST", "path": "/api/tool-gateway"}
        for name in ("discovery_brief", "build_context", "search_context")
    ]
    replies = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "search-1", "type": "function",
            "function": {"name": "search_context", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "Evidence is still incomplete."},
        {"role": "assistant", "content": "Let me try a different approach:"},
        {"role": "assistant", "content": "Final cycle report: no supported defect was found; the next question is narrower."},
    ])
    fallback = []

    monkeypatch.setattr(discovery_worker, "RESOURCE_PAUSE_LEVEL", "off")
    monkeypatch.setattr(discovery_worker, "MAX_TOOL_ITERS", 6)
    monkeypatch.setattr(discovery_worker.platform_client, "tools", lambda: catalog)
    monkeypatch.setattr(discovery_worker.platform_client, "invoke",
                        lambda name, args, confirmed=False: {"ok": True, "result": {"name": name}})

    def chat(messages, tools, **kwargs):
        fallback.append(bool(kwargs.get("prefer_fallback")))
        return next(replies)

    monkeypatch.setattr(discovery_worker.platform_client, "chat", chat)
    result = discovery_worker.run_cycle("inspect local evidence")

    assert result.startswith("Final cycle report:")
    assert fallback == [False, False, False, True]
