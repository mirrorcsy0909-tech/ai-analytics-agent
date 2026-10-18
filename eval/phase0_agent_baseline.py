"""Replay the frozen synthetic tasks through the current, unchanged Agent.

This script calls the configured model and uses its API credentials. It never
uses the retail database. Run only when a live baseline is intended:
    python3 eval/phase0_agent_baseline.py --run
Raw observations support review; this runner never invents a task success rate.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "eval"))
sys.dont_write_bytecode = True

# Keep the baseline local; do not inherit tracing destinations from .env.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from phase0_baseline import create_fixture, environment, source_hashes


def safe_json(value):
    return json.loads(json.dumps(value, default=lambda x: x.item() if hasattr(x, "item") else str(x)))


def error_details(error):
    # Provider error messages can echo credentials. Redact before persisting.
    details = {"type": type(error).__name__, "status_code": getattr(error, "status_code", None)}
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
        if isinstance(body, dict):
            for field in ["code", "param", "message"]:
                value = body.get(field)
                if not isinstance(value, str):
                    continue
                for name, secret in os.environ.items():
                    if any(word in name.upper() for word in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
                        if secret and len(secret) >= 6:
                            value = value.replace(secret, "[redacted]")
                value = re.sub(r"(?:sk|rk|pk)-[^\s'\"<>]+", "[redacted]", value)
                details[field] = value[:500]
    return details


def save(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe_json(report), indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Make live model calls.")
    parser.add_argument("--limit", type=int, default=12, help="Maximum task count (1-12).")
    parser.add_argument("--implementation", choices=["legacy", "workflow"], default="legacy")
    parser.add_argument("--observe", action="store_true", help="Record per-invocation metrics locally; tracing stays off.")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "outputs/phase0_agent_latest.json")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to make live model calls.")
    if not 1 <= args.limit <= 12:
        parser.error("--limit must be between 1 and 12.")

    tasks_path = BASE_DIR / "eval/agent_tasks_v1.json"
    benchmark = json.loads(tasks_path.read_text())
    report = {
        "schema_version": 1,
        "benchmark": benchmark["benchmark"],
        "benchmark_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Actual current Agent, synthetic fixture only; no retail data or remote tracing.",
        "implementation": args.implementation,
        "limits": {"request_timeout_seconds": 30, "model_max_retries": 0,
                   "max_output_tokens_per_call": 3000, "graph_recursion_limit": 32},
        "environment": environment(),
        "source_sha256": source_hashes(),
        "cases": [],
        "task_success_rate": None,
        "review_status": "pending",
    }
    save(report, args.output)
    try:
        from agent import analytics_agent as current
        from tools import sql_tool, visualization_tool

        model = current.build_model(timeout=30, max_retries=0, max_tokens=3000)
        report["configured_model"] = model.model_name
        report["api"] = "responses"
        report["reasoning"] = model.reasoning
        report["system_prompt_sha256"] = hashlib.sha256(current.SYSTEM_PROMPT.encode()).hexdigest()
        builder = current.build_legacy_agent if args.implementation == "legacy" else current.build_agent
        agent = builder(model=model)
    except Exception as error:
        report.update(status="setup_failed", error=error_details(error))
        save(report, args.output)
        print("SETUP FAILED:", type(error).__name__, flush=True)
        return 2

    original_sql = current.run_sql_query
    original_ab = current.run_ab_test
    original_bar = current.create_bar_chart
    original_line = current.create_line_chart
    observations = []

    def observed_sql(query):
        observation = {"type": "sql", "query": query}
        observations.append(observation)
        try:
            frame = original_sql(query)
            observation.update(columns=list(frame.columns), rows=json.loads(frame.to_json(orient="records")))
            return frame
        except Exception as error:
            observation["error"] = error_details(error)
            raise

    def observed_ab(*args, **kwargs):
        observation = {"type": "ab_test", "args": args, "kwargs": kwargs}
        observations.append(observation)
        try:
            result = original_ab(*args, **kwargs)
            observation["result"] = safe_json(result)
            return result
        except Exception as error:
            observation["error"] = error_details(error)
            raise

    def observed_chart(kind, function, *args, **kwargs):
        frame = kwargs.get("df", args[0] if args else None)
        observation = {"type": "chart", "kind": kind,
                       "data": json.loads(frame.to_json(orient="records")) if frame is not None else None}
        observations.append(observation)
        path = Path(function(*args, **kwargs))
        observation.update(filename=path.name, bytes=path.stat().st_size,
                           png_valid=path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        return str(path)

    with tempfile.TemporaryDirectory(prefix="analytics-agent-phase0-") as temp:
        root = Path(temp)
        fixture = root / "synthetic.db"
        create_fixture(fixture)
        before = hashlib.sha256(fixture.read_bytes()).hexdigest()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sql_tool, "DB_PATH", fixture))
            stack.enter_context(patch.object(current, "run_sql_query", observed_sql))
            stack.enter_context(patch.object(current, "run_ab_test", observed_ab))
            stack.enter_context(patch.object(current, "create_bar_chart",
                lambda *a, **kw: observed_chart("bar", original_bar, *a, **kw)))
            stack.enter_context(patch.object(current, "create_line_chart",
                lambda *a, **kw: observed_chart("line", original_line, *a, **kw)))
            for index, task in enumerate(benchmark["tasks"][:args.limit], 1):
                observations = []
                charts = root / task["id"]
                charts.mkdir()
                case = {"id": task["id"], "category": task["category"],
                        "question": task["question"], "expected": task["expected"],
                        "review": None}
                start = time.perf_counter()
                print(f"RUN {index}/{len(benchmark['tasks'])} {task['id']}", flush=True)
                with patch.object(visualization_tool, "OUTPUT_DIR", charts):
                    try:
                        data = {"messages": [{"role": "user", "content": task["question"]}]}
                        config = {"recursion_limit": 32, "configurable": {"thread_id": task["id"]}}
                        if args.observe:
                            from agent.observability import ObservationSettings, invoke_analysis
                            result = invoke_analysis(agent, data, config, settings=ObservationSettings(), metrics_dir=None)
                            case["metrics"] = result["metrics"]
                        else:
                            result = agent.invoke(data, config=config)
                        messages = result.get("messages", [])
                        case["messages"] = [{"type": m.type, "content": m.content,
                                             "tool_calls": getattr(m, "tool_calls", []),
                                             "name": getattr(m, "name", None),
                                             "usage": getattr(m, "usage_metadata", None),
                                             "model_name": getattr(m, "response_metadata", {}).get("model_name")}
                                            for m in messages]
                        case["answer"] = current.extract_final_text(result)
                        usage = [getattr(m, "usage_metadata", None) for m in messages]
                        case["token_usage"] = {k: sum(u.get(k, 0) for u in usage if u)
                                               for k in ["input_tokens", "output_tokens", "total_tokens"]}
                        case["status"] = "completed"
                        case["workflow"] = {key: result.get(key) for key in
                            ["status", "plan", "tool_results", "validation", "retry_count", "trace", "artifacts"]
                            if key in result}
                    except Exception as error:
                        case.update(status="error", error=error_details(error), token_usage=None)
                        if hasattr(error, "analysis_metrics"):
                            case["metrics"] = error.analysis_metrics
                case["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
                case["observations"] = observations
                case["fixture_unchanged"] = hashlib.sha256(fixture.read_bytes()).hexdigest() == before
                report["cases"].append(case)
                save(report, args.output)
                print(f"{case['status'].upper()} {task['id']} {case['duration_ms']} ms", flush=True)
                if case["status"] == "error":
                    # Avoid replaying all tasks against an unavailable provider.
                    report["stop_reason"] = "Stopped at first model/runtime error; remaining tasks not run."
                    break
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "completed" if len(report["cases"]) == len(benchmark["tasks"]) and all(
        c["status"] == "completed" for c in report["cases"]) else "incomplete"
    report["existing_source_unchanged"] = source_hashes() == report["source_sha256"]
    save(report, args.output)
    print("Report:", args.output, flush=True)
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
