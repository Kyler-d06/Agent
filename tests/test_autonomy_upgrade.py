import uuid
import importlib.util
from pathlib import Path

from durable_agent import run
from improvement_engine import ImprovementEngine
from platform_contracts import canonical
from runtime_store import RuntimeStore
from test_durable_work import InProcessClient, call
from test_platform import actor, invoke, queue


def load_worker():
    path = Path(__file__).resolve().parents[1] / "assistant_worker.py"
    spec = importlib.util.spec_from_file_location("autonomy_upgrade_worker", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_patch_scan_and_isolated_copy_leave_original_unchanged(core):
    work = core.universal_platform.work
    source = work.workspace_write("sample.py", "# TODO tighten this\ndef collect(items=[]):\n    return items\n")
    scan = work.source_bug_scan()
    kinds = {finding["kind"] for finding in scan["findings"]}
    assert {"mutable_default", "unfinished_marker"} <= kinds

    copied = work.source_copy_create(label="bug-fix")
    copy_file = copied["path"] + "/sample.py"
    before = work.workspace_read(copy_file)
    patched = work.workspace_patch(copy_file, "def collect(items=[]):", "def collect(items=None):", before["sha256"])
    assert patched["sha256"] != source["sha256"]
    assert work.workspace_read("sample.py")["content"] == "# TODO tighten this\ndef collect(items=[]):\n    return items\n"
    diff = work.workspace_diff(copied["path"])
    assert diff["ok"] and "def collect(items=None):" in diff["patch"]
    assert copied["original_modified"] is False


def test_bug_scan_only_treats_comments_as_unfinished_markers(core):
    work = core.universal_platform.work
    work.workspace_write("markers.py", 'pattern = r"TODO|FIXME|XXX"\nexample = "# TODO fixture"\n# TODO real work\n')
    findings = [f for f in work.source_bug_scan()["findings"] if f["kind"] == "unfinished_marker"]
    assert len(findings) == 1
    assert findings[0]["path"] == "markers.py" and findings[0]["line"] == 3


def test_self_improvement_finishes_safely_without_a_reproducible_candidate(core, monkeypatch):
    core.universal_platform.work.workspace_write("clean.py", "value = 1\n")
    queue(core, "repair only a reproducible finding", {
        "self_improvement": True, "self_improvement_source": "", "approval_mode": "auto_edit",
        "template": "coding", "max_steps": 40,
    })
    job = core.universal_platform.store.claim("clean-repair")
    worker = load_worker()
    worker.client = InProcessClient(core, job, [])
    result = worker.run_self_improvement(job)
    assert result["ok"] and result["no_change"]
    assert "no reproducible candidate" in result["answer"].lower()
    assert worker.client.model_calls == 0


def test_self_improvement_job_binds_auto_edits_to_created_copy(core):
    work = core.universal_platform.work
    work.workspace_write("source.py", "value = 1\n")
    queue(core, "repair a copy", {"self_improvement": True, "self_improvement_source": "",
                                   "approval_mode": "auto_edit", "template": "coding"})
    job = core.universal_platform.store.claim("self-improvement-test")
    u = core.universal_platform
    created = u.invoke("assistant", "source_copy_create", {"path": "", "label": "bound"},
                       job["id"] + ":agent:copy", job["id"], job["lease_token"])
    assert created["ok"], created
    prefix = created["result"]["path"]
    allowed_path = prefix + "/source.py"
    before = work.workspace_read(allowed_path)
    allowed = u.invoke("assistant", "workspace_patch",
                       {"path": allowed_path, "old_str": "value = 1", "new_str": "value = 2",
                        "expected_sha256": before["sha256"]},
                       job["id"] + ":agent:patch", job["id"], job["lease_token"])
    assert allowed["ok"], allowed
    denied = u.invoke("assistant", "workspace_patch",
                      {"path": "source.py", "old_str": "value = 1", "new_str": "value = 9"},
                      job["id"] + ":agent:escape", job["id"], job["lease_token"])
    assert denied["error"]["code"] == "approval_required"
    assert work.workspace_read("source.py")["content"] == "value = 1\n"


def test_self_improvement_can_run_only_copy_bound_sandbox_tests_unattended(core, monkeypatch):
    work = core.universal_platform.work
    work.workspace_write("test_source.py", "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n")
    queue(core, "repair and test a copy", {"self_improvement": True, "self_improvement_source": "",
                                            "approval_mode": "auto_edit", "template": "coding"})
    job = core.universal_platform.store.claim("self-improvement-test-runner")
    u = core.universal_platform
    created = u.invoke("assistant", "source_copy_create", {"path": "", "label": "testable"},
                       job["id"] + ":copy", job["id"], job["lease_token"])
    prefix = created["result"]["path"]
    monkeypatch.setattr("work_tools.run_container", lambda *_args, **_kwargs: {
        "ok": True, "returncode": 0, "stdout": "", "stderr": "Ran 1 test in 0.01s\nOK\n"
    })
    allowed = u.invoke("assistant", "workspace_test", {"path": prefix, "runner": "unittest"},
                       job["id"] + ":test-copy", job["id"], job["lease_token"])
    assert allowed["ok"] and allowed["result"]["verified"]
    denied = u.invoke("assistant", "workspace_test", {"path": "", "runner": "unittest"},
                      job["id"] + ":test-original", job["id"], job["lease_token"])
    assert denied["error"]["code"] == "approval_required"


def test_role_catalog_goal_planner_and_self_improvement_queue_are_bounded(core, owner):
    with core.app.test_client() as client:
        roles = invoke(core, "list_agent_roles")["result"]
        assert {"router", "researcher", "coder", "reviewer"} <= {role["name"] for role in roles}
        updated = client.post("/api/owner/agent-roles", headers=owner, json={
            "name": "maintainer", "system_prompt": "Maintain local code.",
            "allowed_tools": ["workspace_read", "workspace_patch"],
        })
        assert updated.status_code == 200
        goal_job = invoke(core, "queue_goal_planning", {"focus": "launch readiness"})["result"]
        goal_job_again = invoke(core, "queue_goal_planning")["result"]
        assert goal_job_again["job_id"] == goal_job["job_id"] and goal_job_again["coalesced"]

        # Stop the planner so a separate improvement job can be inspected.
        core.universal_platform.store.control(goal_job["job_id"], "cancel")
        core.universal_platform.work.workspace_write("launch_check.py", "ready = True\n")
        improvement = invoke(core, "queue_self_improvement", {"source_path": "", "focus": "static defects"})["result"]
        assert improvement["safety"].startswith("original source read-only")
        with core.universal_platform.store.connect() as db:
            payload = db.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (improvement["job_id"],)).fetchone()[0]
        assert '"self_improvement": true' in payload and '"approval_mode": "auto_edit"' in payload


def test_owner_can_explicitly_authorize_browser_fallback_for_one_research_cycle(core, owner):
    with core.app.test_client() as client:
        response = client.post("/api/jobs/research-cycle", headers=owner, json={
            "focus": "local launch readiness", "browser_escalation": True,
            "auto_repair_on_failure": True,
        })
        assert response.status_code == 200
        job_id = response.get_json()["result"]["job_id"]
    with core.universal_platform.store.connect() as db:
        payload = db.execute("SELECT payload_json FROM agent_jobs WHERE id=?", (job_id,)).fetchone()[0]
        event = db.execute("SELECT data_json FROM platform_events WHERE kind='provider.escalation.approved' AND job_id=?", (job_id,)).fetchone()
    assert '"_browser_escalation_authorized": true' in payload
    assert '"auto_repair_on_failure": true' in payload
    assert event is not None


def test_approval_modes_and_operator_context_are_enforced(core, owner):
    u = core.universal_platform
    queue(core, "suggest change", {"approval_mode": "suggest"})
    suggest = u.store.claim("suggest-worker")
    denied = u.invoke("assistant", "workspace_write", {"path": "suggest.txt", "content": "x"},
                      suggest["id"] + ":write", suggest["id"], suggest["lease_token"])
    assert denied["error"]["code"] == "approval_required"
    u.store.complete(suggest["id"], suggest["lease_token"], "blocked", denied)

    queue(core, "auto edit", {"approval_mode": "auto_edit"})
    auto = u.store.claim("auto-worker")
    allowed = u.invoke("assistant", "workspace_write", {"path": "auto.txt", "content": "x"},
                       auto["id"] + ":write", auto["id"], auto["lease_token"])
    assert allowed["ok"]

    with core.app.test_client() as client:
        response = client.post("/api/owner/operator-context", headers=owner,
                               json={"content": "# Priorities\n\nFinish launch checks.\n"})
        assert response.status_code == 200
        state = client.get("/api/state", headers=owner).get_json()
        assert "Finish launch checks" in state["configuration"]["operator_context"]


def test_research_bootstraps_tools_and_stops_repeated_capability_denial(core):
    core.universal_platform.work.workspace_write("local_question.py", "value = 1\n")
    queue(core, "Research the highest-value local question", {
        "research_cycle": True, "template": "research", "approval_mode": "full_auto",
        "max_steps": 30, "auto_repair_on_failure": False,
    })
    job = core.universal_platform.store.claim("stuck-research")

    class TrackingClient(InProcessClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fallback_preferences = []

        def chat(self, *args, **kwargs):
            self.fallback_preferences.append(bool(kwargs.get("prefer_fallback")))
            return super().chat(*args, **kwargs)

    denial = {"role": "assistant", "content":
              "I cannot call tools or access your local file system, so here is a manual checklist."}
    client = TrackingClient(core, job, [denial, denial])
    result = run(client, job["objective"])

    assert result["failure_code"] == "model_stuck_repeating"
    assert client.model_calls == 2 and client.fallback_preferences == [False, True]
    assert {"discovery_brief", "build_context", "list_goals"} <= {
        action["tool"] for action in result["actions"]
    }
    assert result["diagnostics"]["recovery_attempts"] == 1
    with core.universal_platform.store.connect() as db:
        kinds = {row[0] for row in db.execute(
            "SELECT kind FROM platform_events WHERE job_id=?", (job["id"],)
        )}
    assert {"agent.recovery_requested", "agent.non_progress"} <= kinds


def test_failed_research_queues_isolated_repair_only_when_permission_mode_allows(core):
    core.universal_platform.work.workspace_write("repair_target.py", "value = 1\n")
    worker = load_worker()
    denial = {"role": "assistant", "content": "I cannot use tools or access the local file system."}

    queue(core, "Research and repair failures", {
        "research_cycle": True, "template": "research", "approval_mode": "full_auto",
        "max_steps": 30, "auto_repair_on_failure": True,
    })
    allowed_job = core.universal_platform.store.claim("auto-repair-allowed")
    worker.client = InProcessClient(core, allowed_job, [denial, denial])
    status, result = worker.process_job(allowed_job)
    assert status == "blocked" and result["automatic_recovery"]["queued"]
    repair_id = result["automatic_recovery"]["result"]["job_id"]
    with core.universal_platform.store.connect() as db:
        repair = db.execute("SELECT status,payload_json FROM agent_jobs WHERE id=?", (repair_id,)).fetchone()
    assert repair["status"] == "queued"
    repair_payload = canonical({}) if not repair else repair["payload_json"]
    assert '"self_improvement": true' in repair_payload
    assert allowed_job["id"] in repair_payload

    # Cancel the queued repair so it cannot be coalesced with the permission test.
    core.universal_platform.store.control(repair_id, "cancel")
    queue(core, "Research but ask before side effects", {
        "research_cycle": True, "template": "research", "approval_mode": "suggest",
        "max_steps": 30, "auto_repair_on_failure": True,
    })
    suggest_job = core.universal_platform.store.claim("auto-repair-suggest")
    worker.client = InProcessClient(core, suggest_job, [denial, denial])
    status, result = worker.process_job(suggest_job)
    assert status == "blocked" and not result["automatic_recovery"]["queued"]
    assert result["automatic_recovery"]["error"]["code"] == "approval_required"


def test_self_improvement_confinement_does_not_override_suggest_mode(core):
    core.universal_platform.work.workspace_write("permission_target.py", "value = 1\n")
    queue(core, "propose an isolated repair", {
        "self_improvement": True, "self_improvement_source": "",
        "approval_mode": "suggest", "template": "coding",
    })
    job = core.universal_platform.store.claim("suggest-copy")
    result = core.universal_platform.invoke(
        "assistant", "source_copy_create", {"path": "", "label": "permission-check"},
        job["id"] + ":copy", job["id"], job["lease_token"],
    )
    assert result["error"]["code"] == "approval_required"


def test_bug_scan_can_inspect_an_explicit_isolated_copy(core):
    work = core.universal_platform.work
    work.workspace_write("buggy.py", "def collect(items=[]):\n    return items\n")
    copied = work.source_copy_create(label="scan-copy")
    result = work.source_bug_scan(copied["path"])
    assert result["files_scanned"] > 0
    assert any(finding["kind"] == "mutable_default" and finding["path"].endswith("buggy.py")
               for finding in result["findings"])


def test_context_broker_excludes_dependencies_and_old_repair_copies(core):
    research = Path(core.RESEARCH_REPO)
    (research / ".venv" / "Lib").mkdir(parents=True)
    (research / "self_improvement_copies" / "old").mkdir(parents=True)
    (research / "src").mkdir(parents=True)
    marker = "overnight_context_marker"
    (research / ".venv" / "Lib" / "dependency.py").write_text(marker, encoding="utf-8")
    (research / "self_improvement_copies" / "old" / "copy.py").write_text(marker, encoding="utf-8")
    (research / "src" / "live.py").write_text(marker, encoding="utf-8")

    packet = core.agent_platform.build_context_packet(marker, sources=["research_repo"])
    paths = {item["path"].replace("\\", "/") for item in packet["selected"]}
    assert "src/live.py" in paths
    assert not any(path.startswith(".venv/") or path.startswith("self_improvement_copies/") for path in paths)

    with core.app.test_client() as client:
        searched = client.get("/api/context/search", headers={"X-API-Key": "test-owner-key"},
                              query_string={"q": marker, "source": "research_repo"}).get_json()["result"]
    paths = {item["path"].replace("\\", "/") for item in searched["research_repo"]}
    assert paths == {"src/live.py"}


def test_critic_is_skipped_until_deterministic_checks_pass(core):
    queue(core, "repair safely", {"template": "coding", "critic_review": True})
    job = core.universal_platform.store.claim("critic-skip")

    class NoCriticClient(InProcessClient):
        def chat(self, *args, **kwargs):
            raise AssertionError("critic must not run while deterministic checks fail")

    state = {"turns": 1, "plan": [], "actions": [],
             "messages": [{"role": "system", "content": "test"},
                          {"role": "user", "content": job["objective"]}]}
    from durable_agent import verify
    result = verify(NoCriticClient(core, job, []), job["payload"], state, "agent")
    critic = next(check for check in result["checks"] if check["name"] == "critic_review")
    assert not critic["passed"] and "skipped" in critic["feedback"]


def test_blocking_pre_hook_is_logged_and_prevents_tool(core):
    hooks = core.universal_platform.hooks_file
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text('{"get_current_datetime":{"pre":{"argv":["missing-hook-executable"],"blocking":true}}}')
    result = core.universal_platform.invoke("assistant", "get_current_datetime", {}, uuid.uuid4().hex)
    assert not result["ok"] and result["error"]["code"] == "hook_blocked"
    with core.universal_platform.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM platform_events WHERE kind='tool.hook'").fetchone()[0] == 1


def test_structured_plan_and_named_handoff_survive_checkpoint(core):
    queue(core, "Maintain the local source file")
    job = core.universal_platform.store.claim("role-test")
    replies = [
        call("update_plan", {"steps": [{"step": "inspect", "status": "active"},
                                        {"step": "finish", "status": "pending"}]}, "plan-1"),
        call("handoff_to", {"role_name": "coder", "context_note": "Implement the bounded maintenance task."}, "handoff-1"),
        call("update_plan", {"steps": [{"step": "inspect", "status": "done"},
                                        {"step": "finish", "status": "done"}]}, "plan-2"),
        {"role": "assistant", "content": "The bounded task is complete."},
    ]
    client = InProcessClient(core, job, replies)
    result = run(client, job["objective"])
    assert result["ok"] and all(step["status"] == "done" for step in result["plan"])
    state = client.checkpoint()["agent"]
    assert state["role_history"][-1] == "coder"
    assert [a["tool"] for a in result["actions"]] == ["update_plan", "handoff_to", "update_plan"]


def test_population_evolution_keeps_candidates_and_does_not_auto_promote(tmp_path, monkeypatch):
    class Models:
        calls = 0

        def chat(self, *args, **kwargs):
            self.calls += 1
            return {"content": canonical({"text": f"candidate {self.calls}", "task_types": ["test"]})}

    store = RuntimeStore(tmp_path / "runtime.db")
    engine = ImprovementEngine(store, tmp_path, Models())
    engine.suite("quality", [{"input": "x", "expected": "x"}], min_score=0.5)

    def evaluate(version_id, suite):
        version = engine.version(version_id)
        score = int(version["content"]["text"].split()[-1]) / 10
        return {"id": uuid.uuid4().hex, "cases": [], "elapsed_seconds": 0, "score": score, "passed": score >= 0.5}

    monkeypatch.setattr(engine, "evaluate", evaluate)
    result = engine.evolve("planner", "prompt", "quality", population=3, generations=2)
    assert result["winner"] and result["score"] == 0.6 and not result["promoted"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM improvement_versions WHERE name='planner'").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM improvement_versions WHERE status='active'").fetchone()[0] == 0
