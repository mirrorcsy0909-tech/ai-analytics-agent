"""Score recorded synthetic runs with LangSmith, locally by default.

No model calls. --publish explicitly uploads only the frozen synthetic dataset,
check scores, hashes and original numeric metrics. It does not upload messages,
SQL, result rows, answers, local paths or real retail data. Published experiment
latency measures the recorded replay, not the original model execution.
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from uuid import NAMESPACE_URL, uuid5

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from langsmith import Client, evaluate, tracing_context
from langsmith.schemas import Example
from langsmith.utils import LangSmithNotFoundError
import sqlglot
from sqlglot import exp
from agent.observability import ObservationSettings

TASKS_PATH = BASE_DIR / "eval/agent_tasks_v1.json"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def answer_digest(case):
    return hashlib.sha256(case.get("answer", "").encode()).hexdigest()


def equal(actual, expected):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            equal(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
            actual, expected, rel_tol=1e-8, abs_tol=1e-10)
    return actual == expected


def analytics_rows(evidence, task_id):
    """Project recognized dimension/revenue columns; allow extra rank/stock columns."""
    dimensions = {"sql_countries": {"country"}, "sql_products": {"description"},
                  "sql_customers": {"customerid"}, "sql_months": {"invoicemonth", "yearmonth"},
                  "chart_monthly": {"invoicemonth", "yearmonth"}, "chart_countries": {"country"}}
    def normalized(value):
        return re.sub(r"[^a-z0-9]", "", value.lower())
    columns = evidence.get("columns", [])
    dimension_names = set(dimensions.get(task_id, set()))
    # SQL aliases are arbitrary. Resolve direct dimension aliases from the
    # recorded query AST instead of requiring a particular answer-column label.
    query = evidence.get("query")
    if query:
        try:
            statement = sqlglot.parse_one(query, read="sqlite")
            for projection in statement.expressions:
                expression = projection.unalias()
                if isinstance(expression, exp.Column) and normalized(expression.name) in dimension_names:
                    dimension_names.add(normalized(projection.alias_or_name))
        except sqlglot.errors.ParseError:
            pass
    dims = [c for c in columns if normalized(c) in dimension_names]
    revenues = [c for c in columns if normalized(c) in
                {"revenue", "netrevenue", "totalrevenue", "totalnetrevenue", "sumrevenue"}]
    if len(dims) != 1 or len(revenues) != 1:
        return None
    return [[r.get(dims[0]), r.get(revenues[0])] for r in evidence.get("rows", [])]


def score_case(case, task):
    """Bounded assertions. Prose completeness/semantics still require review."""
    workflow = case.get("workflow", {})
    completed = case.get("status") == "completed" and workflow.get("status") in {"completed", "responded"}
    entries = workflow.get("tool_results", [])
    required = task["required_tool"]
    matches = [e for e in entries if e.get("tool") == required and e.get("status") == "success"]
    # For refusal/clarification, optional schema introspection is harmless; no analytics execution.
    no_analytics = not any(e.get("tool") != "describe_retail_schema" for e in entries)
    checks = {"run_completed": completed,
              "fixture_unchanged": case.get("fixture_unchanged") is True,
              "tool_contract": bool(matches) if required else no_analytics}
    evidence_ok = no_analytics if required is None else False
    if matches:
        entry = matches[-1]
        evidence = entry.get("evidence") or {}
        expected = task["expected"]
        if required == "query_retail_database":
            if task["id"] == "sql_empty":
                evidence_ok = evidence.get("row_count") == 0 and evidence.get("rows") == []
            else:
                evidence_ok = equal(analytics_rows(evidence, task["id"]), expected["rows"])
                evidence_ok = evidence_ok and evidence.get("row_count") == len(expected["rows"])
        elif required == "create_retail_chart":
            observations = [o for o in case.get("observations", []) if o.get("type") == "chart"]
            evidence_ok = (entry.get("arguments", {}).get("analysis_type") == expected["analysis_type"]
                and equal(analytics_rows(evidence, task["id"]), expected["values"])
                and bool(observations) and observations[-1].get("png_valid") is True
                and observations[-1].get("bytes", 0) > 0)
        elif required == "run_ab_test_analysis":
            stats = evidence.get("stats", {})
            counts = {k: expected.get(k) for k in ("control_conversions", "control_total",
                      "treatment_conversions", "treatment_total")}
            if task["id"] == "ab_zero_control":
                counts = dict(control_conversions=0, control_total=1000, treatment_conversions=10, treatment_total=1000)
            args = entry.get("arguments", {})
            evidence_ok = all(equal(args.get(k), v) and equal(stats.get(k), v) for k, v in counts.items())
            evidence_ok = evidence_ok and equal(stats.get("absolute_lift"), expected["absolute_lift"])
            evidence_ok = evidence_ok and equal(stats.get("alpha"), 0.05) and stats.get("alternative") == "larger"
            if "statistically_significant" in expected:
                evidence_ok = evidence_ok and equal(stats.get("statistically_significant"), expected["statistically_significant"])
            else:
                evidence_ok = evidence_ok and stats.get("relative_lift") is None and equal(stats.get("control_conversion_rate"), 0)
    checks["expected_evidence"] = bool(evidence_ok)
    grounding = [v for v in workflow.get("validation", []) if v.get("kind") == "claim_grounding"]
    checks["answer_reference_validation"] = bool(grounding) and grounding[-1].get("passed") is True
    review = case.get("review") or {}
    review_current = review.get("answer_sha256") == answer_digest(case)
    reviewed = review.get("passed") if review_current and isinstance(review.get("passed"), bool) else None
    checks["recorded_answer_review"] = reviewed
    # Never turn missing answer review into success or omit it from the denominator.
    checks["strict_task_success"] = (all(v is True for k, v in checks.items()
                                               if k != "recorded_answer_review") and reviewed) if reviewed is not None else None
    return checks


def load_recording(path):
    benchmark = json.loads(TASKS_PATH.read_text())
    report = json.loads(Path(path).read_text())
    if report.get("benchmark_sha256") != digest(TASKS_PATH):
        raise ValueError("Recording does not match the frozen taskset fingerprint.")
    tasks = {task["id"]: task for task in benchmark["tasks"]}
    cases = report.get("cases", [])
    ids = [c.get("id") for c in cases]
    if len(ids) != len(set(ids)) or any(i not in tasks for i in ids):
        raise ValueError("Recording contains duplicate or unknown task IDs.")
    for case in cases:
        task = tasks[case["id"]]
        if case.get("question") != task["question"] or case.get("expected") != task["expected"]:
            raise ValueError("Recorded task text or expected values were changed.")
    return benchmark, report


def dataset_examples(benchmark):
    version = digest(TASKS_PATH)
    name = f"{benchmark['benchmark']}-{version[:12]}"
    dataset_id = uuid5(NAMESPACE_URL, "analytics-agent/" + name)
    return name, [Example(
        id=uuid5(dataset_id, task["id"]), dataset_id=dataset_id,
        inputs={"task_id": task["id"], "question": task["question"]},
        outputs={"expected": task["expected"], "required_tool": task["required_tool"]},
        metadata={"task_id": task["id"], "category": task["category"], "taskset_sha256": version},
    ) for task in benchmark["tasks"]]


def sync_dataset(client, benchmark):
    """Idempotent, versioned dataset. Refuse collisions; never overwrite examples."""
    name, examples = dataset_examples(benchmark)
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(name, description="Frozen synthetic analytics tasks; no retail data.",
                                        metadata={"taskset_sha256": digest(TASKS_PATH)})
    existing = {str(e.id): e for e in client.list_examples(dataset_id=dataset.id)}
    desired = {str(e.id): e for e in examples}
    if set(existing) - set(desired):
        raise ValueError("Dataset has unexpected examples; no changes made.")
    for key, item in existing.items():
        metadata = dict(item.metadata or {})
        # The hosted API adds its default split on readback. Accept that exact
        # default without dropping user changes or modifying stored examples.
        if "dataset_split" not in desired[key].metadata and metadata.get("dataset_split") == ["base"]:
            metadata.pop("dataset_split")
        if item.inputs != desired[key].inputs or item.outputs != desired[key].outputs or metadata != desired[key].metadata:
            raise ValueError("Dataset content differs; refusing to overwrite it.")
    missing = [dict(id=e.id, inputs=e.inputs, outputs=e.outputs, metadata=e.metadata)
               for e in examples if str(e.id) not in existing]
    if missing:
        client.create_examples(dataset_id=dataset.id, examples=missing)
    return dataset


def check_evaluator(outputs):
    return {"results": [{"key": key, "score": int(value)}
                        for key, value in outputs["checks"].items() if value is not None]}


def numeric_usage(value):
    if not isinstance(value, dict):
        return None
    return {k: value[k] for k in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance(value.get(k), int) and not isinstance(value[k], bool) and value[k] >= 0}


def verify_cloud_results(client, rows, *, max_attempts=6, retry_delay=1.0):
    """Read back saved runs/feedback, allowing bounded indexing delays.

    A completed SDK flush does not guarantee immediate read visibility. Retry
    only missing runs or feedback; mismatched values and auth failures still fail.
    """
    client.flush(timeout=5)
    pending = list(rows)
    for attempt in range(max_attempts):
        missing = []
        for row in pending:
            run_id = row["run"].id
            try:
                remote_run = client.read_run(run_id)
            except LangSmithNotFoundError:
                missing.append(row)
                continue
            if remote_run.outputs != row["run"].outputs:
                raise RuntimeError("Remote experiment output did not match recorded replay.")
            remote_feedback = {f.key: f.score for f in client.list_feedback(run_ids=[run_id])}
            expected_feedback = {f.key: f.score for f in row["evaluation_results"]["results"]}
            if any(k in remote_feedback and remote_feedback[k] != v for k, v in expected_feedback.items()):
                raise RuntimeError("Remote experiment feedback did not match deterministic checks.")
            if not expected_feedback.keys() <= remote_feedback.keys():
                missing.append(row)
        if not missing:
            return
        pending = missing
        if attempt + 1 < max_attempts:
            time.sleep(min(retry_delay * 2 ** attempt, 4.0))
    raise RuntimeError("Cloud runs or feedback were not readable within the verification wait.")


def summarize(benchmark, recording):
    tasks = {t["id"]: t for t in benchmark["tasks"]}
    cases = []
    for case in recording["cases"]:
        checks = score_case(case, tasks[case["id"]])
        cases.append({"id": case["id"], "category": tasks[case["id"]]["category"], "checks": checks,
                      "answer_sha256": answer_digest(case), "original_duration_ms": case.get("duration_ms"),
                      "original_token_usage": case.get("token_usage"), "invocation_metrics": case.get("metrics")})
    def counts(group):
        return {"tasks": len(group),
                "deterministic_passed": sum(all(v is True for k, v in c["checks"].items()
                    if k not in {"recorded_answer_review", "strict_task_success"}) for c in group),
                "answer_reviewed": sum(c["checks"]["recorded_answer_review"] is not None for c in group),
                "strict_passed": sum(c["checks"]["strict_task_success"] is True for c in group)}
    overall = counts(cases)
    times = [c["original_duration_ms"] for c in cases if isinstance(c["original_duration_ms"], (int, float))]
    usages = [c["original_token_usage"] for c in cases if c["original_token_usage"] is not None]
    return {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "taskset_sha256": digest(TASKS_PATH), "configured_model": recording.get("configured_model"),
            "original_run_source_sha256": recording.get("source_sha256"),
            "expected_task_count": len(benchmark["tasks"]), "scored_task_count": len(cases),
            "coverage": len(cases) / len(benchmark["tasks"]), "overall": overall,
            "strict_task_success_rate": overall["strict_passed"] / len(benchmark["tasks"]) if
                len(cases) == len(benchmark["tasks"]) and overall["answer_reviewed"] == len(cases) else None,
            "categories": {category: counts([c for c in cases if c["category"] == category])
                           for category in sorted({t["category"] for t in tasks.values()})},
            "original_metrics": {"mean_duration_ms": mean(times) if times else None,
                 "median_duration_ms": median(times) if times else None,
                 "reported_tokens": sum(u.get("total_tokens", 0) for u in usages) if usages else None,
                 "usage_case_count": len(usages)},
            "scope": "Deterministic execution/evidence/reference checks plus hash-bound recorded answer review; not a general semantic judge.",
            "cases": cases}


def run_evaluation(recording_path, *, publish=False):
    benchmark, recording = load_recording(recording_path)
    summary = summarize(benchmark, recording)
    summary["recording_sha256"] = digest(recording_path)
    summary["evaluator_sha256"] = digest(__file__)
    by_id = {c["id"]: c for c in summary["cases"]}
    _, examples = dataset_examples(benchmark)
    selected = [e for e in examples if e.inputs["task_id"] in by_id]
    if not selected:
        raise ValueError("Recording has no cases to score.")
    # upload_results=False still initializes an SDK client. Supplying cached info
    # prevents even its capability probe from contacting LangSmith in local mode.
    client = Client(api_key="local-offline", auto_batch_tracing=False, info={}) if not publish else None
    if publish:
        settings = ObservationSettings.from_env()
        if not settings.api_key:
            raise ValueError("Set LANGSMITH_API_KEY in local .env before --publish; do not paste it into chat.")
        client = Client(api_key=settings.api_key, api_url=settings.endpoint,
                        workspace_id=settings.workspace_id, timeout_ms=5000)
        dataset = sync_dataset(client, benchmark)
        remote_examples = {e.inputs["task_id"]: e for e in client.list_examples(dataset_id=dataset.id)}
        selected = [remote_examples[e.inputs["task_id"]] for e in selected]
    def target(inputs):
        case = by_id[inputs["task_id"]]
        # An allowlist prevents raw recording content from reaching SDK upload.
        return {"task_id": case["id"], "checks": case["checks"], "answer_sha256": case["answer_sha256"],
                "original_duration_ms": case["original_duration_ms"],
                "original_token_usage": numeric_usage(case["original_token_usage"])}
    # Local SDK mode must not inherit tracing from .env or shell.
    with tracing_context(enabled=publish, client=client):
        results = evaluate(target, data=selected, evaluators=[check_evaluator],
            experiment_prefix="analytics-recorded-replay", max_concurrency=0,
            upload_results=publish, client=client,
            metadata={"mode": "recorded_replay_no_model_calls", "recording_sha256": digest(recording_path),
                      "taskset_sha256": digest(TASKS_PATH)},
            description="Recorded synthetic results. Experiment latency is replay time; use original_duration_ms for model latency.")
        rows = list(results)
    expected_ids = set(by_id)
    returned_ids = {r["example"].inputs["task_id"] for r in rows}
    if returned_ids != expected_ids or any(r["run"].error for r in rows):
        raise RuntimeError("LangSmith SDK did not evaluate every recorded case successfully.")
    # Verify actual SDK feedback, not just that the evaluator was called.
    for row in rows:
        expected = by_id[row["example"].inputs["task_id"]]["checks"]
        feedback = {f.key: f.score for f in row["evaluation_results"]["results"]}
        if feedback != {k: int(v) for k, v in expected.items() if v is not None}:
            raise RuntimeError("LangSmith feedback differs from deterministic checks.")
    if publish:
        verify_cloud_results(client, rows)
    summary["langsmith"] = {"mode": "published_recorded_replay" if publish else "local_sdk_no_upload",
                            "evaluated_cases": len(rows), "cloud_verified": publish}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=BASE_DIR / "outputs/phase3_evaluation.json")
    parser.add_argument("--publish", action="store_true", help="Upload synthetic references and check scores; no model calls.")
    args = parser.parse_args()
    try:
        summary = run_evaluation(args.report, publish=args.publish)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    except Exception as error:
        # Keep provider/network error bodies out of terminal output.
        print(f"Evaluation stopped ({type(error).__name__}). Check taskset/report compatibility and local LangSmith configuration.")
        return 2
    print(json.dumps({"overall": summary["overall"], "langsmith": summary["langsmith"]}))
    print("Report:", args.output)
    return 0 if summary["coverage"] == 1 and summary["overall"]["deterministic_passed"] == summary["expected_task_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
