"""Per-invocation metrics and opt-in, payload-free LangSmith traces.

Use invoke_analysis at application boundaries. A graph can still be invoked
without telemetry for tests. Metrics never contain prompts, SQL, rows or answers.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langsmith import Client, tracing_context

BASE_DIR = Path(__file__).resolve().parents[1]
SAFE_NAMES = {"AnalyticsAnalysis", "LangGraph", "ChatOpenAI", "RunnableSequence",
              "prepare", "planner", "execute", "validate", "retry", "fail", "limit", "finalize",
              "tools", "describe_retail_schema", "query_retail_database", "create_retail_chart",
              "run_ab_test_analysis"}


@dataclass(frozen=True)
class ObservationSettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    project: str = "analytics-agent"
    endpoint: str = "https://api.smith.langchain.com"
    workspace_id: str | None = None

    @classmethod
    def from_env(cls):
        load_dotenv(BASE_DIR / ".env", override=False)
        return cls(
            enabled=os.getenv("ANALYTICS_LANGSMITH_TRACING", "false").lower() == "true",
            api_key=os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY", ""),
            project=os.getenv("LANGSMITH_PROJECT", "analytics-agent"),
            endpoint=os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
            workspace_id=os.getenv("LANGSMITH_WORKSPACE_ID"),
        )


def sanitize_trace_ops(runs):
    """Final allowlist before SDK background upload, including error traces.

    hide_inputs/outputs alone do not hide events, errors, serialized arguments or
    metadata. Keep only trace topology, times, safe names and numeric usage.
    """
    allowed = {"id", "trace_id", "parent_run_id", "dotted_order", "start_time", "end_time",
               "run_type", "session_name", "session_id", "reference_example_id"}
    clean = []
    for run in runs:
        item = {k: v for k, v in run.items() if k in allowed}
        if "name" in run:
            item["name"] = run["name"] if run["name"] in SAFE_NAMES else "component"
        if "inputs" in run:
            item["inputs"] = {}
        if "outputs" in run:
            item["outputs"] = {}
        if run.get("error"):
            item["error"] = "Component failed; details retained only by the application."
        metadata = run.get("extra", {}).get("metadata", {})
        item["extra"] = {"metadata": {"analytics_phase": 3, **{
            k: v for k, v in metadata.items()
            if k in {"ls_input_tokens", "ls_output_tokens", "ls_total_tokens"}
            and isinstance(v, (int, float)) and not isinstance(v, bool)
        }}}
        clean.append(item)
    return clean


class InvocationMetrics(BaseCallbackHandler):
    """Collect callbacks from this invocation only, including partial failures."""

    def __init__(self):
        self.lock = threading.RLock()
        self.pending = {}
        self.models = []
        self.tools = []

    def _start(self, run_id, kind, name):
        with self.lock:
            self.pending[run_id] = (kind, name, time.perf_counter())

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._start(run_id, "model", "model")

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._start(run_id, "model", "model")

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = serialized.get("name", "component")
        self._start(run_id, "tool", name if name in SAFE_NAMES else "component")

    def _end(self, run_id, error=None, usage=None):
        with self.lock:
            pending = self.pending.pop(run_id, None)
            if pending is None:
                return
            kind, name, start = pending
            record = {"name": name, "duration_ms": round((time.perf_counter() - start) * 1000, 3),
                      "status": "error" if error else "completed", "error_type": type(error).__name__ if error else None}
            if kind == "model":
                record["token_usage"] = usage
                self.models.append(record)
            else:
                self.tools.append(record)

    def on_llm_end(self, response, *, run_id, **kwargs):
        usages = [getattr(getattr(g, "message", None), "usage_metadata", None)
                  for batch in response.generations for g in batch]
        usages = [u for u in usages if u]
        usage = {k: sum(u.get(k, 0) for u in usages)
                 for k in ("input_tokens", "output_tokens", "total_tokens")} if usages else None
        self._end(run_id, usage=usage)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    def snapshot(self):
        with self.lock:
            models, tools = list(self.models), list(self.tools)
            usages = [m["token_usage"] for m in models if m["token_usage"] is not None]
            return {"model_calls": len(models), "tool_calls": len(tools),
                    "model_events": models, "tool_events": tools,
                    "token_usage": {k: sum(u[k] for u in usages) for k in
                                    ("input_tokens", "output_tokens", "total_tokens")} if usages else None,
                    "usage_complete": bool(models) and len(usages) == len(models),
                    "pending_callbacks": len(self.pending)}


def write_metrics(metrics, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (metrics["invocation_id"] + ".json")
    # Unique files avoid append races between concurrent Streamlit sessions.
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(metrics, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
    destination.chmod(0o600)


def invoke_analysis(agent, data, config=None, *, settings=None,
                    metrics_dir=BASE_DIR / "outputs/telemetry", client=None):
    """Invoke once; preserve graph exceptions and attach content-free metrics.

    Resume calls count only newly executed callbacks. workflow_retry_count is
    the current turn's checkpoint total, not a count of new retries on resume.
    Cloud failures must not change analysis results or trigger model retries.
    """
    settings = settings or ObservationSettings.from_env()
    meter = InvocationMetrics()
    config = dict(config or {})
    callbacks = config.get("callbacks")
    if callbacks is None or isinstance(callbacks, (list, tuple)):
        config["callbacks"] = [*(callbacks or []), meter]
    else:
        callbacks = callbacks.copy()
        callbacks.add_handler(meter, inherit=True)
        config["callbacks"] = callbacks
    config.setdefault("run_name", "AnalyticsAnalysis")
    trace_status = "disabled"
    trace_errors = []
    if settings.enabled:
        if not settings.api_key and client is None:
            trace_status = "disabled_missing_key"
        else:
            try:
                client = client or Client(
                    api_key=settings.api_key, api_url=settings.endpoint,
                    workspace_id=settings.workspace_id,
                    hide_inputs=True, hide_outputs=True, hide_metadata=True,
                    omit_traced_runtime_info=True, process_buffered_run_ops=sanitize_trace_ops,
                    run_ops_buffer_size=100, run_ops_buffer_timeout_ms=100,
                    tracing_error_callback=lambda error: trace_errors.append(type(error).__name__),
                    timeout_ms=2000,
                )
                from langchain_core.tracers import LangChainTracer
                tracer = LangChainTracer(client=client, project_name=settings.project)
                if isinstance(config["callbacks"], list):
                    config["callbacks"].append(tracer)
                else:
                    config["callbacks"].add_handler(tracer, inherit=True)
                trace_status = "submitted_unconfirmed"
            except Exception:
                trace_status = "disabled_setup_error"
    invocation_id = uuid4().hex
    started = time.perf_counter()
    metrics = {"schema_version": 1, "invocation_id": invocation_id,
               "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "scope": "One invocation; no prompt, SQL, result rows, answer or credentials stored.",
               "resumed": data is None, "status": "error", "error_type": None}
    result = None
    try:
        # Explicitly defeat ambient LANGSMITH/LANGCHAIN tracing when disabled.
        with tracing_context(enabled=trace_status == "submitted_unconfirmed", client=client,
                             project_name=settings.project):
            result = agent.invoke(data, config=config)
        metrics["status"] = result.get("status", "completed")
        return {**result, "metrics": metrics}
    except Exception as error:
        metrics["error_type"] = type(error).__name__
        error.analysis_metrics = metrics
        raise
    finally:
        metrics.update(meter.snapshot(), duration_ms=round((time.perf_counter() - started) * 1000, 3),
                       langsmith_status=trace_status)
        if result is not None:
            validations = result.get("validation", [])
            tools = result.get("tool_results", [])
            metrics.update(workflow_retry_count=result.get("retry_count", 0),
                           workflow_tool_errors=sum(t.get("status") == "error" for t in tools),
                           validation_failures=sum(v.get("passed") is False for v in validations))
        if trace_status == "submitted_unconfirmed":
            try:
                client.flush(timeout=2)
            except Exception:
                trace_errors.append("FlushError")
            if trace_errors:
                metrics["langsmith_status"] = "upload_error"
        metrics["local_record_status"] = "written" if metrics_dir is not None else "disabled"
        if metrics_dir is not None:
            try:
                write_metrics(metrics, metrics_dir)
            except (OSError, ValueError):
                metrics["local_record_status"] = "write_error"
