"""Offline checks of actual graph control flow, checkpoints and model settings."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.analytics_agent import extract_final_text
from agent.model_config import build_model
from agent.workflow import build_workflow


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.inputs = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        self.inputs.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@tool
def lookup(value: str) -> str:
    """Return a deterministic lookup result."""
    return value


def call(name="lookup", args=None, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args or {"value": "42"}, "id": call_id, "type": "tool_call"}])


class WorkflowTests(unittest.TestCase):
    def graph(self, model, tools=None, **kwargs):
        return build_workflow(model=model, tools=tools or [lookup], system_prompt="Use tools.", **kwargs)

    def test_success_records_plan_evidence_and_validation(self):
        graph = self.graph(ScriptedModel(call(), AIMessage(content="The value is 42.")))
        result = graph.invoke({"messages": [("user", "Look it up")]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_results"][0]["content"], "42")
        self.assertEqual(result["plan"][0]["tool"], "lookup")
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["trace"], ["prepare", "plan", "execute", "validate", "plan", "finalize"])
        self.assertFalse(result["validation"][-1]["semantic_accuracy_checked"])

    def test_wrapper_error_string_triggers_recovery(self):
        calls = []

        @tool
        def query_retail_database(sql_query: str) -> str:
            """Simulate an existing tool wrapper with one invalid query."""
            calls.append(sql_query)
            return "SQL query failed. Error: bad query" if sql_query == "bad" else "42"

        model = ScriptedModel(
            call("query_retail_database", {"sql_query": "bad"}, "bad"),
            call("query_retail_database", {"sql_query": "SELECT 42"}, "good"),
            AIMessage(content="42"))
        result = self.graph(model, [query_retail_database]).invoke({"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual([r["status"] for r in result["tool_results"]], ["error", "success"])
        self.assertEqual(calls, ["bad", "SELECT 42"])

    def test_exceptions_stop_after_retry_budget(self):
        executions = []

        @tool
        def broken(value: str) -> str:
            """Simulate an unavailable tool."""
            executions.append(value)
            raise RuntimeError("Unavailable")

        model = ScriptedModel(*(call("broken", call_id=str(i)) for i in range(3)))
        result = self.graph(model, [broken], max_retries=2).invoke({"messages": [("user", "Analyze")]})
        self.assertEqual(len(executions), 3)
        self.assertEqual(result["retry_count"], 2)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No verified result", result["final_answer"])

    def test_cannot_claim_success_after_failed_tool(self):
        @tool
        def broken(value: str) -> str:
            """Return a failure string like the SQL wrapper."""
            return "SQL query failed. Error: missing column"

        result = self.graph(ScriptedModel(call("broken"), AIMessage(content="Revenue is 999.")), [broken]).invoke(
            {"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("999", extract_final_text(result))

    def test_tool_budget_allows_final_answer_but_blocks_more_calls(self):
        result = self.graph(ScriptedModel(call(), AIMessage(content="42")), max_tool_rounds=1).invoke(
            {"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "completed")
        model = ScriptedModel(call(call_id="one"), call(call_id="two"))
        result = self.graph(model, max_tool_rounds=1).invoke({"messages": [("user", "Loop")]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["tool_results"]), 1)
        self.assertEqual(result["messages"][-2].tool_call_id, "two")
        self.assertEqual(result["messages"][-2].status, "error")

    def test_tool_free_reply_is_not_marked_verified_analysis(self):
        result = self.graph(ScriptedModel(AIMessage(content="Please provide the four counts."))).invoke(
            {"messages": [("user", "Run an A/B test")]})
        self.assertEqual(result["status"], "responded")
        self.assertFalse(result["validation"][-1]["has_tool_evidence"])

    def test_checkpoint_sessions_are_isolated_and_turn_evidence_resets(self):
        model = ScriptedModel(call(), AIMessage(content="42"), AIMessage(content="Need counts"),
                              AIMessage(content="Earlier value was 42"))
        graph = self.graph(model, checkpointer=InMemorySaver())
        one = {"configurable": {"thread_id": "one"}}
        two = {"configurable": {"thread_id": "two"}}
        graph.invoke({"messages": [("user", "Lookup")]}, config=one)
        second = graph.invoke({"messages": [("user", "A/B test")]}, config=two)
        self.assertEqual(len(second["messages"]), 2)
        self.assertEqual(second["tool_results"], [])
        followup = graph.invoke({"messages": [("user", "What was it?")]}, config=one)
        self.assertGreater(len(followup["messages"]), 2)
        self.assertEqual(followup["question"], "What was it?")
        self.assertEqual(followup["tool_results"], [])
        self.assertEqual(followup["retry_count"], 0)

    def test_interrupted_model_call_resumes_from_checkpoint(self):
        model = ScriptedModel(ConnectionError("Interrupted"), call(), AIMessage(content="42"))
        graph = self.graph(model, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "resume"}}
        with self.assertRaises(ConnectionError):
            graph.invoke({"messages": [("user", "Lookup")]}, config=config)
        self.assertEqual(graph.get_state(config).next, ("planner",))
        result = graph.invoke(None, config=config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sum(m.type == "human" for m in result["messages"]), 1)

    def test_resume_after_tools_does_not_repeat_completed_execution(self):
        model = ScriptedModel(call(), ConnectionError("Interrupted summary"), AIMessage(content="42"))
        graph = self.graph(model, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "resume-after-tools"}}
        with self.assertRaises(ConnectionError):
            graph.invoke({"messages": [("user", "Lookup")]}, config=config)
        self.assertEqual(len(graph.get_state(config).values["tool_results"]), 1)
        result = graph.invoke(None, config=config)
        self.assertEqual(len(result["tool_results"]), 1)
        self.assertEqual(result["trace"].count("execute"), 1)
        self.assertEqual(result["status"], "completed")

    def test_multiple_calls_keep_each_result_paired(self):
        first, second = call(call_id="one"), call(args={"value": "7"}, call_id="two")
        model = ScriptedModel(AIMessage(content="", tool_calls=first.tool_calls + second.tool_calls),
                              AIMessage(content="42 and 7"))
        result = self.graph(model).invoke({"messages": [("user", "Look up both")]})
        self.assertEqual([(r["call_id"], r["content"]) for r in result["tool_results"]],
                         [("one", "42"), ("two", "7")])
        self.assertEqual(result["tool_rounds"], 1)
        self.assertEqual(result["status"], "completed")

    def test_missing_chart_artifact_is_a_failed_execution(self):
        @tool
        def create_retail_chart(analysis_type: str) -> str:
            """Return a nonexistent chart path."""
            return "Chart created successfully: /nonexistent-phase1-chart.png"

        result = self.graph(ScriptedModel(call("create_retail_chart", {"analysis_type": "monthly"})),
                            [create_retail_chart], max_retries=0).invoke({"messages": [("user", "Plot")]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["artifacts"], [])

    def test_same_chart_path_is_reported_on_each_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "same.png"

            @tool
            def create_retail_chart(analysis_type: str) -> str:
                """Simulate two updates to the same chart path."""
                chart.write_bytes(b"synthetic-chart")
                return f"Chart created successfully: {chart}"

            model = ScriptedModel(call("create_retail_chart", {"analysis_type": "monthly"}),
                                  AIMessage(content="Chart ready"),
                                  call("create_retail_chart", {"analysis_type": "monthly"}, "second"),
                                  AIMessage(content="Chart updated"))
            graph = self.graph(model, [create_retail_chart], checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": "chart"}}
            for _ in range(2):
                result = graph.invoke({"messages": [("user", "Plot")]}, config=config)
                self.assertEqual(result["artifacts"], [str(chart)])

    def test_unknown_tool_fails_safely(self):
        result = self.graph(ScriptedModel(call("nonexistent")), max_retries=0).invoke(
            {"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "failed")

    def test_empty_reply_fails_safely(self):
        result = self.graph(ScriptedModel(AIMessage(content=""))).invoke({"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "failed")

    def test_invalid_budgets_and_question_are_rejected(self):
        with self.assertRaises(ValueError):
            self.graph(ScriptedModel(), max_tool_rounds=0)
        with self.assertRaises(ValueError):
            self.graph(ScriptedModel()).invoke({"messages": [("user", " ")]})

    def test_responses_blocks_do_not_leak_into_final_text(self):
        result = {"messages": [AIMessage(content=[
            {"type": "reasoning", "encrypted_content": "not-user-text"},
            {"type": "text", "text": "Result"}])]}
        self.assertEqual(extract_final_text(result), "Result")

    def test_model_config_preserves_model_and_uses_responses(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unused-offline-test-key",
                                    "OPENAI_MODEL": "openai:gpt-5.6", "OPENAI_REASONING_EFFORT": "medium"}):
            model = build_model()
        self.assertEqual(model.model_name, "gpt-5.6")
        self.assertTrue(model.use_responses_api)
        self.assertFalse(model.store)
        self.assertEqual(model.reasoning, {"effort": "medium"})


if __name__ == "__main__":
    unittest.main()
