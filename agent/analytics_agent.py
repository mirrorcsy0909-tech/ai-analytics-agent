import sys
from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool

# Add project root to Python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from tools.sql_tool import run_sql_query
from tools.visualization_tool import create_bar_chart, create_line_chart
from tools.ab_test_tool import run_ab_test
from agent.model_config import build_model
from agent.evidence import table_evidence, seal_evidence
from agent.answer_validation import GROUNDING_PROMPT, validate_answer


load_dotenv()


def dataframe_to_text(df, max_rows: int = 20) -> str:
    """
    Convert a pandas DataFrame to readable text for the LLM.
    """
    if df.empty:
        return "No rows returned."

    if len(df) > max_rows:
        df = df.head(max_rows)

    return df.to_string(index=False)


@tool
def describe_retail_schema() -> str:
    """
    Describe the SQLite retail database schema.
    Use this before writing SQL if you need to know available tables or columns.
    """
    return """
Database: analytics.db

Table: transactions

Columns:
- InvoiceNo: invoice number
- StockCode: product code
- Description: product description
- Quantity: number of units purchased
- InvoiceDate: transaction timestamp
- UnitPrice: unit price
- CustomerID: customer identifier
- Country: customer country
- Revenue: Quantity * UnitPrice
- InvoiceMonth: month extracted from invoice date

Important notes:
- Use the transactions table for retail sales analysis.
- Revenue is already available as a numeric column.
- Currency is unspecified. Never assume dollars or any other currency.
- Use only SELECT queries.
- For country, product, customer, and monthly analysis, aggregate Revenue with SUM(Revenue).
"""


@tool(response_format="content_and_artifact")
def query_retail_database(sql_query: str) -> tuple[str, dict]:
    """Run one read-only SELECT/CTE over transactions; aggregate or LIMIT to 1000 rows.

    Returns JSON evidence with displayed rows and an evidence ID. Currency is
    unspecified. Cite exact displayed columns and rows when answering.
    """
    try:
        return table_evidence(run_sql_query(sql_query), query=sql_query)
    except Exception as error:
        return f"SQL query failed. Error: {error}", {"kind": "error"}


@tool(response_format="content_and_artifact")
def create_retail_chart(analysis_type: str) -> tuple[str, dict]:
    """Create a standard chart and return its path, data and evidence ID.

    analysis_type: countries, products, customers, or monthly. Chart rows are
    included in JSON evidence; do not invent chart data or currency units.
    """
    analysis_type = analysis_type.lower().strip()
    definitions = {
        "countries": ("Country", "", "total_revenue DESC", "Top Countries by Revenue"),
        "products": ("Description", "", "total_revenue DESC", "Top Products by Revenue"),
        "customers": ("CustomerID", "WHERE CustomerID IS NOT NULL", "total_revenue DESC", "Top Customers by Revenue"),
        "monthly": ("InvoiceMonth", "", "InvoiceMonth", "Monthly Revenue Trend"),
    }
    if analysis_type not in definitions:
        return "Invalid analysis_type. Use countries, products, customers, or monthly.", {"kind": "error"}
    x_col, where, order, title = definitions[analysis_type]
    query = f"SELECT {x_col}, SUM(Revenue) AS total_revenue FROM transactions {where} GROUP BY {x_col} ORDER BY {order}"
    if analysis_type != "monthly":
        query += " LIMIT 10"
    frame = run_sql_query(query)
    chart_function = create_line_chart if analysis_type == "monthly" else create_bar_chart
    path = chart_function(df=frame, x_col=x_col, y_col="total_revenue", title=title,
                          filename=f"agent_{analysis_type}.png")
    return table_evidence(frame, kind="chart", query=query, path=path,
                          x_col=x_col, y_col="total_revenue", analysis_type=analysis_type)


@tool(response_format="content_and_artifact")
def run_ab_test_analysis(control_conversions: int, control_total: int,
                         treatment_conversions: int, treatment_total: int,
                         alpha: float = 0.05, alternative: str = "larger") -> tuple[str, dict]:
    """Compare independent conversion counts, default H1 treatment > control.

    Returns JSON evidence, inputs, test method, assumptions and statistics.
    Relative lift is null when the control rate is zero; report it as undefined.
    Sparse data uses Fisher exact; the separately labeled interval is two-sided.
    alternative is larger (treatment > control), smaller, or two-sided; follow
    the user's prespecified hypothesis and never switch direction after results.
    """
    try:
        counts = dict(control_conversions=control_conversions, control_total=control_total,
                      treatment_conversions=treatment_conversions, treatment_total=treatment_total)
        result = run_ab_test(**counts, alpha=alpha, alternative=alternative)
        return seal_evidence({"kind": "ab_test", "stats": {**result, **counts}})
    except Exception as error:
        return f"A/B test failed. Error: {error}", {"kind": "error"}


SYSTEM_PROMPT = """
You are an AI Analytics Agent for a data science portfolio project.

Your job is to help users analyze retail transaction data and A/B testing results.

Available capabilities:
1. Describe the retail database schema.
2. Run safe SELECT SQL queries against the transactions table.
3. Create standard retail charts for countries, products, customers, and monthly revenue.
4. Run A/B testing analysis using a two-proportion z-test.

Rules:
- Always use tools when the user asks for actual data analysis, charts, or A/B testing.
- Do not make up numbers. Use the SQL tool or A/B testing tool.
- Only use SELECT SQL queries.
- Read-only CTEs are allowed. Aggregate or LIMIT queries to at most 1000 rows.
- If the user asks for a chart, use create_retail_chart.
- If the user asks for A/B testing but does not provide counts, ask for:
  control conversions, control total, treatment conversions, treatment total.
- After tool results, summarize the finding in clear business language.
- Be concise, practical, and business-focused.
"""


def analytics_tools():
    return [describe_retail_schema, query_retail_database,
            create_retail_chart, run_ab_test_analysis]


def build_legacy_agent(*, model=None):
    """
    Build the LangChain AI agent.
    """
    return create_agent(
        model=model if model is not None else build_model(),
        tools=analytics_tools(),
        system_prompt=SYSTEM_PROMPT,
    )


def build_agent(*, model=None, checkpointer=None, max_retries=2, max_tool_rounds=6):
    from agent.workflow import build_workflow

    return build_workflow(
        model=model if model is not None else build_model(),
        tools=analytics_tools(), system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer, max_retries=max_retries, max_tool_rounds=max_tool_rounds,
        answer_validator=validate_answer, final_response_prompt=GROUNDING_PROMPT,
    )


def extract_final_text(agent_result) -> str:
    """
    Extract the final assistant message from the agent result.
    """
    if agent_result.get("final_answer"):
        return agent_result["final_answer"]

    messages = agent_result.get("messages", [])

    if not messages:
        return "No response returned by agent."

    final_message = messages[-1]
    content = final_message.content

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
                text_parts.append(block["text"])
        return "\n".join(text_parts)

    return str(content)


def main():
    from langgraph.checkpoint.memory import InMemorySaver
    from agent.observability import invoke_analysis

    agent = build_agent(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": uuid4().hex}, "recursion_limit": 40}

    print("Welcome to the AI Analytics Agent")
    print("Ask about revenue, countries, products, customers, monthly trends, charts, or A/B testing.")
    print("Type 'exit' to quit.\n")

    while True:
        question = input("Ask your question: ")

        if question.lower().strip() == "exit":
            print("Goodbye!")
            break

        try:
            result = invoke_analysis(agent,
                {"messages": [{"role": "user", "content": question}]}, config=config
            )
        except Exception:
            print("Analysis could not finish. Check your connection and model settings, then try again.")
            continue

        print("\nAgent Answer:")
        print(extract_final_text(result))
        print()


if __name__ == "__main__":
    main()
