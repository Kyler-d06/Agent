"""Run source-controlled calculator and live RSI prompt regression suites."""
import argparse
import json
from pathlib import Path

from improvement_engine import ImprovementEngine
from evaluation_prompts import CAPABILITY_QUALITY_PROMPT, DISCOVERY_QUALITY_PROMPT
from model_gateway import ModelGateway
from runtime_store import RuntimeStore


def run(root, database=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    engine = ImprovementEngine(RuntimeStore(str(database or root / "evaluations.db")), root, None)
    spec = json.loads((Path(__file__).parent / "evaluations" / "calculator.json").read_text(encoding="utf-8"))
    engine.suite(**spec)
    version = engine.register_builtin("calculate")
    report = engine.evaluate(version["id"], spec["name"])
    if report["passed"]:
        report["promotion"] = engine.promote(version["id"], spec["name"])
    report["version_id"] = version["id"]
    report["source_hash"] = version["content"]["source_hash"]
    (root / "calculator-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_prompt(root, suite_file, version_name, prompt, task_types, database=None, models=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    store = RuntimeStore(str(database or root / "evaluations.db"))
    models = models or ModelGateway(store=store)
    engine = ImprovementEngine(store, root, models)
    spec = json.loads((Path(__file__).parent / "evaluations" / suite_file).read_text(encoding="utf-8"))
    engine.suite(**spec)
    version = engine.register_prompt(version_name, prompt, task_types)
    report = engine.evaluate(version["id"], spec["name"])
    if report["passed"]:
        report["promotion"] = engine.promote(version["id"], spec["name"])
    report["version_id"] = version["id"]
    report["source_hash"] = version["hash"]
    (root / f"{version_name}-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".platform/evaluations")
    parser.add_argument("--database", help="Optional existing core SQLite database")
    parser.add_argument("--live", choices=["discovery", "capability", "all"],
                        help="Also run a model-backed 20-24 case suite and promote only if it passes")
    args = parser.parse_args()
    report = run(args.output, args.database)
    reports = {"calculator": report}
    if args.live in {"discovery", "all"}:
        reports["discovery"] = run_prompt(args.output, "discovery_pipeline.json", "discovery_quality",
                                           DISCOVERY_QUALITY_PROMPT, ["research"], args.database)
    if args.live in {"capability", "all"}:
        reports["capability"] = run_prompt(args.output, "capability_factory.json", "capability_factory_quality",
                                            CAPABILITY_QUALITY_PROMPT, ["capability_build"], args.database)
    print(json.dumps(reports, indent=2))
    raise SystemExit(0 if all(item["passed"] for item in reports.values()) else 1)
