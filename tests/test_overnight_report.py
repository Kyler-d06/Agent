from overnight_report_worker import build_report


def test_report_explains_failures_and_free_web_readiness():
    platform = {"maintenance": {
        "services": [{"name": "core", "status": "running", "restarts": 0}],
        "readiness": {"overnight_mode": True, "ollama_api_ready": True,
                      "docker_daemon_ready": True, "dsh_configured": True,
                      "free_web_retrieval_ready": True, "web_search_backend": "ddg"},
    }}
    jobs = [{"id": "JOB-test", "status": "blocked", "objective": "Use DSH",
             "updated_at": 1, "result": {"error": "dirty checkout"}}]

    report = build_report(platform, jobs, "discovery completed")

    assert "Overall: RUNNING" in report
    assert "JOB-test — BLOCKED" in report and "dirty checkout" in report
    assert "Free web retrieval: ready" in report
    assert "Latest discovery output" in report


def test_report_does_not_present_stale_failure_for_running_retry():
    platform = {"maintenance": {"services": [{"name": "core", "status": "running"}], "readiness": {}}}
    report = build_report(platform, [{"id": "JOB-retry", "status": "running",
                                      "result": {"error": "old failure"}}])
    assert "In progress" in report and "old failure" not in report
