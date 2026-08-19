import sys
from pathlib import Path

# Add project root to Python path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from tools.sql_tool import run_sql_query, is_safe_select_query
from tools.visualization_tool import create_bar_chart, create_line_chart
from tools.ab_test_tool import run_ab_test


def test_sql_tool():
    """
    Test whether the SQL tool can run a valid SELECT query.
    """
    query = """
    SELECT Country,
           SUM(Revenue) AS total_revenue
    FROM transactions
    GROUP BY Country
    ORDER BY total_revenue DESC
    LIMIT 5;
    """

    result = run_sql_query(query)

    assert not result.empty, "SQL result should not be empty."
    assert "Country" in result.columns, "Result should include Country column."
    assert "total_revenue" in result.columns, "Result should include total_revenue column."
    assert len(result) <= 5, "Result should return at most 5 rows."

    print("✅ SQL Tool test passed.")


def test_sql_safety():
    """
    Test whether unsafe SQL queries are blocked.
    """
    unsafe_queries = [
        "DROP TABLE transactions;",
        "DELETE FROM transactions;",
        "UPDATE transactions SET Revenue = 0;",
        "ALTER TABLE transactions ADD COLUMN test TEXT;",
        "SELECT * FROM transactions; DROP TABLE transactions;",
    ]

    for query in unsafe_queries:
        assert not is_safe_select_query(query), f"Unsafe query was not blocked: {query}"

        try:
            run_sql_query(query)
            raise AssertionError(f"Unsafe query should have failed: {query}")
        except ValueError:
            pass

    print("✅ SQL Safety test passed.")


def test_visualization_tool():
    """
    Test whether visualization functions can generate chart files.
    """
    country_query = """
    SELECT Country,
           SUM(Revenue) AS total_revenue
    FROM transactions
    GROUP BY Country
    ORDER BY total_revenue DESC
    LIMIT 5;
    """

    country_result = run_sql_query(country_query)

    bar_chart_path = create_bar_chart(
        df=country_result,
        x_col="Country",
        y_col="total_revenue",
        title="Evaluation Test: Top Countries by Revenue",
        filename="eval_top_countries.png",
    )

    assert Path(bar_chart_path).exists(), "Bar chart file was not created."

    monthly_query = """
    SELECT InvoiceMonth,
           SUM(Revenue) AS total_revenue
    FROM transactions
    GROUP BY InvoiceMonth
    ORDER BY InvoiceMonth;
    """

    monthly_result = run_sql_query(monthly_query)

    line_chart_path = create_line_chart(
        df=monthly_result,
        x_col="InvoiceMonth",
        y_col="total_revenue",
        title="Evaluation Test: Monthly Revenue Trend",
        filename="eval_monthly_revenue.png",
    )

    assert Path(line_chart_path).exists(), "Line chart file was not created."

    print("✅ Visualization Tool test passed.")


def test_ab_test_tool():
    """
    Test whether the A/B testing tool returns expected statistics.
    """
    result = run_ab_test(
        control_conversions=1200,
        control_total=10000,
        treatment_conversions=1250,
        treatment_total=10000,
    )

    required_keys = [
        "control_conversion_rate",
        "treatment_conversion_rate",
        "absolute_lift",
        "relative_lift",
        "z_statistic",
        "p_value",
        "confidence_interval_95",
        "statistically_significant",
        "recommendation",
    ]

    for key in required_keys:
        assert key in result, f"Missing key in A/B test result: {key}"

    assert 0 <= result["control_conversion_rate"] <= 1
    assert 0 <= result["treatment_conversion_rate"] <= 1
    assert 0 <= result["p_value"] <= 1
    assert isinstance(result["confidence_interval_95"], tuple)
    assert len(result["confidence_interval_95"]) == 2

    print("✅ A/B Testing Tool test passed.")


def run_all_tests():
    """
    Run all evaluation tests.
    """
    print("Running AI Analytics Agent evaluation harness...\n")

    test_sql_tool()
    test_sql_safety()
    test_visualization_tool()
    test_ab_test_tool()

    print("\n🎉 All evaluation tests passed successfully.")


if __name__ == "__main__":
    run_all_tests()