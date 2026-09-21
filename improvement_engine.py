"""Immutable prompt/capability candidates evaluated against owner-authored cases."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from platform_contracts import canonical, digest, validate
from work_tools import run_container, sandbox_command


def _calculator_hash():
    import calculator
    import inspect
    import tools
    return digest({"implementation": Path(calculator.__file__).read_text(encoding="utf-8"),
                   "entrypoint": inspect.getsource(tools.calculate)})


class ImprovementEngine:
    def __init__(self, store, root, models):
        self.store, self.models = store, models
        self.root = Path(root) / ".platform" / "versions"
        self.root.mkdir(parents=True, exist_ok=True)

    def register_builtin(self, name):
        """Local maintainer entry point; never executes submitted code on the host.

        Builtins are allowlisted, bounded existing functions. Generated capability
        candidates continue to execute exclusively in Docker.
        """
        if name != "calculate":
            raise ValueError("unknown builtin")
        content = {"builtin": name, "source_hash": _calculator_hash()}
        with self.store.connect() as db:
            prior = db.execute("SELECT id FROM improvement_versions WHERE name=? AND kind='builtin' AND hash=?", (name, digest(content))).fetchone()
            if prior:
                return self.version(prior[0])
            if db.execute("SELECT 1 FROM improvement_versions WHERE name=? AND kind!='builtin'", (name,)).fetchone():
                raise ValueError("improvement name already used")
            vid = uuid.uuid4().hex
            db.execute("INSERT INTO improvement_versions VALUES(?,?,?,?,?,'candidate',?)", (vid, name, "builtin", canonical(content), digest(content), time.time()))
        return self.version(vid)

    def register_prompt(self, name, text, task_types):
        """Idempotently register a source-controlled prompt candidate."""
        content = {"text": text, "task_types": sorted(set(task_types))}
        target_hash = digest(content)
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM improvement_versions WHERE name=? AND kind='prompt' AND hash=?",
                             (name, target_hash)).fetchone()
        if row:
            return self.version(row[0])
        return self.version(self.propose(name, "prompt", content)["id"])

    def _run_builtin(self, version, args):
        content = version["content"]
        if content.get("builtin") != "calculate" or content.get("source_hash") != _calculator_hash():
            raise ValueError("builtin source changed; register and evaluate a new version")
        validate({"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"], "additionalProperties": False}, args)
        # Exercise the same wrapper exposed through /api/tools.
        from tools import calculate
        return calculate(**args)

    def propose(self, name, kind, content):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name) or kind not in {"prompt", "capability"}:
            raise ValueError("valid name and prompt/capability kind required")
        if len(canonical(content)) > 200000:
            raise ValueError("candidate too large")
        if kind == "prompt":
            if not isinstance(content.get("text"), str) or not content["text"].strip():
                raise ValueError("prompt text required")
        else:
            from jsonschema import Draft202012Validator
            if "def run(" not in content.get("code", "") or not content.get("description"):
                raise ValueError("capability code must define run(args); description required")
            if content.get("input_schema", {}).get("type") != "object":
                raise ValueError("object input_schema required")
            Draft202012Validator.check_schema(content["input_schema"])
        vid = uuid.uuid4().hex
        with self.store.connect() as db:
            prior = db.execute("SELECT kind FROM improvement_versions WHERE name=? LIMIT 1", (name,)).fetchone()
            if prior and prior[0] != kind:
                raise ValueError("an improvement name cannot change kind")
            db.execute("INSERT INTO improvement_versions VALUES(?,?,?,?,?,'candidate',?)", (vid, name, kind, canonical(content), digest(content), time.time()))
        return {"id": vid, "status": "candidate", "hash": digest(content)}

    def suite(self, name, cases, min_score=1.0):
        if not name or not isinstance(cases, list) or not 1 <= len(cases) <= 100 or not 0 <= min_score <= 1:
            raise ValueError("suite needs 1-100 independent cases and min_score in [0,1]")
        for case in cases:
            if "input" not in case or "expected" not in case or case.get("matcher", "equals") not in {"equals", "contains", "schema"}:
                raise ValueError("each case needs input, expected and a supported matcher")
        with self.store.connect() as db:
            db.execute("INSERT INTO evaluation_suites VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET cases_json=excluded.cases_json,min_score=excluded.min_score,updated_at=excluded.updated_at",
                       (name, canonical(cases), min_score, time.time()))
        return {"name": name, "cases": len(cases)}

    def version(self, vid):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM improvement_versions WHERE id=?", (vid,)).fetchone()
        if not row:
            raise ValueError("unknown improvement version")
        version = dict(row)
        version["content"] = json.loads(version.pop("content_json"))
        if digest(version["content"]) != version["hash"]:
            raise ValueError("version integrity check failed")
        return version

    def run_capability(self, version, args, timeout=60):
        content = version["content"]
        validate(content["input_schema"], args)
        path = self.root / version["id"]
        path.mkdir(exist_ok=True)
        code_path = path / "tool.py"
        code_path.write_text(content["code"], encoding="utf-8")
        runner = "import json,sys;from tool import run;print(json.dumps(run(json.load(sys.stdin))))"
        command = sandbox_command("python:3.12-slim", path, ["python", "-c", runner])
        command.insert(2, "-i")
        result = run_container(command, timeout=timeout, input_text=canonical(args))
        if not result["ok"]:
            raise RuntimeError(result["stderr"][:2000])
        return json.loads(result["stdout"])

    def evaluate(self, version_id, suite):
        version = self.version(version_id)
        with self.store.connect() as db:
            spec = db.execute("SELECT * FROM evaluation_suites WHERE name=?", (suite,)).fetchone()
        if not spec:
            raise ValueError("owner-authored evaluation suite required")
        cases = json.loads(spec["cases_json"])
        results, started = [], time.monotonic()
        for i, case in enumerate(cases):
            try:
                remaining = 240 - (time.monotonic() - started)
                if remaining < 1:
                    raise TimeoutError("evaluation exceeded its four-minute budget")
                if version["kind"] == "builtin":
                    actual = self._run_builtin(version, case["input"])
                elif version["kind"] == "capability":
                    actual = self.run_capability(version, case["input"], timeout=min(60, remaining))
                else:
                    answer = self.models.chat([{"role": "system", "content": version["content"]["text"]},
                                               {"role": "user", "content": canonical(case["input"]) if not isinstance(case["input"], str) else case["input"]}],
                                              task_type="evaluation", temperature=0, budget_seconds=remaining)
                    actual = answer["content"]
                matcher = case.get("matcher", "equals")
                if matcher == "schema":
                    actual = json.loads(actual) if isinstance(actual, str) else actual
                    validate(case["expected"], actual)
                    passed = True
                elif matcher == "contains":
                    passed = str(case["expected"]) in str(actual)
                else:
                    passed = actual == case["expected"]
                results.append({"case": i, "passed": passed, "actual": actual})
            except Exception as e:
                results.append({"case": i, "passed": False, "error": type(e).__name__ + ": " + str(e)[:1000]})
        score = sum(r["passed"] for r in results) / len(results)
        report = {"cases": results, "elapsed_seconds": time.monotonic() - started, "score": score, "passed": score >= spec["min_score"]}
        eid = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)", (eid, version_id, suite, digest({"cases": cases, "min_score": spec["min_score"]}), score, int(report["passed"]), canonical(report), time.time()))
        self.store.event("improvement.evaluated", {"version_id": version_id, "evaluation_id": eid, "score": score})
        return {"id": eid, **report}

    @staticmethod
    def _json_object(text):
        text = str(text or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("generator did not return a JSON object")
            value = json.loads(text[start:end + 1])
        if not isinstance(value, dict):
            raise ValueError("generator did not return a JSON object")
        return value

    def evolve(self, name, kind, suite, population=6, generations=3, promote=False):
        """Generate and score a bounded population; promotion remains explicit."""
        if self.models is None:
            raise ValueError("a model gateway is required for evolution")
        if kind not in {"prompt", "capability"} or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name or ""):
            raise ValueError("valid name and prompt/capability kind required")
        population, generations = int(population), int(generations)
        if not 2 <= population <= 8 or not 1 <= generations <= 5 or population * generations > 40:
            raise ValueError("population must be 2-8, generations 1-5, with at most 40 candidates")
        with self.store.connect() as db:
            spec = db.execute("SELECT cases_json,min_score FROM evaluation_suites WHERE name=?", (suite,)).fetchone()
            active = db.execute("SELECT id FROM improvement_versions WHERE name=? AND kind=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                                (name, kind)).fetchone()
        if not spec:
            raise ValueError("owner-authored evaluation suite required")
        seeds = [self.version(active[0])["content"]] if active else []
        history, survivors = [], []
        for generation in range(1, generations + 1):
            candidates = []
            seed_text = canonical(seeds)[:30000] if seeds else "[]"
            feedback = canonical([{"score": s["evaluation"]["score"],
                                   "failures": [c for c in s["evaluation"]["cases"] if not c.get("passed")][:5]}
                                  for s in survivors])[:16000]
            for index in range(population):
                contract = ("Return JSON with text (a non-empty system prompt) and task_types (an array of short strings)."
                            if kind == "prompt" else
                            "Return JSON with description, input_schema (an object JSON schema), and code defining run(args). Use the Python standard library only; no shell, credentials, filesystem escape, or hidden downloads.")
                response = self.models.chat([
                    {"role": "system", "content": "Generate one bounded improvement candidate as strict JSON only."},
                    {"role": "user", "content": f"Improve {kind} '{name}' for evaluation suite '{suite}'.\n{contract}\n"
                     f"Generation: {generation}; variant: {index + 1}.\nSurvivor seeds:\n{seed_text}\nPrior failure feedback:\n{feedback}"},
                ], task_type="improvement", temperature=0.4, budget_seconds=180)
                try:
                    content = self._json_object(response.get("content"))
                    proposed = self.propose(name, kind, content)
                    evaluation = self.evaluate(proposed["id"], suite)
                    candidates.append({"version_id": proposed["id"], "evaluation": evaluation, "content": content})
                except Exception as exc:
                    history.append({"generation": generation, "variant": index + 1, "error": type(exc).__name__ + ": " + str(exc)[:1000]})
            if not candidates:
                raise RuntimeError(f"generation {generation} produced no evaluable candidates")
            candidates.sort(key=lambda item: (-item["evaluation"]["score"], item["version_id"]))
            survivors = candidates[:2]
            seeds = [item["content"] for item in survivors]
            history.append({"generation": generation, "candidates": [
                {"version_id": c["version_id"], "score": c["evaluation"]["score"], "passed": c["evaluation"]["passed"]}
                for c in candidates], "survivors": [s["version_id"] for s in survivors]})
            self.store.event("improvement.evolved_generation", {"name": name, "kind": kind, "suite": suite,
                                                                 "generation": generation, "survivors": history[-1]["survivors"]})
        winner = survivors[0]
        promotion = None
        if promote:
            promotion = self.promote(winner["version_id"], suite)
        result = {"name": name, "kind": kind, "suite": suite, "winner": winner["version_id"],
                  "score": winner["evaluation"]["score"], "passed": winner["evaluation"]["passed"],
                  "promoted": bool(promotion), "history": history}
        self.store.event("improvement.evolved", {k: result[k] for k in ("name", "kind", "suite", "winner", "score", "passed", "promoted")})
        return result

    def promote(self, version_id, suite):
        version = self.version(version_id)
        if version["kind"] == "builtin":
            self._run_builtin(version, {"expression": "0"})
        with self.store.connect(True) as db:
            spec = db.execute("SELECT * FROM evaluation_suites WHERE name=?", (suite,)).fetchone()
            if not spec:
                raise ValueError("unknown suite")
            suite_hash = digest({"cases": json.loads(spec["cases_json"]), "min_score": spec["min_score"]})
            latest = db.execute("SELECT * FROM evaluations WHERE version_id=? AND suite=? AND suite_hash=? ORDER BY created_at DESC LIMIT 1", (version_id, suite, suite_hash)).fetchone()
            if not latest or not latest["passed"]:
                raise ValueError("candidate must pass the current independent suite")
            active = db.execute("SELECT id FROM improvement_versions WHERE name=? AND status='active'", (version["name"],)).fetchone()
            if active and active[0] != version_id:
                baseline = db.execute("SELECT score FROM evaluations WHERE version_id=? AND suite=? AND suite_hash=? ORDER BY created_at DESC LIMIT 1", (active[0], suite, suite_hash)).fetchone()
                if not baseline or latest["score"] < baseline[0]:
                    raise ValueError("evaluate baseline on the same suite; candidate must not regress")
            db.execute("UPDATE improvement_versions SET status='retired' WHERE name=? AND status='active'", (version["name"],))
            db.execute("UPDATE improvement_versions SET status='active' WHERE id=?", (version_id,))
        self.store.event("improvement.promoted", {"id": version_id, "previous": active[0] if active else None})
        return {"id": version_id, "status": "active"}

    def rollback(self, version_id):
        version = self.version(version_id)
        if version["status"] != "retired":
            raise ValueError("rollback target must be a previously active version")
        with self.store.connect(True) as db:
            db.execute("UPDATE improvement_versions SET status='retired' WHERE name=? AND status='active'", (version["name"],))
            db.execute("UPDATE improvement_versions SET status='active' WHERE id=?", (version_id,))
        self.store.event("improvement.rolled_back", {"id": version_id})
        return {"id": version_id, "status": "active"}

    def active(self, kind=None):
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM improvement_versions WHERE status='active' AND (? IS NULL OR kind=?)", (kind, kind))]
        return [self.version(vid) for vid in ids]

    def tools(self):
        return [{"name": "skill_" + v["name"], "description": v["content"]["description"], "input_schema": v["content"]["input_schema"],
                 "method": "POST", "path": "/api/tool-gateway", "effect": "execute", "requires_confirmation": True,
                 "version_id": v["id"]} for v in self.active("capability")]
