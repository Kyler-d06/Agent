"""Ontology-backed probabilistic forecasting and discovery-to-impact delivery."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from platform_contracts import object_schema, tool


TEXT = {"type": "string"}
REF = object_schema({"kind": TEXT, "id": TEXT, "url": TEXT}, ["kind"])
OUTCOME = object_schema({"label": TEXT, "probability": {"type": "number", "minimum": 0, "maximum": 1}},
                        ["label", "probability"])

FORECAST_TOOLS = [
    tool("upsert_ontology_entity", "Create or update a typed entity in the sourced ontology graph.",
         {"entity_type": TEXT, "name": TEXT, "description": TEXT, "aliases": {"type": "array", "items": TEXT},
          "attributes": {"type": "object"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "source_refs": {"type": "array", "minItems": 1, "items": REF}},
         ["entity_type", "name", "source_refs"], effect="write"),
    tool("relate_ontology_entities", "Add a sourced directed relationship between two ontology entities.",
         {"subject_id": TEXT, "predicate": TEXT, "object_id": TEXT, "attributes": {"type": "object"},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "source_refs": {"type": "array", "minItems": 1, "items": REF}},
         ["subject_id", "predicate", "object_id", "source_refs"], effect="write"),
    tool("ontology_context", "Read an entity, its graph neighborhood, and forecasts attached to it.",
         {"entity_id": TEXT, "depth": {"type": "integer", "minimum": 1, "maximum": 3}}, ["entity_id"]),
    tool("create_forecast", "Record a resolvable probabilistic forecast grounded in explicit context. Outcomes must be mutually exclusive and sum to one.",
         {"question": TEXT, "domain": TEXT, "target_entity_id": TEXT, "due_at": TEXT,
          "resolution_criterion": TEXT, "outcomes": {"type": "array", "minItems": 2, "maxItems": 20, "items": OUTCOME},
          "context_refs": {"type": "array", "minItems": 1, "items": REF}, "base_rate": {"type": "number", "minimum": 0, "maximum": 1},
          "method": TEXT, "rationale": TEXT, "assumptions": {"type": "array", "items": TEXT},
          "unknowns": {"type": "array", "items": TEXT}},
         ["question", "due_at", "resolution_criterion", "outcomes", "context_refs", "rationale"], effect="write"),
    tool("revise_forecast", "Append an immutable forecast revision when evidence changes; never overwrite the prior probability.",
         {"forecast_id": TEXT, "outcomes": {"type": "array", "minItems": 2, "maxItems": 20, "items": OUTCOME},
          "context_refs": {"type": "array", "minItems": 1, "items": REF}, "method": TEXT, "rationale": TEXT,
          "assumptions": {"type": "array", "items": TEXT}, "unknowns": {"type": "array", "items": TEXT}},
         ["forecast_id", "outcomes", "context_refs", "rationale"], effect="write"),
    tool("list_forecasts", "List open or resolved forecasts with their latest probability revision.",
         {"status": {"type": "string", "enum": ["open", "resolved", "void"]}, "domain": TEXT,
          "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
    tool("resolve_forecast", "Resolve a forecast against sourced real-world evidence and calculate Brier and logarithmic scores.",
         {"forecast_id": TEXT, "outcome": TEXT, "source_refs": {"type": "array", "minItems": 1, "items": REF},
          "notes": TEXT}, ["forecast_id", "outcome", "source_refs"], effect="write"),
    tool("forecast_calibration", "Measure forecast accuracy and calibration from resolved forecasts.",
         {"domain": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 5000}}),
    tool("propose_impact_project", "Convert supported research into a scored real-world deliverable proposal.",
         {"title": TEXT, "objective": TEXT, "domain": TEXT, "source_refs": {"type": "array", "minItems": 1, "items": REF},
          "probability_success": {"type": "number", "minimum": 0, "maximum": 1},
          "impact_magnitude": {"type": "number", "minimum": 0, "maximum": 1},
          "effort": {"type": "number", "minimum": 0, "maximum": 1},
          "safety_risk": {"type": "number", "minimum": 0, "maximum": 1},
          "deliverable_kind": {"type": "string", "enum": ["software", "experiment", "analysis", "design", "document", "prototype", "operation"]},
          "acceptance_criteria": {"type": "array", "minItems": 1, "items": TEXT}},
         ["title", "objective", "source_refs", "probability_success", "impact_magnitude", "effort",
          "safety_risk", "deliverable_kind", "acceptance_criteria"], effect="write"),
    tool("rank_impact_projects", "Rank proposed impact projects by expected impact, effort, and safety risk.",
         {"status": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
    tool("queue_impact_project", "Queue an owner-authorized impact project as a durable deliverable job.",
         {"project_id": TEXT, "template": {"type": "string", "enum": ["impact", "coding", "research", "office", "operations"]},
          "priority": {"type": "number", "minimum": 0, "maximum": 1}}, ["project_id", "template"], effect="execute"),
    tool("record_impact_outcome", "Record the verified artifact and observed impact of a completed or failed project.",
         {"project_id": TEXT, "status": {"type": "string", "enum": ["completed", "failed", "blocked", "cancelled"]},
          "artifact_id": TEXT, "summary": TEXT, "actual_impact": {"type": "number", "minimum": 0, "maximum": 1},
          "source_refs": {"type": "array", "items": REF}}, ["project_id", "status", "summary"], effect="write"),
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS ontology_entities (
 id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 aliases_json TEXT NOT NULL DEFAULT '[]', attributes_json TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL,
 source_refs_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(entity_type,name));
CREATE TABLE IF NOT EXISTS ontology_relations (
 id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, predicate TEXT NOT NULL, object_id TEXT NOT NULL,
 attributes_json TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL, source_refs_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(subject_id) REFERENCES ontology_entities(id), FOREIGN KEY(object_id) REFERENCES ontology_entities(id));
CREATE INDEX IF NOT EXISTS idx_ontology_subject ON ontology_relations(subject_id,predicate);
CREATE INDEX IF NOT EXISTS idx_ontology_object ON ontology_relations(object_id,predicate);
CREATE TABLE IF NOT EXISTS forecasts (
 id TEXT PRIMARY KEY, question TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '', target_entity_id TEXT,
 due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', resolution_criterion TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(target_entity_id) REFERENCES ontology_entities(id));
CREATE TABLE IF NOT EXISTS forecast_revisions (
 id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL, revision INTEGER NOT NULL, outcomes_json TEXT NOT NULL,
 context_refs_json TEXT NOT NULL, base_rate REAL, method TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL,
 assumptions_json TEXT NOT NULL DEFAULT '[]', unknowns_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 UNIQUE(forecast_id,revision), FOREIGN KEY(forecast_id) REFERENCES forecasts(id));
CREATE INDEX IF NOT EXISTS idx_forecasts_status_due ON forecasts(status,due_at);
CREATE TABLE IF NOT EXISTS forecast_resolutions (
 id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL UNIQUE, outcome TEXT NOT NULL, source_refs_json TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', brier_score REAL NOT NULL, log_score REAL NOT NULL, resolved_at TEXT NOT NULL,
 FOREIGN KEY(forecast_id) REFERENCES forecasts(id));
CREATE TABLE IF NOT EXISTS impact_projects (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',
 source_refs_json TEXT NOT NULL, probability_success REAL NOT NULL, impact_magnitude REAL NOT NULL,
 effort REAL NOT NULL, safety_risk REAL NOT NULL, score REAL NOT NULL, deliverable_kind TEXT NOT NULL,
 acceptance_criteria_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', job_id TEXT, artifact_id TEXT,
 outcome_summary TEXT, actual_impact REAL, outcome_refs_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_impact_status_score ON impact_projects(status,score DESC);
"""


TABLES_BY_REF = {
    "ontology_entity": ("ontology_entities", "id"), "world_item": ("world_items", "id"),
    "knowledge_item": ("knowledge_items", "id"), "hypothesis": ("hypotheses", "id"),
    "evidence": ("evidence", "id"), "discovery": ("discoveries", "id"),
    "experiment": ("experiments", "id"), "forecast": ("forecasts", "id"),
    "memory": ("memories", "id"), "artifact": ("artifacts", "id"),
}


def _json(value, fallback):
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _clamp(value, default=0.5):
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected a probability between 0 and 1") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("expected a probability between 0 and 1")
    return number


def _iso(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("due_at must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("due_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ForecastingPlatform:
    def __init__(self, *, app, db_path, get_db, ok, err, logged_tool, now_iso,
                 publish_event=None, queue_job=None, verify_artifact=None):
        self.get_db, self.ok, self.err, self.now_iso = get_db, ok, err, now_iso
        self.publish_event, self.queue_job, self.verify_artifact = publish_event, queue_job, verify_artifact
        with sqlite3.connect(db_path) as db:
            db.executescript(SCHEMA)
        self._register_routes(app, logged_tool)

    def _refs(self, refs, *, required=True):
        if not isinstance(refs, list) or (required and not refs) or len(refs) > 100:
            raise ValueError("one to 100 context/source references are required")
        normalized = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("references must be objects")
            kind = str(ref.get("kind") or "").strip()
            if kind == "url":
                url = str(ref.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    raise ValueError("URL references require HTTP(S)")
                normalized.append({"kind": kind, "url": url})
            elif kind == "user_context":
                ident = str(ref.get("id") or "").strip()
                if not ident:
                    raise ValueError("user_context references require an id")
                normalized.append({"kind": kind, "id": ident})
            elif kind in TABLES_BY_REF:
                ident = str(ref.get("id") or "").strip()
                table, column = TABLES_BY_REF[kind]
                if not ident or not self.get_db().execute(f"SELECT 1 FROM {table} WHERE {column}=?", (ident,)).fetchone():
                    raise ValueError(f"unknown {kind} reference: {ident}")
                normalized.append({"kind": kind, "id": ident})
            else:
                raise ValueError(f"unsupported reference kind: {kind}")
        # Canonical de-duplication keeps provenance compact without losing kinds.
        return [json.loads(item) for item in dict.fromkeys(json.dumps(r, sort_keys=True) for r in normalized)]

    @staticmethod
    def _outcomes(outcomes):
        if not isinstance(outcomes, list) or not 2 <= len(outcomes) <= 20:
            raise ValueError("forecasts require two to 20 mutually exclusive outcomes")
        result, labels = [], set()
        for item in outcomes:
            if not isinstance(item, dict):
                raise ValueError("each outcome must be an object")
            label = str(item.get("label") or "").strip()
            if not label or label.casefold() in labels:
                raise ValueError("outcome labels must be non-empty and unique")
            labels.add(label.casefold())
            result.append({"label": label, "probability": _clamp(item.get("probability"))})
        total = sum(item["probability"] for item in result)
        if abs(total - 1) > 0.001:
            raise ValueError(f"outcome probabilities must sum to 1 (received {total:.6f})")
        # Normalize harmless floating point drift while retaining the submitted ratios.
        return [{**item, "probability": item["probability"] / total} for item in result]

    def _forecast(self, forecast_id):
        row = self.get_db().execute("SELECT * FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
        if not row:
            raise ValueError("forecast not found")
        revisions = self.get_db().execute(
            "SELECT * FROM forecast_revisions WHERE forecast_id=? ORDER BY revision", (forecast_id,)).fetchall()
        resolution = self.get_db().execute("SELECT * FROM forecast_resolutions WHERE forecast_id=?", (forecast_id,)).fetchone()
        return {**dict(row), "revisions": [self._revision(r) for r in revisions],
                "resolution": dict(resolution) if resolution else None}

    @staticmethod
    def _revision(row):
        value = dict(row)
        for source, target, fallback in (("outcomes_json", "outcomes", []), ("context_refs_json", "context_refs", []),
                                          ("assumptions_json", "assumptions", []), ("unknowns_json", "unknowns", [])):
            value[target] = _json(value.pop(source), fallback)
        return value

    def _event(self, kind, payload):
        if self.publish_event:
            return self.publish_event(kind, tag=kind.split(".")[0], source={"type": "forecasting_platform"}, payload=payload)
        return None

    def _register_routes(self, app, logged_tool):
        from flask import request

        @app.route("/api/ontology/entities", methods=["POST"])
        @logged_tool("upsert_ontology_entity")
        def upsert_entity():
            d = request.get_json(force=True)
            typ = re.sub(r"[^a-z0-9_.-]+", "_", str(d.get("entity_type") or "").strip().lower()).strip("_")
            name = str(d.get("name") or "").strip()
            if not typ or not name:
                return self.err("entity_type and name required")
            try:
                refs = self._refs(d.get("source_refs"))
                confidence = _clamp(d.get("confidence"), 0.5)
            except ValueError as exc:
                return self.err(str(exc))
            row = self.get_db().execute("SELECT * FROM ontology_entities WHERE entity_type=? AND name=?", (typ, name)).fetchone()
            now = self.now_iso()
            if row:
                prior_refs = _json(row["source_refs_json"], [])
                refs = [json.loads(item) for item in dict.fromkeys(json.dumps(r, sort_keys=True) for r in prior_refs + refs)]
                self.get_db().execute(
                    "UPDATE ontology_entities SET description=?,aliases_json=?,attributes_json=?,confidence=?,source_refs_json=?,updated_at=? WHERE id=?",
                    (str(d.get("description") or row["description"]), json.dumps(d.get("aliases") or _json(row["aliases_json"], [])),
                     json.dumps(d.get("attributes") or _json(row["attributes_json"], {})), confidence, json.dumps(refs), now, row["id"]))
                entity_id, created = row["id"], False
            else:
                entity_id, created = "ONT-" + uuid.uuid4().hex, True
                self.get_db().execute(
                    "INSERT INTO ontology_entities VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (entity_id, typ, name, str(d.get("description") or ""), json.dumps(d.get("aliases") or []),
                     json.dumps(d.get("attributes") or {}), confidence, json.dumps(refs), now, now))
            self.get_db().commit()
            self._event("ontology.entity_updated", {"entity_id": entity_id, "entity_type": typ, "created": created})
            return self.ok({"id": entity_id, "created": created})

        @app.route("/api/ontology/relations", methods=["POST"])
        @logged_tool("relate_ontology_entities")
        def relate_entities():
            d = request.get_json(force=True)
            subject, obj = d.get("subject_id"), d.get("object_id")
            predicate = re.sub(r"[^a-z0-9_.-]+", "_", str(d.get("predicate") or "").strip().lower()).strip("_")
            db = self.get_db()
            if not subject or not obj or not predicate:
                return self.err("subject_id, predicate, and object_id required")
            if subject == obj:
                return self.err("self-relations are not allowed")
            if not db.execute("SELECT 1 FROM ontology_entities WHERE id=?", (subject,)).fetchone() or not db.execute("SELECT 1 FROM ontology_entities WHERE id=?", (obj,)).fetchone():
                return self.err("both ontology entities must exist", 404)
            try:
                refs, confidence = self._refs(d.get("source_refs")), _clamp(d.get("confidence"), 0.5)
            except ValueError as exc:
                return self.err(str(exc))
            rid, now = "REL-" + uuid.uuid4().hex, self.now_iso()
            db.execute("INSERT INTO ontology_relations VALUES(?,?,?,?,?,?,?,?,?)",
                       (rid, subject, predicate, obj, json.dumps(d.get("attributes") or {}), confidence, json.dumps(refs), now, now))
            db.commit()
            return self.ok({"id": rid})

        @app.route("/api/ontology/context")
        @logged_tool("ontology_context")
        def ontology_context():
            entity_id = request.args.get("entity_id", "")
            depth = max(1, min(int(request.args.get("depth", 1)), 3))
            db = self.get_db()
            entity = db.execute("SELECT * FROM ontology_entities WHERE id=?", (entity_id,)).fetchone()
            if not entity:
                return self.err("ontology entity not found", 404)
            seen, frontier, relations = {entity_id}, {entity_id}, []
            for _ in range(depth):
                if not frontier:
                    break
                marks = ",".join("?" for _ in frontier)
                rows = db.execute(f"SELECT * FROM ontology_relations WHERE subject_id IN ({marks}) OR object_id IN ({marks}) LIMIT 500", (*frontier, *frontier)).fetchall()
                next_frontier = set()
                for row in rows:
                    value = dict(row)
                    value["attributes"] = _json(value.pop("attributes_json"), {})
                    value["source_refs"] = _json(value.pop("source_refs_json"), [])
                    if value["id"] not in {r["id"] for r in relations}:
                        relations.append(value)
                    next_frontier.update((value["subject_id"], value["object_id"]))
                frontier = next_frontier - seen
                seen |= frontier
            marks = ",".join("?" for _ in seen)
            entities = []
            for row in db.execute(f"SELECT * FROM ontology_entities WHERE id IN ({marks})", tuple(seen)).fetchall():
                value = dict(row)
                value["aliases"] = _json(value.pop("aliases_json"), [])
                value["attributes"] = _json(value.pop("attributes_json"), {})
                value["source_refs"] = _json(value.pop("source_refs_json"), [])
                entities.append(value)
            forecasts = [dict(r) for r in db.execute(f"SELECT * FROM forecasts WHERE target_entity_id IN ({marks}) ORDER BY due_at", tuple(seen)).fetchall()]
            return self.ok({"root": entity_id, "entities": entities, "relations": relations, "forecasts": forecasts})

        @app.route("/api/forecasts", methods=["POST"])
        @logged_tool("create_forecast")
        def create_forecast():
            d = request.get_json(force=True)
            question = str(d.get("question") or "").strip()
            criterion = str(d.get("resolution_criterion") or "").strip()
            rationale = str(d.get("rationale") or "").strip()
            if not question or not criterion or not rationale:
                return self.err("question, resolution_criterion, and rationale required")
            target = d.get("target_entity_id") or None
            if target and not self.get_db().execute("SELECT 1 FROM ontology_entities WHERE id=?", (target,)).fetchone():
                return self.err("target ontology entity not found", 404)
            try:
                due, outcomes = _iso(d.get("due_at")), self._outcomes(d.get("outcomes"))
                refs = self._refs(d.get("context_refs"))
                base_rate = _clamp(d.get("base_rate")) if d.get("base_rate") is not None else None
            except ValueError as exc:
                return self.err(str(exc))
            fid, rid, now = "FC-" + uuid.uuid4().hex, "FCR-" + uuid.uuid4().hex, self.now_iso()
            db = self.get_db()
            db.execute("INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?)",
                       (fid, question, str(d.get("domain") or ""), target, due, "open", criterion, now, now))
            db.execute("INSERT INTO forecast_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, fid, 1, json.dumps(outcomes), json.dumps(refs), base_rate, str(d.get("method") or ""), rationale,
                        json.dumps(d.get("assumptions") or []), json.dumps(d.get("unknowns") or []), now))
            db.commit()
            self._event("forecast.created", {"forecast_id": fid, "due_at": due, "outcomes": outcomes})
            return self.ok(self._forecast(fid))

        @app.route("/api/forecasts/revise", methods=["POST"])
        @logged_tool("revise_forecast")
        def revise_forecast():
            d = request.get_json(force=True)
            fid = d.get("forecast_id")
            row = self.get_db().execute("SELECT status FROM forecasts WHERE id=?", (fid,)).fetchone()
            if not row:
                return self.err("forecast not found", 404)
            if row["status"] != "open":
                return self.err("only open forecasts can be revised")
            if not str(d.get("rationale") or "").strip():
                return self.err("revision rationale required")
            try:
                outcomes, refs = self._outcomes(d.get("outcomes")), self._refs(d.get("context_refs"))
            except ValueError as exc:
                return self.err(str(exc))
            db, now = self.get_db(), self.now_iso()
            number = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM forecast_revisions WHERE forecast_id=?", (fid,)).fetchone()[0]
            db.execute("INSERT INTO forecast_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("FCR-" + uuid.uuid4().hex, fid, number, json.dumps(outcomes), json.dumps(refs), None,
                        str(d.get("method") or ""), str(d["rationale"]).strip(), json.dumps(d.get("assumptions") or []),
                        json.dumps(d.get("unknowns") or []), now))
            db.execute("UPDATE forecasts SET updated_at=? WHERE id=?", (now, fid)); db.commit()
            self._event("forecast.revised", {"forecast_id": fid, "revision": number, "outcomes": outcomes})
            return self.ok(self._forecast(fid))

        @app.route("/api/forecasts")
        @logged_tool("list_forecasts")
        def list_forecasts():
            status, domain = request.args.get("status"), request.args.get("domain")
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
            clauses, args = [], []
            if status:
                clauses.append("f.status=?"); args.append(status)
            if domain:
                clauses.append("f.domain=?"); args.append(domain)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = self.get_db().execute(
                "SELECT f.*,r.outcomes_json,r.revision,r.created_at revision_at FROM forecasts f JOIN forecast_revisions r ON r.forecast_id=f.id AND r.revision=(SELECT MAX(r2.revision) FROM forecast_revisions r2 WHERE r2.forecast_id=f.id)" + where + " ORDER BY f.status,f.due_at LIMIT ?",
                (*args, limit)).fetchall()
            return self.ok([{**dict(r), "outcomes": _json(r["outcomes_json"], [])} for r in rows])

        @app.route("/api/forecasts/resolve", methods=["POST"])
        @logged_tool("resolve_forecast")
        def resolve_forecast():
            d, db = request.get_json(force=True), self.get_db()
            fid, outcome = d.get("forecast_id"), str(d.get("outcome") or "").strip()
            forecast = db.execute("SELECT * FROM forecasts WHERE id=?", (fid,)).fetchone()
            if not forecast:
                return self.err("forecast not found", 404)
            if forecast["status"] != "open":
                return self.err("forecast is not open")
            latest = db.execute("SELECT * FROM forecast_revisions WHERE forecast_id=? ORDER BY revision DESC LIMIT 1", (fid,)).fetchone()
            outcomes = _json(latest["outcomes_json"], [])
            labels = {item["label"] for item in outcomes}
            if outcome not in labels:
                return self.err("outcome must exactly match a forecast outcome label")
            try:
                refs = self._refs(d.get("source_refs"))
            except ValueError as exc:
                return self.err(str(exc))
            brier = sum((item["probability"] - (1.0 if item["label"] == outcome else 0.0)) ** 2 for item in outcomes)
            realized_probability = next(item["probability"] for item in outcomes if item["label"] == outcome)
            log_score = -math.log(max(realized_probability, 1e-15))
            now, resolution_id = self.now_iso(), "RES-" + uuid.uuid4().hex
            db.execute("INSERT INTO forecast_resolutions VALUES(?,?,?,?,?,?,?,?)",
                       (resolution_id, fid, outcome, json.dumps(refs), str(d.get("notes") or ""), brier, log_score, now))
            db.execute("UPDATE forecasts SET status='resolved',updated_at=? WHERE id=?", (now, fid)); db.commit()
            self._event("forecast.resolved", {"forecast_id": fid, "outcome": outcome, "brier_score": brier, "log_score": log_score})
            return self.ok({"forecast_id": fid, "outcome": outcome, "brier_score": brier, "log_score": log_score})

        @app.route("/api/forecasts/calibration")
        @logged_tool("forecast_calibration")
        def forecast_calibration():
            domain = request.args.get("domain")
            limit = max(1, min(int(request.args.get("limit", 1000)), 5000))
            where, args = (" AND f.domain=?", [domain]) if domain else ("", [])
            rows = self.get_db().execute(
                "SELECT f.id,f.domain,x.outcome,x.brier_score,x.log_score,r.outcomes_json FROM forecasts f JOIN forecast_resolutions x ON x.forecast_id=f.id JOIN forecast_revisions r ON r.forecast_id=f.id AND r.revision=(SELECT MAX(r2.revision) FROM forecast_revisions r2 WHERE r2.forecast_id=f.id) WHERE f.status='resolved'" + where + " ORDER BY x.resolved_at DESC LIMIT ?", (*args, limit)).fetchall()
            buckets = {}
            correct = 0
            for row in rows:
                outcomes = _json(row["outcomes_json"], [])
                top = max(outcomes, key=lambda item: item["probability"])
                hit = int(top["label"] == row["outcome"]); correct += hit
                key = min(9, int(top["probability"] * 10)) / 10
                bucket = buckets.setdefault(f"{key:.1f}-{key + 0.1:.1f}", {"count": 0, "mean_confidence": 0.0, "accuracy": 0.0})
                bucket["count"] += 1; bucket["mean_confidence"] += top["probability"]; bucket["accuracy"] += hit
            for bucket in buckets.values():
                bucket["mean_confidence"] /= bucket["count"]; bucket["accuracy"] /= bucket["count"]
            count = len(rows)
            return self.ok({"resolved": count, "top_choice_accuracy": correct / count if count else None,
                            "mean_brier_score": sum(r["brier_score"] for r in rows) / count if count else None,
                            "mean_log_score": sum(r["log_score"] for r in rows) / count if count else None,
                            "buckets": buckets, "note": "Calibration is not meaningful with a small or selectively resolved sample."})

        @app.route("/api/impact/projects", methods=["POST"])
        @logged_tool("propose_impact_project")
        def propose_impact():
            d = request.get_json(force=True)
            title, objective = str(d.get("title") or "").strip(), str(d.get("objective") or "").strip()
            criteria = d.get("acceptance_criteria")
            if not title or not objective or not isinstance(criteria, list) or not [c for c in criteria if str(c).strip()]:
                return self.err("title, objective, and acceptance_criteria required")
            try:
                refs = self._refs(d.get("source_refs"))
                probability, impact = _clamp(d.get("probability_success")), _clamp(d.get("impact_magnitude"))
                effort, risk = _clamp(d.get("effort")), _clamp(d.get("safety_risk"), 0)
            except ValueError as exc:
                return self.err(str(exc))
            kind = d.get("deliverable_kind")
            if kind not in {"software", "experiment", "analysis", "design", "document", "prototype", "operation"}:
                return self.err("invalid deliverable_kind")
            score = probability * impact * (1 - risk) / (effort + 0.25)
            pid, now = "IMP-" + uuid.uuid4().hex, self.now_iso()
            self.get_db().execute("INSERT INTO impact_projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (pid, title, objective, str(d.get("domain") or ""), json.dumps(refs), probability, impact,
                                   effort, risk, score, kind, json.dumps(criteria), "proposed", None, None, None, None, None, now, now))
            self.get_db().commit()
            self._event("impact.proposed", {"project_id": pid, "title": title, "score": score})
            return self.ok({"id": pid, "score": score, "status": "proposed"})

        @app.route("/api/impact/projects")
        @logged_tool("rank_impact_projects")
        def rank_impact():
            status = request.args.get("status", "proposed")
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
            rows = self.get_db().execute("SELECT * FROM impact_projects WHERE status=? ORDER BY score DESC,created_at LIMIT ?", (status, limit)).fetchall()
            return self.ok([{**dict(r), "source_refs": _json(r["source_refs_json"], []),
                             "acceptance_criteria": _json(r["acceptance_criteria_json"], [])} for r in rows])

        @app.route("/api/impact/projects/queue", methods=["POST"])
        @logged_tool("queue_impact_project")
        def queue_impact():
            d, db = request.get_json(force=True), self.get_db()
            row = db.execute("SELECT * FROM impact_projects WHERE id=?", (d.get("project_id"),)).fetchone()
            if not row:
                return self.err("impact project not found", 404)
            if row["status"] != "proposed":
                return self.err("only proposed projects can be queued")
            if not self.queue_job:
                return self.err("durable job queue is unavailable", 503)
            criteria = _json(row["acceptance_criteria_json"], [])
            objective = row["objective"] + "\n\nAcceptance criteria:\n" + "\n".join("- " + str(item) for item in criteria)
            job_id = self.queue_job("agent", objective, {"template": d.get("template"), "max_steps": 40,
                                                          "max_seconds": 7200, "impact_project_id": row["id"]},
                                    _clamp(d.get("priority"), min(1, 0.5 + row["score"] / 4)))
            now = self.now_iso()
            db.execute("UPDATE impact_projects SET status='queued',job_id=?,updated_at=? WHERE id=?", (job_id, now, row["id"])); db.commit()
            self._event("impact.queued", {"project_id": row["id"], "job_id": job_id})
            return self.ok({"project_id": row["id"], "job_id": job_id, "status": "queued"})

        @app.route("/api/impact/projects/outcome", methods=["POST"])
        @logged_tool("record_impact_outcome")
        def record_impact_outcome():
            d, db = request.get_json(force=True), self.get_db()
            row = db.execute("SELECT * FROM impact_projects WHERE id=?", (d.get("project_id"),)).fetchone()
            if not row:
                return self.err("impact project not found", 404)
            status, artifact_id = d.get("status"), d.get("artifact_id") or None
            if status not in {"completed", "failed", "blocked", "cancelled"}:
                return self.err("invalid impact outcome status")
            if status == "completed":
                if not artifact_id:
                    return self.err("completed impact projects require a verified artifact_id")
                try:
                    verification = self.verify_artifact(artifact_id) if self.verify_artifact else None
                except Exception as exc:
                    return self.err("artifact verification failed: " + str(exc))
                if not verification or not verification.get("verified"):
                    return self.err("completed impact projects require a currently verified artifact")
            try:
                refs = self._refs(d.get("source_refs") or [], required=False)
                actual = _clamp(d.get("actual_impact")) if d.get("actual_impact") is not None else None
            except ValueError as exc:
                return self.err(str(exc))
            now = self.now_iso()
            db.execute("UPDATE impact_projects SET status=?,artifact_id=?,outcome_summary=?,actual_impact=?,outcome_refs_json=?,updated_at=? WHERE id=?",
                       (status, artifact_id, str(d.get("summary") or ""), actual, json.dumps(refs), now, row["id"])); db.commit()
            self._event("impact.outcome", {"project_id": row["id"], "status": status, "artifact_id": artifact_id, "actual_impact": actual})
            return self.ok({"project_id": row["id"], "status": status, "artifact_id": artifact_id})

    def summary(self):
        db = self.get_db()
        return {
            "ontology_entities": db.execute("SELECT COUNT(*) FROM ontology_entities").fetchone()[0],
            "ontology_relations": db.execute("SELECT COUNT(*) FROM ontology_relations").fetchone()[0],
            "open_forecasts": db.execute("SELECT COUNT(*) FROM forecasts WHERE status='open'").fetchone()[0],
            "resolved_forecasts": db.execute("SELECT COUNT(*) FROM forecasts WHERE status='resolved'").fetchone()[0],
            "proposed_impacts": db.execute("SELECT COUNT(*) FROM impact_projects WHERE status='proposed'").fetchone()[0],
            "active_impacts": db.execute("SELECT COUNT(*) FROM impact_projects WHERE status IN ('queued','running')").fetchone()[0],
        }
