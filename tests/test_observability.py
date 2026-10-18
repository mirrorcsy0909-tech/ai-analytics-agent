"""Check real callback/SDK paths without network or model credentials."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import Client
from langsmith.schemas import Example
from pydantic import PrivateAttr

from agent.observability import ObservationSettings, invoke_analysis, sanitize_trace_ops
from agent.workflow import build_workflow
from eval.phase3_evaluate import (TASKS_PATH, dataset_examples, load_recording, run_evaluation,
                                  score_case, summarize, sync_dataset)
from eval.phase3_evaluate import numeric_usage, verify_cloud_results

ROOT = Path(__file__).resolve().parents[1]
DISABLED = ObservationSettings()


class CallbackModel(BaseChatModel):
    responses: list
    _index: int = PrivateAttr(0)

    @property
    def _llm_type(self):
        return "offline-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        value = self.responses[self._index]
        self._index += 1
        if isinstance(value, Exception):
            raise value
        return ChatResult(generations=[ChatGeneration(message=value)])


def reply(text="Done", tokens=3):
    return AIMessage(content=text, usage_metadata={"input_tokens": tokens, "output_tokens": 2, "total_tokens": tokens + 2})


@tool
def lookup(value: str) -> str:
    """Return a local test value."""
    return value


def request():
    return AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"value": "SECRET_ROW"}, "id": "call-1"}],
                     usage_metadata={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5})


def graph(responses, **kwargs):
    return build_workflow(model=CallbackModel(responses=responses), tools=[lookup], system_prompt="SECRET_PROMPT", **kwargs)


class ObservabilityTests(unittest.TestCase):
    def test_callbacks_count_current_invocation_not_checkpoint_history(self):
        agent = graph([reply(tokens=3), reply(tokens=7)], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "private-thread"}}
        with tempfile.TemporaryDirectory() as temp:
            first = invoke_analysis(agent, {"messages": [("user", "SECRET_QUESTION")]}, config, settings=DISABLED, metrics_dir=temp)
            second = invoke_analysis(agent, {"messages": [("user", "SECRET_FOLLOWUP")]}, config, settings=DISABLED, metrics_dir=temp)
            self.assertEqual(first["metrics"]["token_usage"]["total_tokens"], 5)
            self.assertEqual(second["metrics"]["token_usage"]["total_tokens"], 9)
            self.assertEqual(second["metrics"]["model_calls"], 1)
            self.assertEqual(len(second["messages"]), 4)
            self.assertTrue(second["metrics"]["usage_complete"])
            self.assertEqual(len(list(Path(temp).glob("*.json"))), 2)
            text = "".join(p.read_text() for p in Path(temp).glob("*.json"))
            for secret in ["SECRET_", "private-thread", "Done"]:
                self.assertNotIn(secret, text)
            self.assertNotIn("callbacks", config)

    def test_failure_keeps_partial_usage_and_resume_does_not_repeat_tool(self):
        agent = graph([request(), ConnectionError("SECRET_ERROR"), reply()], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "resume"}}
        with self.assertRaises(ConnectionError) as caught:
            invoke_analysis(agent, {"messages": [("user", "Analyze")]}, config, settings=DISABLED, metrics_dir=None)
        metrics = caught.exception.analysis_metrics
        self.assertEqual(metrics["model_calls"], 2)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["token_usage"]["total_tokens"], 5)
        self.assertFalse(metrics["usage_complete"])
        self.assertEqual(metrics["error_type"], "ConnectionError")
        self.assertNotIn("SECRET", json.dumps(metrics))
        result = invoke_analysis(agent, None, config, settings=DISABLED, metrics_dir=None)
        self.assertTrue(result["metrics"]["resumed"])
        self.assertEqual(result["metrics"]["tool_calls"], 0)
        self.assertEqual(result["metrics"]["model_calls"], 1)
        self.assertEqual(result["metrics"]["token_usage"]["total_tokens"], 5)

    def test_missing_usage_is_unknown_not_zero(self):
        result = invoke_analysis(graph([AIMessage(content="Done")]), {"messages": [("user", "Hi")]},
                                 settings=DISABLED, metrics_dir=None)
        self.assertIsNone(result["metrics"]["token_usage"])
        self.assertFalse(result["metrics"]["usage_complete"])

    def test_ambient_tracing_does_not_override_disabled_setting(self):
        with patch.dict("os.environ", {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true"}), \
             patch("agent.observability.Client") as client:
            result = invoke_analysis(graph([reply()]), {"messages": [("user", "Hi")]}, settings=DISABLED, metrics_dir=None)
        client.assert_not_called()
        self.assertEqual(result["metrics"]["langsmith_status"], "disabled")

    def test_missing_key_and_tracing_setup_failure_do_not_break_analysis(self):
        enabled = ObservationSettings(enabled=True)
        result = invoke_analysis(graph([reply()]), {"messages": [("user", "Hi")]}, settings=enabled, metrics_dir=None)
        self.assertEqual(result["metrics"]["langsmith_status"], "disabled_missing_key")
        with patch("agent.observability.Client", side_effect=RuntimeError("SECRET")):
            result = invoke_analysis(graph([reply()]), {"messages": [("user", "Hi")]},
                                     settings=ObservationSettings(enabled=True, api_key="fake"), metrics_dir=None)
        self.assertEqual(result["status"], "responded")
        self.assertEqual(result["metrics"]["langsmith_status"], "disabled_setup_error")

    def test_log_failure_does_not_change_result(self):
        with patch("agent.observability.write_metrics", side_effect=OSError("full")):
            result = invoke_analysis(graph([reply()]), {"messages": [("user", "Hi")]}, settings=DISABLED)
        self.assertEqual(result["status"], "responded")
        self.assertEqual(result["metrics"]["local_record_status"], "write_error")

    def test_trace_allowlist_removes_error_events_metadata_and_payloads(self):
        item = {"id": str(uuid4()), "name": "SECRET_NAME", "run_type": "llm", "inputs": {"key": "SECRET"},
                "outputs": {"text": "SECRET"}, "events": [{"data": "SECRET"}], "error": "SECRET_TRACEBACK",
                "serialized": {"kwargs": "SECRET"}, "attachments": {"secret": "SECRET"},
                "extra": {"metadata": {"thread_id": "SECRET", "ls_input_tokens": 3}, "runtime": "SECRET"}}
        clean = sanitize_trace_ops([item])[0]
        self.assertNotIn("SECRET", json.dumps(clean))
        self.assertEqual(clean["id"], item["id"])
        self.assertEqual(clean["extra"]["metadata"]["ls_input_tokens"], 3)
        self.assertEqual(item["name"], "SECRET_NAME")

    def test_actual_sdk_trace_topology_and_payload_filter_without_network(self):
        # Intercept after the real SDK buffer processor, immediately before transport.
        sent = []
        client = Client(api_key="fake-local-only", auto_batch_tracing=False,
                        info={}, hide_inputs=True, hide_outputs=True, hide_metadata=True,
                        omit_traced_runtime_info=True, process_buffered_run_ops=sanitize_trace_ops,
                        run_ops_buffer_size=100, run_ops_buffer_timeout_ms=100)
        with patch.object(Client, "_create_run", lambda self, run, **kw: sent.append(copy.deepcopy(run))), \
             patch.object(Client, "_update_run", lambda self, run, **kw: sent.append(copy.deepcopy(run))):
            result = invoke_analysis(graph([request(), reply("SECRET_ANSWER")]),
                                     {"messages": [("user", "SECRET_QUESTION")]},
                                     settings=ObservationSettings(enabled=True), client=client, metrics_dir=None)
            client.flush(timeout=2)
        self.assertEqual(result["metrics"]["langsmith_status"], "submitted_unconfirmed")
        self.assertTrue(sent)
        self.assertTrue(any(r.get("parent_run_id") for r in sent))
        self.assertTrue(any(r.get("run_type") == "llm" for r in sent))
        self.assertTrue(any(r.get("run_type") == "tool" for r in sent))
        self.assertNotIn("SECRET", json.dumps(sent, default=str))


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "eval/results/phase2_agent_workflow.json"
        cls.benchmark, cls.recording = load_recording(cls.path)
        cls.tasks = {t["id"]: t for t in cls.benchmark["tasks"]}

    def case(self, case_id):
        return copy.deepcopy(next(c for c in self.recording["cases"] if c["id"] == case_id))

    def test_recorded_phase2_checks_and_hash_bound_reviews(self):
        summary = summarize(self.benchmark, self.recording)
        self.assertEqual(summary["overall"]["deterministic_passed"], 12)
        self.assertEqual(summary["overall"]["strict_passed"], 12)

    def test_wrong_sql_value_or_rank_order_fails(self):
        for change in ["value", "order"]:
            case = self.case("sql_products")
            rows = case["workflow"]["tool_results"][-1]["evidence"]["rows"]
            if change == "value": rows[0]["TotalRevenue"] = 999
            else: rows.reverse()
            self.assertFalse(score_case(case, self.tasks[case["id"]])["expected_evidence"])

    def test_sql_dimension_alias_is_resolved_from_query_not_hardcoded_labels(self):
        case = self.case("sql_products")
        evidence = case["workflow"]["tool_results"][-1]["evidence"]
        evidence["query"] = evidence["query"].replace("Description", "Description AS arbitrary_label", 1)
        evidence["columns"] = ["arbitrary_label" if c == "Description" else c for c in evidence["columns"]]
        for row in evidence["rows"]:
            row["arbitrary_label"] = row.pop("Description")
        self.assertTrue(score_case(case, self.tasks[case["id"]])["expected_evidence"])

    def test_bad_chart_or_ab_counts_fails(self):
        case = self.case("chart_monthly")
        next(o for o in case["observations"] if o["type"] == "chart")["png_valid"] = False
        self.assertFalse(score_case(case, self.tasks[case["id"]])["expected_evidence"])
        case = self.case("ab_small_uplift")
        case["workflow"]["tool_results"][-1]["arguments"]["control_total"] = 1
        self.assertFalse(score_case(case, self.tasks[case["id"]])["expected_evidence"])

    def test_modified_answer_requires_fresh_review(self):
        case = self.case("sql_countries")
        case["answer"] += " invented statement"
        checks = score_case(case, self.tasks[case["id"]])
        self.assertIsNone(checks["recorded_answer_review"])
        self.assertIsNone(checks["strict_task_success"])

    def test_unreviewed_or_partial_run_does_not_get_strict_success_rate(self):
        recording = copy.deepcopy(self.recording)
        recording["cases"][0]["review"] = None
        self.assertIsNone(summarize(self.benchmark, recording)["strict_task_success_rate"])
        recording["cases"] = recording["cases"][:1]
        self.assertLess(summarize(self.benchmark, recording)["coverage"], 1)

    def test_invalid_reference_recording_is_rejected(self):
        for change in ["taskset", "duplicate", "question"]:
            recording = copy.deepcopy(self.recording)
            if change == "taskset": recording["benchmark_sha256"] = "wrong"
            elif change == "duplicate": recording["cases"].append(recording["cases"][0])
            else: recording["cases"][0]["question"] = "changed"
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "recording.json"
                path.write_text(json.dumps(recording))
                with self.assertRaises(ValueError): load_recording(path)

    def test_local_langsmith_evaluation_works_without_upload_or_model_calls(self):
        with patch.object(Client, "request_with_retries", side_effect=AssertionError("Network forbidden")) as network:
            result = run_evaluation(self.path)
        network.assert_not_called()
        self.assertEqual(result["langsmith"]["evaluated_cases"], 12)
        self.assertFalse(result["langsmith"]["cloud_verified"])

    def test_dataset_sync_is_idempotent_and_refuses_content_collision(self):
        name, examples = dataset_examples(self.benchmark)
        dataset = SimpleNamespace(id=examples[0].dataset_id)
        from unittest.mock import Mock
        client = Mock()
        client.has_dataset.return_value = False
        client.create_dataset.return_value = dataset
        client.list_examples.return_value = iter([])
        sync_dataset(client, self.benchmark)
        self.assertEqual(len(client.create_examples.call_args.kwargs["examples"]), 12)
        client.reset_mock()
        client.has_dataset.return_value = True
        client.read_dataset.return_value = dataset
        client.list_examples.return_value = iter(examples)
        sync_dataset(client, self.benchmark)
        client.create_examples.assert_not_called()
        bad = Example(id=examples[0].id, dataset_id=dataset.id, inputs={"question": "changed"}, outputs={}, metadata={})
        client.list_examples.return_value = iter([bad])
        with self.assertRaises(ValueError): sync_dataset(client, self.benchmark)
        client.create_examples.assert_not_called()

    def test_dataset_sync_accepts_server_default_split_but_rejects_changes(self):
        from unittest.mock import Mock
        _, examples = dataset_examples(self.benchmark)
        client = Mock()
        client.has_dataset.return_value = True
        client.read_dataset.return_value = SimpleNamespace(id=examples[0].dataset_id)
        remote = [e.model_copy(update={"metadata": {**e.metadata, "dataset_split": ["base"]}})
                  for e in examples]
        client.list_examples.return_value = iter(remote)
        sync_dataset(client, self.benchmark)
        client.create_examples.assert_not_called()
        self.assertEqual(remote[0].metadata["dataset_split"], ["base"])
        for metadata in ({**examples[0].metadata, "dataset_split": ["changed"]},
                         {**examples[0].metadata, "unexpected": "changed"}):
            changed = remote.copy()
            changed[0] = examples[0].model_copy(update={"metadata": metadata})
            client.list_examples.return_value = iter(changed)
            with self.assertRaises(ValueError):
                sync_dataset(client, self.benchmark)
        client.create_examples.assert_not_called()

    def test_cloud_verification_rejects_submission_without_persisted_feedback(self):
        from unittest.mock import Mock
        client = Mock()
        output = {"task_id": "sql_countries", "checks": {"tool_contract": True}}
        row = {"run": SimpleNamespace(id=uuid4(), outputs=output),
               "evaluation_results": {"results": [SimpleNamespace(key="tool_contract", score=1)]}}
        client.read_run.return_value = SimpleNamespace(outputs=output)
        client.list_feedback.return_value = []
        with self.assertRaises(RuntimeError): verify_cloud_results(client, [row], max_attempts=1)
        client.list_feedback.return_value = [SimpleNamespace(key="tool_contract", score=1)]
        verify_cloud_results(client, [row])
        client.read_run.return_value = SimpleNamespace(outputs={})
        with self.assertRaises(RuntimeError): verify_cloud_results(client, [row], max_attempts=1)

    def test_cloud_verification_waits_for_run_and_feedback_visibility(self):
        from unittest.mock import Mock
        from langsmith.utils import LangSmithNotFoundError
        client = Mock()
        output = {"task_id": "sql_countries", "checks": {"tool_contract": True}}
        row = {"run": SimpleNamespace(id=uuid4(), outputs=output),
               "evaluation_results": {"results": [SimpleNamespace(key="tool_contract", score=1)]}}
        client.read_run.side_effect = [LangSmithNotFoundError("Not indexed yet"),
                                      SimpleNamespace(outputs=output), SimpleNamespace(outputs=output)]
        client.list_feedback.side_effect = [[], [SimpleNamespace(key="tool_contract", score=1)]]
        with patch("eval.phase3_evaluate.time.sleep") as sleep:
            verify_cloud_results(client, [row], max_attempts=3)
        self.assertEqual(client.read_run.call_count, 3)
        self.assertEqual(client.list_feedback.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_cloud_verification_stops_when_runs_never_appear(self):
        from unittest.mock import Mock
        from langsmith.utils import LangSmithNotFoundError
        client = Mock()
        row = {"run": SimpleNamespace(id=uuid4(), outputs={}),
               "evaluation_results": {"results": []}}
        client.read_run.side_effect = LangSmithNotFoundError("Not indexed yet")
        with patch("eval.phase3_evaluate.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                verify_cloud_results(client, [row], max_attempts=2)
        self.assertEqual(client.read_run.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        client.list_feedback.assert_not_called()

    def test_cloud_usage_allowlist_excludes_extra_text_fields(self):
        self.assertEqual(numeric_usage({"total_tokens": 10, "secret": "SECRET", "input_tokens": True}),
                         {"total_tokens": 10})


if __name__ == "__main__":
    unittest.main()
