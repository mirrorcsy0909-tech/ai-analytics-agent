import sys
from pathlib import Path

# Add project root to Python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from tools.sql_tool import run_sql_query
from tools.visualization_tool import create_bar_chart, create_line_chart
from tools.ab_test_tool import run_ab_test


def get_analysis_for_question(question: str):
    """
    Convert a simple user question into an analysis type and SQL query.
    This is still rule-based.
    Later, we will replace this logic with an AI agent.
    """
    question = question.lower()

    if "ab test" in question or "a/b" in question or "conversion" in question or "treatment" in question:
        return "ab_test", None

    if "country" in question or "countries" in question:
        return "countries", """
        SELECT Country,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY Country
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

    if "month" in question or "monthly" in question or "trend" in question:
        return "monthly", """
        SELECT InvoiceMonth,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY InvoiceMonth
        ORDER BY InvoiceMonth;
        """

    if "product" in question or "products" in question:
        return "products", """
        SELECT Description,
               SUM(Revenue) AS total_revenue
        FROM transactions
        GROUP BY Description
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

    if "customer" in question or "customers" in question:
        return "customers", """
        SELECT CustomerID,
               SUM(Revenue) AS total_revenue
        FROM transactions
        WHERE CustomerID IS NOT NULL
        GROUP BY CustomerID
        ORDER BY total_revenue DESC
        LIMIT 10;
        """

    return None, None


def generate_business_insight(analysis_type: str, result):
    """
    Generate a simple business insight based on the analysis type and result.
    This is not AI yet. It is a rule-based insight generator.
    """

    if result.empty:
        return "No results were found for this question."

    if analysis_type == "countries":
        top_country = result.iloc[0]["Country"]
        top_revenue = result.iloc[0]["total_revenue"]

        return (
            f"{top_country} is the highest-revenue market, generating "
            f"{top_revenue:,.2f} in revenue among the top countries. "
            "This suggests that revenue is geographically concentrated, "
            "so the business may consider both retaining its strongest market "
            "and expanding in other countries to reduce market concentration risk."
        )

    if analysis_type == "products":
        top_product = result.iloc[0]["Description"]
        top_revenue = result.iloc[0]["total_revenue"]

        return (
            f"The top product is '{top_product}', generating {top_revenue:,.2f} in revenue. "
            "This product may be a key revenue driver, so the business should monitor inventory, "
            "avoid stockouts, and consider promoting similar high-performing products."
        )

    if analysis_type == "customers":
        top_customer = result.iloc[0]["CustomerID"]
        top_revenue = result.iloc[0]["total_revenue"]

        return (
            f"Customer {top_customer} is the highest-value customer in this result, "
            f"generating {top_revenue:,.2f} in revenue. "
            "This suggests an opportunity to build retention strategies, loyalty programs, "
            "or targeted offers for high-value customers."
        )

    if analysis_type == "monthly":
        peak_row = result.loc[result["total_revenue"].idxmax()]
        peak_month = peak_row["InvoiceMonth"]
        peak_revenue = peak_row["total_revenue"]

        return (
            f"The highest revenue month is {peak_month}, with {peak_revenue:,.2f} in revenue. "
            "This suggests potential seasonality in customer demand, so the business can plan "
            "inventory, staffing, and marketing campaigns around peak sales periods."
        )

    return "This result provides useful information for understanding business performance."


def generate_chart(analysis_type: str, result):
    """
    Generate and save a chart based on the analysis type.
    """

    if result.empty:
        return None

    if analysis_type == "countries":
        return create_bar_chart(
            df=result,
            x_col="Country",
            y_col="total_revenue",
            title="Top 10 Countries by Revenue",
            filename="top_countries_by_revenue.png",
        )

    if analysis_type == "products":
        return create_bar_chart(
            df=result,
            x_col="Description",
            y_col="total_revenue",
            title="Top 10 Products by Revenue",
            filename="top_products_by_revenue.png",
        )

    if analysis_type == "customers":
        return create_bar_chart(
            df=result,
            x_col="CustomerID",
            y_col="total_revenue",
            title="Top 10 Customers by Revenue",
            filename="top_customers_by_revenue.png",
        )

    if analysis_type == "monthly":
        return create_line_chart(
            df=result,
            x_col="InvoiceMonth",
            y_col="total_revenue",
            title="Monthly Revenue Trend",
            filename="monthly_revenue_trend.png",
        )

    return None


def print_ab_test_result():
    """
    Run a demo A/B test.
    Later, we can connect this to a real A/B testing dataset.
    """

    result = run_ab_test(
        control_conversions=1200,
        control_total=10000,
        treatment_conversions=1250,
        treatment_total=10000,
    )

    print("\nA/B Test Result:")
    print(f"Control conversion rate: {result['control_conversion_rate']:.4f}")
    print(f"Treatment conversion rate: {result['treatment_conversion_rate']:.4f}")
    print(f"Absolute lift: {result['absolute_lift']:.4f}")
    print(f"Relative lift: {result['relative_lift']:.2%}")
    print(f"Z-statistic: {result['z_statistic']:.4f}")
    print(f"P-value: {result['p_value']:.4f}")
    print(f"95% Confidence interval: {result['confidence_interval_95']}")
    print(f"Statistically significant: {result['statistically_significant']}")

    print("\nBusiness Recommendation:")
    print(result["recommendation"])
    print()


def main():
    print("Welcome to the Basic Analytics Assistant")
    print("Ask a question about countries, months, products, customers, or A/B testing.")
    print("Type 'exit' to quit.\n")

    while True:
        question = input("Ask your question: ")

        if question.lower() == "exit":
            print("Goodbye!")
            break

        analysis_type, sql_query = get_analysis_for_question(question)

        if analysis_type == "ab_test":
            print_ab_test_result()
            continue

        if not sql_query:
            print("Sorry, I don't know how to answer that yet.\n")
            continue

        result = run_sql_query(sql_query)

        print("\nSQL Query:")
        print(sql_query)

        print("\nResult:")
        print(result)

        print("\nBusiness Insight:")
        print(generate_business_insight(analysis_type, result))

        chart_path = generate_chart(analysis_type, result)

        if chart_path:
            print("\nChart saved at:")
            print(chart_path)

        print()


if __name__ == "__main__":
    main()