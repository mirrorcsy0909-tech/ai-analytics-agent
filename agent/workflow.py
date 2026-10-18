"""Explicit, bounded analytics workflow with optional checkpoint persistence.

Phase 1 validation checks tool execution and artifacts. Statistical and semantic
answer validation belong to Phase 2; a completed graph is not an accuracy score.
"""

from pathlib import Path
import hashlib
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode


class AnalysisState(MessagesState):
    question: str
    plan: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    artifacts: list[str]
    retry_count: int
    tool_rounds: int
    last_batch_ok: bool
    status: str
    trace: list[str]
    final_answer: str
    answer_errors: list[str]


def message_text(message):
    content = message.content
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content
                     if isinstance(block, dict) and block.get("type") in {"text", "output_text"})


def build_workflow(*, model, tools, system_prompt, checkpointer=None,
                   max_retries=2, max_tool_rounds=6, answer_validator=None,
                   final_response_prompt=""):
    if max_retries < 0 or max_tool_rounds < 1:
        raise ValueError("Retry count must be nonnegative and tool rounds must be positive.")
    bound_model = model.bind_tools(tools)
    tool_node = ToolNode(tools, handle_tool_errors=True)

    def prepare(state):
        question = next((message_text(m) for m in reversed(state["messages"])
                         if isinstance(m, HumanMessage)), "")
        if not question.strip():
            raise ValueError("A nonempty user question is required.")
        # Only messages persist across turns. Per-analysis evidence and budgets reset.
        return {"question": question, "plan": [], "tool_results": [], "validation": [],
                "artifacts": [], "retry_count": 0, "tool_rounds": 0,
                "last_batch_ok": True, "status": "planning", "trace": ["prepare"],
                "final_answer": "", "answer_errors": []}

    def plan(state, config):
        prompt = system_prompt + (
            "\nWorkflow rules: Treat tool errors as failed evidence. Repair a failed "
            "tool call when possible; never claim its analysis succeeded. "
            "Do not fabricate replacement statistics when a tool fails."
        )
        if not state["last_batch_ok"]:
            prompt += f"\nA tool failed. This is recovery attempt {state['retry_count']} of {max_retries}."
        prompt += final_response_prompt
        if state.get("answer_errors"):
            prompt += "\nFinal response failed validation: " + "; ".join(state["answer_errors"])
        response = bound_model.invoke([SystemMessage(content=prompt), *state["messages"]], config=config)
        calls = getattr(response, "tool_calls", [])
        steps = [{"tool": call["name"], "arguments": call["args"], "call_id": call["id"]}
                 for call in calls]
        return {"messages": [response], "plan": state["plan"] + steps,
                "status": "executing" if calls else "responding",
                "trace": state["trace"] + ["plan"]}

    def execute(state, config):
        request = state["messages"][-1]
        result = tool_node.invoke({"messages": [request]}, config=config)
        messages = result["messages"]
        requested = {call["id"]: call for call in request.tool_calls}
        entries = []
        artifacts = list(state["artifacts"])
        for message in messages:
            call = requested[message.tool_call_id]
            content = message_text(message)
            # Existing wrappers return error strings rather than raising.
            failed = getattr(message, "status", "success") == "error" or content.startswith((
                "SQL query failed.", "A/B test failed.", "Invalid analysis_type."))
            evidence = getattr(message, "artifact", None)
            if isinstance(evidence, dict) and evidence.get("kind") == "error":
                failed = True
            if call["name"] == "create_retail_chart" and not failed:
                prefix = "Chart created successfully: "
                path = Path(evidence["path"]) if isinstance(evidence, dict) and evidence.get("kind") == "chart" else Path(content.removeprefix(prefix))
                valid_path = path.is_file()
                if isinstance(evidence, dict) and evidence.get("kind") == "chart" and valid_path:
                    valid_path = hashlib.sha256(path.read_bytes()).hexdigest() == evidence.get("png_sha256")
                if valid_path:
                    if str(path) not in artifacts:
                        artifacts.append(str(path))
                else:
                    failed = True
            entries.append({"call_id": message.tool_call_id, "tool": call["name"],
                            "arguments": call["args"], "content": content,
                            "status": "error" if failed else "success",
                            "evidence": evidence,
                            "round": state["tool_rounds"] + 1})
        paired = {m.tool_call_id for m in messages} == set(requested)
        return {"messages": messages, "tool_results": state["tool_results"] + entries,
                "artifacts": artifacts, "tool_rounds": state["tool_rounds"] + 1,
                "last_batch_ok": paired and all(e["status"] == "success" for e in entries),
                "trace": state["trace"] + ["execute"]}

    def validate(state):
        batch = [r for r in state["tool_results"] if r["round"] == state["tool_rounds"]]
        event = {"kind": "tool_execution", "round": state["tool_rounds"],
                 "passed": state["last_batch_ok"],
                 "failed_tools": [r["tool"] for r in batch if r["status"] == "error"]}
        return {"validation": state["validation"] + [event],
                "status": "validated" if event["passed"] else "tool_error",
                "trace": state["trace"] + ["validate"]}

    def retry(state):
        return {"retry_count": state["retry_count"] + 1, "status": "retrying",
                "trace": state["trace"] + ["retry"]}

    def fail(state):
        answer = ("I couldn't complete this analysis with the available tools. "
                  "Please check the inputs or try a narrower question. No verified result is available.")
        return {"messages": [AIMessage(content=answer)], "final_answer": answer,
                "status": "failed", "trace": state["trace"] + ["fail"]}

    def finalize(state):
        answer = message_text(state["messages"][-1]).strip()
        if not answer or not state["last_batch_ok"]:
            return fail(state)
        has_evidence = bool(state["tool_results"])
        if answer_validator is not None:
            checked = answer_validator(answer, state["tool_results"])
            event = {"kind": "claim_grounding", "passed": checked["passed"],
                     "errors": checked["errors"], "claims_checked": checked.get("claims_checked", 0),
                     "scope": "Evidence references, rendered values, units and declared A/B conclusion; not full semantic correctness."}
            if not checked["passed"]:
                return {"status": "answer_invalid", "answer_errors": checked["errors"],
                        "validation": state["validation"] + [event],
                        "trace": state["trace"] + ["finalize"]}
            answer = checked["answer"]
        else:
            event = {"kind": "claim_grounding", "passed": None, "scope": "No answer validator configured."}
        return {"final_answer": answer, "answer_errors": [], "status": "completed" if has_evidence else "responded",
                "validation": state["validation"] + [event, {"kind": "final_response",
                    "has_text": True, "has_tool_evidence": has_evidence,
                    "semantic_accuracy_checked": False}],
                "trace": state["trace"] + ["finalize"]}

    def after_finalize(state):
        if state["status"] != "answer_invalid":
            return END
        return "retry" if state["retry_count"] < max_retries else "fail"

    def after_plan(state):
        if not getattr(state["messages"][-1], "tool_calls", []):
            return "finalize"
        return "limit" if state["tool_rounds"] >= max_tool_rounds else "execute"

    def limit(state):
        # Close every proposed call so a later conversation turn has a valid
        # message ledger, even when execution is refused by the step budget.
        return {"messages": [ToolMessage(content="Tool execution limit reached; call was not executed.",
                    name=c["name"], tool_call_id=c["id"], status="error")
                    for c in state["messages"][-1].tool_calls],
                "last_batch_ok": False,
                "validation": state["validation"] + [{"kind": "execution_limit", "passed": False}],
                "trace": state["trace"] + ["limit"]}

    def after_validation(state):
        if state["last_batch_ok"]:
            return "planner"
        if state["tool_rounds"] >= max_tool_rounds:
            return "fail"
        return "retry" if state["retry_count"] < max_retries else "fail"

    graph = StateGraph(AnalysisState)
    for name, node in [("prepare", prepare), ("planner", plan), ("execute", execute),
                       ("validate", validate), ("retry", retry), ("fail", fail),
                       ("limit", limit), ("finalize", finalize)]:
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "planner")
    graph.add_conditional_edges("planner", after_plan, ["execute", "finalize", "limit"])
    graph.add_edge("execute", "validate")
    graph.add_conditional_edges("validate", after_validation, ["planner", "retry", "fail"])
    graph.add_edge("retry", "planner")
    graph.add_edge("limit", "fail")
    graph.add_edge("fail", END)
    graph.add_conditional_edges("finalize", after_finalize, [END, "retry", "fail"])
    return graph.compile(checkpointer=checkpointer)
