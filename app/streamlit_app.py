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


@st.cache_resource
def load_agent():
    """
    Load the LangChain agent once and reuse it.
    This avoids rebuilding the agent every time Streamlit refreshes.
    """
    return build_agent(checkpointer=InMemorySaver())


def get_png_files():
    """
    Return all PNG charts from the outputs folder.
    """
    if not OUTPUT_DIR.exists():
        return []

    return sorted(
        OUTPUT_DIR.glob("*.png"),
        key=lambda file: file.stat().st_mtime,
        reverse=True,
    )


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
            st.session_state.pop("last_analysis", None)
            st.session_state.pop("analysis_interrupted", None)
            st.session_state.pop("analysis_error", None)
            st.session_state.pop("last_metrics", None)
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

        new_charts = [Path(path) for path in result.get("artifacts", []) if Path(path).is_file()]
        if new_charts:
            st.subheader("Generated Chart")
            for chart_path in new_charts:
                st.image(str(chart_path), caption=chart_path.name)

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

    st.subheader("Recent Generated Charts")

    recent_charts = get_png_files()[:4]

    if recent_charts:
        for chart_path in recent_charts:
            st.image(str(chart_path), caption=chart_path.name)
    else:
        st.write("No charts generated yet.")


if __name__ == "__main__":
    main()
