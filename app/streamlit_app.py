import sys
from pathlib import Path

import streamlit as st

# Add project root to Python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

OUTPUT_DIR = BASE_DIR / "outputs"

from agent.analytics_agent import build_agent, extract_final_text


@st.cache_resource
def load_agent():
    """
    Load the LangChain agent once and reuse it.
    This avoids rebuilding the agent every time Streamlit refreshes.
    """
    return build_agent()


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


def get_new_charts(before_files, after_files):
    """
    Identify newly created chart files after the agent runs.
    """
    before_set = set(before_files)
    return [file for file in after_files if file not in before_set]


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

    with st.sidebar:
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

    agent = load_agent()

    question = st.text_area(
        "Ask your question:",
        placeholder="Example: What are the top 5 countries by revenue?",
        height=120,
    )

    run_button = st.button("Run Analysis")

    if run_button:
        if not question.strip():
            st.warning("Please enter a question first.")
            return

        before_charts = get_png_files()

        with st.spinner("Agent is analyzing your question..."):
            result = agent.invoke(
                {"messages": [{"role": "user", "content": question}]}
            )

            answer = extract_final_text(result)

        after_charts = get_png_files()
        new_charts = get_new_charts(before_charts, after_charts)

        st.subheader("Agent Answer")
        st.write(answer)

        if new_charts:
            st.subheader("Generated Chart")
            for chart_path in new_charts:
                st.image(str(chart_path), caption=chart_path.name)

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