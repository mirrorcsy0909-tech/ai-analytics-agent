import os
import sys
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
- Use only SELECT queries.
- For country, product, customer, and monthly analysis, aggregate Revenue with SUM(Revenue).
"""


@tool
def query_retail_database(sql_query: str) -> str:
    """
    Run a safe SELECT SQL query against the retail transactions database.

    Input should be a valid SQLite SELECT query.
    Use this for questions about revenue, countries, products, customers, and monthly trends.
    Do not use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, or other write operations.
    """
    try:
        result = run_sql_query(sql_query)
        return dataframe_to_text(result)
    except Exception as error:
        return f"SQL query failed. Error: {error}"


@tool
def create_retail_chart(analysis_type: str) -> str:
    """
    Create and save a chart for a standard retail analysis.

    analysis_type must be one of:
    - countries
    - products
    - customers
    - monthly

    Use this when the user asks to show, plot, chart, or visualize retail performance.
    """
    analysis_type = analysis_type.lower().strip()

    if analysis_type == "countries":
        query = """
        SELECT Country,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY Country
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

        result = run_sql_query(query)

        chart_path = create_bar_chart(
            df=result,
            x_col="Country",
            y_col="total_revenue",
            title="Top 10 Countries by Revenue",
            filename="agent_top_countries_by_revenue.png",
        )

        return f"Chart created successfully: {chart_path}"

    if analysis_type == "products":
        query = """
        SELECT Description,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY Description
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

        result = run_sql_query(query)

        chart_path = create_bar_chart(
            df=result,
            x_col="Description",
            y_col="total_revenue",
            title="Top 10 Products by Revenue",
            filename="agent_top_products_by_revenue.png",
        )

        return f"Chart created successfully: {chart_path}"

    if analysis_type == "customers":
        query = """
        SELECT CustomerID,
               SUM(Revenue) AS total_revenue
        FROM transactions
        WHERE CustomerID IS NOT NULL
        GROUP BY CustomerID
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

        result = run_sql_query(query)

        chart_path = create_bar_chart(
            df=result,
            x_col="CustomerID",
            y_col="total_revenue",
            title="Top 10 Customers by Revenue",
            filename="agent_top_customers_by_revenue.png",
        )

        return f"Chart created successfully: {chart_path}"

    if analysis_type == "monthly":
        query = """
        SELECT InvoiceMonth,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY InvoiceMonth
        ORDER BY InvoiceMonth;
        """

        result = run_sql_query(query)

        chart_path = create_line_chart(
            df=result,
            x_col="InvoiceMonth",
            y_col="total_revenue",
            title="Monthly Revenue Trend",
            filename="agent_monthly_revenue_trend.png",
        )

        return f"Chart created successfully: {chart_path}"

    return "Invalid analysis_type. Use one of: countries, products, customers, monthly."


@tool
def run_ab_test_analysis(
    control_conversions: int,
    control_total: int,
    treatment_conversions: int,
    treatment_total: int,
) -> str:
    """
    Run a two-proportion z-test for an A/B test.

    Use this when the user provides control and treatment conversion counts.
    Returns conversion rates, lift, z-statistic, p-value, 95% confidence interval,
    statistical significance, and a business recommendation.
    """
    try:
        result = run_ab_test(
            control_conversions=control_conversions,
            control_total=control_total,
            treatment_conversions=treatment_conversions,
            treatment_total=treatment_total,
        )

        ci_lower, ci_upper = result["confidence_interval_95"]

        return f"""
A/B Test Result:
- Control conversion rate: {result['control_conversion_rate']:.4f}
- Treatment conversion rate: {result['treatment_conversion_rate']:.4f}
- Absolute lift: {result['absolute_lift']:.4f}
- Relative lift: {result['relative_lift']:.2%}
- Z-statistic: {result['z_statistic']:.4f}
- P-value: {result['p_value']:.4f}
- 95% confidence interval: ({ci_lower:.4f}, {ci_upper:.4f})
- Statistically significant: {result['statistically_significant']}

Business recommendation:
{result['recommendation']}
"""
    except Exception as error:
        return f"A/B test failed. Error: {error}"


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
- If the user asks for a chart, use create_retail_chart.
- If the user asks for A/B testing but does not provide counts, ask for:
  control conversions, control total, treatment conversions, treatment total.
- After tool results, summarize the finding in clear business language.
- Be concise, practical, and business-focused.
"""


def build_agent():
    """
    Build the LangChain AI agent.
    """
    model = os.getenv("OPENAI_MODEL", "openai:gpt-5.5")

    return create_agent(
        model=model,
        tools=[
            describe_retail_schema,
            query_retail_database,
            create_retail_chart,
            run_ab_test_analysis,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


def extract_final_text(agent_result) -> str:
    """
    Extract the final assistant message from the agent result.
    """
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
            if isinstance(block, dict) and "text" in block:
                text_parts.append(block["text"])
            else:
                text_parts.append(str(block))
        return "\n".join(text_parts)

    return str(content)


def main():
    agent = build_agent()

    print("Welcome to the AI Analytics Agent")
    print("Ask about revenue, countries, products, customers, monthly trends, charts, or A/B testing.")
    print("Type 'exit' to quit.\n")

    while True:
        question = input("Ask your question: ")

        if question.lower().strip() == "exit":
            print("Goodbye!")
            break

        result = agent.invoke(
            {"messages": [{"role": "user", "content": question}]}
        )

        print("\nAgent Answer:")
        print(extract_final_text(result))
        print()


if __name__ == "__main__":
    main()