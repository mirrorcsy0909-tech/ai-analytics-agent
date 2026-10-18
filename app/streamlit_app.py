import sys
from pathlib import Path
from uuid import uuid4

import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver

# Add project root to Python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

OUTPUT_DIR = BASE_DIR / "outputs"

from agent.analytics_agent import build_agent, extract_final_text
from agent.observability import invoke_analysis
from app.chart_history import capture_charts, merge_chart_history


def load_agent():
    """Reuse the graph only within its owning browser conversation."""
    if "analysis_agent" not in st.session_state:
        st.session_state.analysis_agent = build_agent(checkpointer=InMemorySaver())
    return st.session_state.analysis_agent


def main():
    st.set_page_config(
        page_title="AI Analytics Agent",
        page_icon="📊",
        layout="wide",
    )

    st.title("📊 AI Analytics Agent")
    st.write(
        "Ask questions about retail revenue, countries, products, customers, "
        "monthly trends, charts, or A/B testing."
    )

    st.info(
        "This app uses an LLM agent with tools for SQL analysis, visualization, "
        "and A/B testing. API usage may generate cost."
    )
    if "analysis_thread_id" not in st.session_state:
        st.session_state.analysis_thread_id = uuid4().hex

    with st.sidebar:
        if st.button("Clear conversation"):
            st.session_state.analysis_thread_id = uuid4().hex
            for key in ("analysis_agent", "last_analysis", "analysis_interrupted",
                        "analysis_error", "last_metrics", "analysis_charts", "chart_unavailable"):
                st.session_state.pop(key, None)
            st.session_state.analysis_question = ""
        st.caption("Clear starts a fresh conversation and removes its displayed charts.")
        st.header("Example Questions")

        st.markdown(
            """
            **SQL Analysis**
            - What are the top 5 countries by revenue?
            - Which products generated the most revenue?
            - Who are the top customers by revenue?

            **Visualization**
            - Create a chart for monthly revenue trend.
            - Plot the top countries by revenue.
            - Show a chart of top products.

            **A/B Testing**
            - Run an A/B test with 1200 conversions out of 10000 users in control and 1250 conversions out of 10000 users in treatment.
            """
        )

    question = st.text_area(
        "Ask your question:",
        placeholder="Example: What are the top 5 countries by revenue?",
        height=120,
        key="analysis_question",
    )

    run_button = st.button("Run Analysis")
    resume_button = (st.button("Retry interrupted analysis")
                     if st.session_state.get("analysis_interrupted") else False)

    if run_button or resume_button:
        if run_button and not question.strip():
            st.warning("Please enter a question first.")
            return

        st.session_state.pop("last_analysis", None)
        st.session_state.pop("analysis_error", None)
        st.session_state.pop("chart_unavailable", None)
        try:
            agent = load_agent()
            config = {"configurable": {"thread_id": st.session_state.analysis_thread_id},
                      "recursion_limit": 40}
            with st.spinner("Agent is analyzing your question..."):
                result = invoke_analysis(agent,
                    None if resume_button else {"messages": [{"role": "user", "content": question}]},
                    config=config,
                )
            st.session_state.last_analysis = result
            st.session_state.last_metrics = result["metrics"]
            st.session_state.analysis_interrupted = False
            charts, unavailable = capture_charts(result, OUTPUT_DIR)
            st.session_state.analysis_charts = merge_chart_history(
                st.session_state.get("analysis_charts", []), charts)
            st.session_state.chart_unavailable = unavailable
        except Exception as error:
            st.session_state.last_metrics = getattr(error, "analysis_metrics", None)
            # Only offer resume when a checkpoint actually has pending work.
            try:
                st.session_state.analysis_interrupted = bool(agent.get_state(config).next)
            except Exception:
                st.session_state.analysis_interrupted = False
            st.session_state.analysis_error = (
                "Analysis could not finish. Check your connection and model settings, then retry.")
            st.rerun()

    if st.session_state.get("analysis_error"):
        st.error(st.session_state.analysis_error)

    if st.session_state.get("last_analysis"):
        result = st.session_state.last_analysis
        st.subheader("Agent Answer")
        answer = extract_final_text(result)
        if result.get("status") == "failed":
            st.warning(answer)
        else:
            st.write(answer)

    if st.session_state.get("last_metrics"):
        metrics = st.session_state.last_metrics
        with st.expander("Run details"):
            st.write(f"Time: {metrics['duration_ms'] / 1000:.2f} seconds · "
                     f"Model calls: {metrics['model_calls']} · Tool calls: {metrics['tool_calls']}")
            usage = metrics.get("token_usage")
            st.write(f"Reported tokens: {usage['total_tokens']}" if usage else "Token usage unavailable")
            if usage and not metrics.get("usage_complete"):
                st.caption("Token usage is partial; some calls did not return usage.")
            st.caption(f"Run ID: {metrics['invocation_id']} · Trace: {metrics['langsmith_status']}")

    st.divider()

    st.subheader("Charts from this conversation")
    st.caption("Up to four recent charts. Start a new chart request to regenerate a result.")
    if st.session_state.get("chart_unavailable"):
        st.warning("A chart is unavailable or has changed. Run the chart request again.")
    charts = st.session_state.get("analysis_charts", [])
    if charts:
        for chart in charts:
            st.image(chart["data"], caption=chart["caption"])
    else:
        st.write("No charts in this conversation yet.")


if __name__ == "__main__":
    main()
