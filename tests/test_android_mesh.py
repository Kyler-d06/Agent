import json
import time

import pytest

import mobile_runtime
import node_agent


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr(node_agent, "NODE_KEY", "test-node")
    monkeypatch.setattr(node_agent, "ROOTS", {"scripts": str(tmp_path)})
    monkeypatch.setattr(node_agent, "jobs", {})
    monkeypatch.setattr(node_agent, "MAX_JOBS", 1)
    monkeypatch.setattr(node_agent, "mobile_status", lambda: {"is_android": False, "available": True, "reasons": []})
    node_agent.app.config["TESTING"] = True
    return node_agent.app.test_client(), {"X-Node-Key": "test-node"}


@pytest.mark.parametrize("battery,allowed", [
    ({"percentage": 80, "plugged": "PLUGGED_USB", "temperature": 30}, True),
    ({"percentage": 10, "plugged": "PLUGGED_USB", "temperature": 30}, False),
    ({"percentage": 80, "plugged": "UNPLUGGED", "temperature": 30}, False),
    ({"percentage": 80, "plugged": "PLUGGED_AC", "temperature": 41}, False),
    ({}, False),
])
def test_android_power_admission(monkeypatch, battery, allowed):
    monkeypatch.setenv("NODE_DEVICE_TYPE", "android")
    monkeypatch.setenv("NODE_REQUIRE_CHARGING", "1")
    monkeypatch.setenv("NODE_MIN_BATTERY", "25")
    monkeypatch.setenv("NODE_MAX_BATTERY_TEMP_C", "40")
    monkeypatch.setattr(mobile_runtime, "battery_status", lambda: battery)
    assert mobile_runtime.mobile_status()["available"] is allowed


def script(tmp_path, content):
    (tmp_path / "work.py").write_text(content)
    (tmp_path / "work.py.agent.json").write_text(json.dumps({"agent_permission": "autonomous", "weight": "light"}))


def test_node_timeout_capacity_and_bounded_output(node, tmp_path, monkeypatch):
    client, headers = node
    # Admission behavior is covered separately. This timeout test must not
    # become flaky when the developer machine happens to be low on RAM.
    monkeypatch.setattr(node_agent, "_profile", lambda: {
        "ram_available_gb": 4, "gpu": {"available": False}, "tier": "standard",
        "mobile": {"is_android": False, "available": True, "reasons": []},
        "capacity": node_agent._capacity(),
    })
    monkeypatch.setattr(node_agent, "MAX_JOB_SECONDS", 1)
    monkeypatch.setattr(node_agent, "MAX_OUTPUT_CHARS", 100)
    script(tmp_path, "import time\nprint('x' * 50000, flush=True)\ntime.sleep(20)\n")
    result = client.post("/run", headers=headers, json={"root": "scripts", "path": "work.py"})
    assert result.status_code == 200, result.get_json()
    jid = result.get_json()["job"]["id"]
    assert client.post("/run", headers=headers, json={"root": "scripts", "path": "work.py"}).status_code == 409
    deadline = time.monotonic() + 12
    while node_agent.jobs[jid]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    result = client.get("/job", headers=headers, query_string={"id": jid}).get_json()["job"]
    assert result["status"] == "failed" and result["error"] == "job timeout"
    assert len(result["stdout"]) <= 100


def test_owner_cannot_bypass_phone_power_policy(node, tmp_path, monkeypatch):
    client, headers = node
    script(tmp_path, "print('work')")
    monkeypatch.setattr(node_agent, "mobile_status", lambda: {"is_android": True, "available": False, "reasons": ["battery too warm"]})
    result = client.post("/run", headers=headers, json={"root": "scripts", "path": "work.py", "owner": True})
    assert result.status_code == 409 and not node_agent.jobs


def test_mesh_selects_ready_phone_skips_busy_desktop(core, owner, monkeypatch):
    with core.app.test_client() as client:
        for name in ("phone", "desktop"):
            assert client.post("/api/nodes/add", headers=owner, json={"name": name, "status_url": "http://" + name}).status_code == 200
        def call(base, path, **kwargs):
            if path == "/manifest":
                return {"ok": True, "roots": ["scripts"], "node": {"accepting_jobs": base.endswith("phone"), "cpu_pct": 5}}
            return {"ok": True, "analysis": {"compatible": True, "permission": "autonomous"}}
        monkeypatch.setattr(core.mesh_platform, "_call", call)
        result = client.get("/api/mesh/select", headers=owner, query_string={"root": "scripts", "path": "work.py"}).get_json()
        assert result["result"]["selected"] == "phone"
