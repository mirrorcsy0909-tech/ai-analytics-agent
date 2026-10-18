"""New-scenario acceptance against a frozen synthetic fixture.

--run makes real model calls. Remote tracing is always disabled here. Expected
results are frozen in acceptance_v1.json, never generated from Agent output.
Narrative correctness needs a separate, hash-bound answer review.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plain(value):
    return json.loads(json.dumps(value, default=lambda x: x.item() if hasattr(x, "item") else str(x)))


def answer_digest(turn):
    return hashlib.sha256(turn.get("answer", "").encode()).hexdigest()


def create_fixture(path, suite):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE transactions (
            InvoiceNo TEXT, StockCode TEXT, Description TEXT, Quantity INTEGER,
            InvoiceDate TEXT, UnitPrice REAL, CustomerID REAL, Country TEXT,
            Revenue REAL, InvoiceMonth TEXT)""")
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", suite["fixture_rows"])
        conn.commit()


def equal(actual, expected):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
            actual, expected, rel_tol=1e-8, abs_tol=1e-10)
    return actual == expected


def check_turn(turn, specification):
    """Check evidence and runtime contracts; do not auto-score natural language."""
    expected = specification["expected"]
    kind = expected["kind"]
    workflow = turn.get("workflow", {})
    entries = workflow.get("tool_results", [])
    successful = [e for e in entries if e.get("status") == "success"
                  and (e.get("evidence") or {}).get("kind") in {"sql", "chart", "ab_test"}]
    checks = {"fixture_unchanged": turn.get("fixture_unchanged") is True}
    if kind == "interrupt":
        checks["expected_interrupt"] = turn.get("error_type") == expected["error_type"]
        checks["checkpoint_pending"] = turn.get("checkpoint_pending") is True
    elif kind == "failure":
        checks["failed_explicitly"] = workflow.get("status") == "failed" and bool(turn.get("answer"))
        checks["no_successful_analysis"] = not successful
        checks["bounded_recovery"] = (0 <= workflow.get("retry_count", -1) <= 2
                                      and workflow.get("tool_rounds", 99) <= 6)
    else:
        checks["completed"] = workflow.get("status") in {"completed", "responded"}
        validations = [v for v in workflow.get("validation", []) if v.get("kind") == "claim_grounding"]
        checks["answer_references_valid"] = bool(validations) and validations[-1].get("passed") is True
        if kind in {"clarification", "refusal"}:
            checks["no_successful_analysis"] = not successful
        elif kind in {"sql", "chart"}:
            matching = [e for e in successful if e["evidence"]["kind"] == kind]
            evidence = matching[-1]["evidence"] if matching else {}
            actual = [[r.get(c) for c in evidence.get("columns", [])] for r in evidence.get("rows", [])]
            checks["reference_table"] = bool(matching) and equal(actual, expected["rows"])
            if kind == "chart":
                checks["chart_type"] = evidence.get("analysis_type") == expected["analysis_type"]
                checks["png_verified"] = any(a.get("png_valid") and a.get("bytes", 0) > 1000
                                             for a in turn.get("artifacts", []))
        elif kind == "ab":
            matching = [e for e in successful if e["evidence"]["kind"] == "ab_test"]
            stats = matching[-1]["evidence"]["stats"] if matching else {}
            checks["counts"] = all(equal(stats.get(k), v) for k, v in expected["counts"].items())
            checks["hypothesis"] = stats.get("alternative") == expected["alternative"] and equal(
                stats.get("alpha"), expected["alpha"])
            checks["reference_statistics"] = all(equal(stats.get(k), expected[k])
                for k in ("absolute_lift", "p_value", "test_method", "conclusion"))
        else:
            raise ValueError("Unknown acceptance case kind.")
    if specification.get("fault") in {"sql_once", "sql_always"}:
        checks["fault_was_exercised"] = turn.get("injected_sql_failures", 0) > 0
    if specification.get("fault") == "sql_once":
        checks["recovered_after_fault"] = workflow.get("retry_count", 0) > 0
    return checks


def summarize(report, suite):
    expected_turns = sum(len(t["turns"]) for t in suite["tasks"])
    turns = [turn for case in report["cases"] for turn in case["turns"]]
    reviewed = [t for t in turns if (t.get("review") or {}).get("answer_sha256") == answer_digest(t)
                and isinstance((t.get("review") or {}).get("passed"), bool)]
    strict = sum(all(t["checks"].values()) and t["review"]["passed"] for t in reviewed)
    usage = [t["metrics"]["token_usage"] for t in turns if t.get("metrics", {}).get("token_usage")]
    return {"expected_scenarios": len(suite["tasks"]), "expected_turns": expected_turns,
            "executed_scenarios": len(report["cases"]), "executed_turns": len(turns),
            "deterministic_passed_turns": sum(all(t["checks"].values()) for t in turns),
            "reviewed_turns": len(reviewed), "strict_passed_turns": strict,
            "strict_success_rate": strict / expected_turns if len(reviewed) == expected_turns else None,
            "model_calls": sum(t.get("metrics", {}).get("model_calls", 0) for t in turns),
            "tool_calls": sum(t.get("metrics", {}).get("tool_calls", 0) for t in turns),
            "reported_tokens": sum(u["total_tokens"] for u in usage),
            "duration_ms": round(sum(t["duration_ms"] for t in turns), 3)}


def save(report, suite, path):
    report["summary"] = summarize(report, suite)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class InterruptedModel:
    """Simulate one transport timeout before a response, then use the real model."""

    def __init__(self, model):
        self.model = model
        self.pending = True

    def bind_tools(self, tools):
        bound = self.model.bind_tools(tools)
        owner = self

        class Bound:
            def invoke(self, messages, config=None):
                if owner.pending:
                    owner.pending = False
                    raise TimeoutError("Injected synthetic pre-response timeout.")
                return bound.invoke(messages, config=config)

        return Bound()


def run_suite(suite_path, output, selected_ids=None):
    from agent import analytics_agent as current
    from agent.observability import ObservationSettings, invoke_analysis
    from langgraph.checkpoint.memory import InMemorySaver
    from tools import sql_tool, visualization_tool
    from eval.phase0_baseline import environment, source_hashes

    suite = json.loads(suite_path.read_text())
    tasks = [t for t in suite["tasks"] if not selected_ids or t["id"] in selected_ids]
    if not tasks or (selected_ids and selected_ids - {t["id"] for t in tasks}):
        raise ValueError("Unknown or empty scenario selection.")
    model = current.build_model(timeout=30, max_retries=0, max_tokens=3000)
    report = {"schema_version": 1, "benchmark": suite["benchmark"], "suite_sha256": digest(suite_path),
              "runner_sha256": digest(__file__), "source_sha256": source_hashes(),
              "dependency_lock_sha256": digest(ROOT / "requirements.lock"),
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": suite["scope"], "configured_model": model.model_name,
              "api": "responses", "reasoning": model.reasoning, "environment": environment(),
              "system_prompt_sha256": hashlib.sha256(current.SYSTEM_PROMPT.encode()).hexdigest(),
              "remote_tracing": False, "cases": [], "reviewer": None}
    save(report, suite, output)
    original_sql = sql_tool.run_sql_query
    with tempfile.TemporaryDirectory(prefix="analytics-acceptance-") as temp:
        fixture = Path(temp) / "synthetic.db"
        create_fixture(fixture, suite)
        initial_hash = digest(fixture)
        with patch.object(sql_tool, "DB_PATH", fixture):
            for task in tasks:
                saver = InMemorySaver()
                agent = current.build_agent(model=InterruptedModel(model) if task.get("model_fault") else model,
                                            checkpointer=saver)
                config = {"configurable": {"thread_id": uuid4().hex}, "recursion_limit": 40}
                case = {"id": task["id"], "category": task["category"], "turns": []}
                report["cases"].append(case)
                for index, specification in enumerate(task["turns"], 1):
                    turn = {"turn": index, "question": specification["question"], "review": None,
                            "answer": "", "artifacts": [], "observations": []}
                    fault = specification.get("fault")
                    injected = [0]

                    def observed_sql(query):
                        observation = {"query": query}
                        turn["observations"].append(observation)
                        if fault == "sql_always" or (fault == "sql_once" and not injected[0]):
                            injected[0] += 1
                            observation["injected_error"] = True
                            raise sqlite3.OperationalError("Synthetic database temporarily unavailable.")
                        frame = original_sql(query)
                        observation["columns"] = list(frame.columns)
                        observation["rows"] = json.loads(frame.to_json(orient="records"))
                        return frame

                    print(f"RUN {task['id']} turn {index}", flush=True)
                    started = time.perf_counter()
                    charts = Path(temp) / task["id"] / str(index)
                    charts.mkdir(parents=True)
                    with patch.object(current, "run_sql_query", observed_sql), \
                         patch.object(visualization_tool, "OUTPUT_DIR", charts):
                        data = None if specification["question"] is None else {
                            "messages": [("user", specification["question"])]}
                        try:
                            result = invoke_analysis(agent, data, config, settings=ObservationSettings(), metrics_dir=None)
                            turn["answer"] = current.extract_final_text(result)
                            turn["metrics"] = result["metrics"]
                            turn["workflow"] = {k: result.get(k) for k in ["status", "tool_results",
                                "validation", "retry_count", "tool_rounds", "trace"]}
                            turn["messages"] = [{"type": m.type, "content": m.content,
                                "tool_calls": getattr(m, "tool_calls", []),
                                "usage": getattr(m, "usage_metadata", None)} for m in result.get("messages", [])]
                            for artifact in result.get("artifacts", []):
                                path = Path(artifact)
                                destination = ROOT / "outputs/acceptance_artifacts" / output.stem / task["id"] / path.name
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(path, destination)
                                turn["artifacts"].append({"path": str(destination.relative_to(ROOT)),
                                    "png_valid": path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                                    "bytes": path.stat().st_size, "sha256": digest(path)})
                        except Exception as error:
                            turn["error_type"] = type(error).__name__
                            turn["metrics"] = getattr(error, "analysis_metrics", {})
                            turn["checkpoint_pending"] = bool(agent.get_state(config).next)
                    turn["injected_sql_failures"] = injected[0]
                    turn["fixture_unchanged"] = digest(fixture) == initial_hash
                    turn["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    turn["checks"] = check_turn(turn, specification)
                    case["turns"].append(turn)
                    save(report, suite, output)
                    print(f"{'CHECKS_PASS' if all(turn['checks'].values()) else 'CHECKS_FAIL'} "
                          f"{task['id']} turn {index} {turn['duration_ms']} ms", flush=True)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["source_unchanged_during_run"] = source_hashes() == report["source_sha256"]
    save(report, suite, output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--suite", type=Path, default=ROOT / "eval/acceptance_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/acceptance_latest.json")
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to authorize live model calls.")
    if args.output.exists():
        parser.error("Choose a new output path; preserve earlier runs.")
    report = run_suite(args.suite, args.output, set(args.only) if args.only else None)
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["deterministic_passed_turns"] == report["summary"]["executed_turns"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
