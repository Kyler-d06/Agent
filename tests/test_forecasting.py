import math

import pytest


def post(client, path, owner, value):
    return client.post(path, headers=owner, json=value)


def create_entity(client, owner, name, entity_type="system"):
    response = post(client, "/api/ontology/entities", owner, {
        "entity_type": entity_type,
        "name": name,
        "description": "A measured system",
        "confidence": 0.8,
        "source_refs": [{"kind": "user_context", "id": "test-fixture"}],
    })
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]["id"]


def test_ontology_graph_supplies_forecast_context(core, owner):
    with core.app.test_client() as client:
        system = create_entity(client, owner, "Battery prototype", "technology")
        material = create_entity(client, owner, "Electrolyte A", "material")
        relation = post(client, "/api/ontology/relations", owner, {
            "subject_id": system,
            "predicate": "uses_material",
            "object_id": material,
            "confidence": 0.7,
            "source_refs": [{"kind": "user_context", "id": "lab-notebook"}],
        })
        assert relation.status_code == 200
        context = client.get("/api/ontology/context", headers=owner,
                             query_string={"entity_id": system, "depth": 1}).get_json()["result"]
        assert {entity["id"] for entity in context["entities"]} == {system, material}
        assert context["relations"][0]["predicate"] == "uses_material"


def test_forecast_requires_context_and_scores_immutable_revisions(core, owner):
    with core.app.test_client() as client:
        target = create_entity(client, owner, "Pilot process", "experiment")
        base = {
            "question": "Will the pilot exceed 80 percent yield by 2030-01-01?",
            "domain": "engineering",
            "target_entity_id": target,
            "due_at": "2030-01-01T00:00:00Z",
            "resolution_criterion": "The preregistered pilot report records yield greater than 80 percent.",
            "outcomes": [{"label": "yes", "probability": 0.6}, {"label": "no", "probability": 0.4}],
            "context_refs": [{"kind": "ontology_entity", "id": target}],
            "base_rate": 0.45,
            "method": "base-rate plus evidence adjustment",
            "rationale": "Comparable pilot processes and the measured prototype establish a bounded estimate.",
            "assumptions": ["Measurement protocol remains unchanged"],
            "unknowns": ["Scale-up variance"],
        }
        missing = post(client, "/api/forecasts", owner, {**base, "context_refs": []})
        assert missing.status_code == 400
        bad_sum = post(client, "/api/forecasts", owner, {**base, "outcomes": [
            {"label": "yes", "probability": 0.8}, {"label": "no", "probability": 0.4}]})
        assert bad_sum.status_code == 400

        created = post(client, "/api/forecasts", owner, base).get_json()["result"]
        forecast_id = created["id"]
        revised = post(client, "/api/forecasts/revise", owner, {
            "forecast_id": forecast_id,
            "outcomes": [{"label": "yes", "probability": 0.7}, {"label": "no", "probability": 0.3}],
            "context_refs": [{"kind": "ontology_entity", "id": target}],
            "rationale": "A replicated bench run raised the conditional success estimate.",
            "method": "Bayesian evidence update",
            "assumptions": [],
            "unknowns": ["Production-line variance"],
        }).get_json()["result"]
        assert [revision["revision"] for revision in revised["revisions"]] == [1, 2]
        assert revised["revisions"][0]["outcomes"][0]["probability"] == 0.6

        resolved = post(client, "/api/forecasts/resolve", owner, {
            "forecast_id": forecast_id,
            "outcome": "yes",
            "source_refs": [{"kind": "user_context", "id": "preregistered-result"}],
            "notes": "Final pilot report reviewed.",
        }).get_json()["result"]
        assert resolved["brier_score"] == pytest.approx(0.18)
        assert resolved["log_score"] == pytest.approx(-math.log(0.7))
        calibration = client.get("/api/forecasts/calibration", headers=owner).get_json()["result"]
        assert calibration["resolved"] == 1
        assert calibration["top_choice_accuracy"] == 1


def test_supported_impact_project_queues_and_requires_verified_deliverable(core, owner, tmp_path):
    with core.app.test_client() as client:
        target = create_entity(client, owner, "Control algorithm", "software")
        forecast = post(client, "/api/forecasts", owner, {
            "question": "Will the controller reduce error in the held-out simulation?",
            "domain": "engineering",
            "target_entity_id": target,
            "due_at": "2030-06-01T00:00:00Z",
            "resolution_criterion": "Held-out mean absolute error is below the registered baseline.",
            "outcomes": [{"label": "yes", "probability": 0.65}, {"label": "no", "probability": 0.35}],
            "context_refs": [{"kind": "ontology_entity", "id": target}],
            "rationale": "The estimate uses the prototype measurements and an explicit baseline.",
        }).get_json()["result"]
        proposal = post(client, "/api/impact/projects", owner, {
            "title": "Build controller prototype",
            "objective": "Implement and test a controller against the held-out simulation baseline.",
            "domain": "engineering",
            "source_refs": [{"kind": "forecast", "id": forecast["id"]}],
            "probability_success": 0.65,
            "impact_magnitude": 0.8,
            "effort": 0.4,
            "safety_risk": 0.1,
            "deliverable_kind": "software",
            "acceptance_criteria": ["Tests run against the held-out baseline", "Artifact hash verifies"],
        }).get_json()["result"]
        queued = post(client, "/api/impact/projects/queue", owner, {
            "project_id": proposal["id"], "template": "impact", "priority": 0.9,
        })
        assert queued.status_code == 200, queued.get_json()
        job_id = queued.get_json()["result"]["job_id"]
        row = core.get_db().execute("SELECT priority,payload_json FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        assert row["priority"] == 0.9 and '"template": "impact"' in row["payload_json"]

        incomplete = post(client, "/api/impact/projects/outcome", owner, {
            "project_id": proposal["id"], "status": "completed", "summary": "Done",
        })
        assert incomplete.status_code == 400
        artifact_path = tmp_path / "controller.txt"
        artifact_path.write_text("verified controller result", encoding="utf-8")
        artifact = core.universal_platform.work.register_artifact("controller.txt", "software", job_id)
        completed = post(client, "/api/impact/projects/outcome", owner, {
            "project_id": proposal["id"], "status": "completed", "summary": "Held-out test passed.",
            "artifact_id": artifact["id"], "actual_impact": 0.75,
            "source_refs": [{"kind": "artifact", "id": artifact["id"]}],
        })
        assert completed.status_code == 200, completed.get_json()


def test_impact_queue_is_confirmation_gated(core):
    tool = next(item for item in core.universal_platform.catalog() if item["name"] == "queue_impact_project")
    assert tool["effect"] == "execute" and tool["requires_confirmation"] is True
