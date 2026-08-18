from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def create_bar_chart(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    title: str,
    filename: str,
) -> str:
    """
    Create and save a bar chart from a DataFrame.
    Returns the saved file path.
    """
    plt.figure(figsize=(10, 6))
    plt.bar(df[x_col].astype(str), df[y_col])
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(y_col)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()

    file_path = OUTPUT_DIR / filename
    plt.savefig(file_path)
    plt.close()

    return str(file_path)


def create_line_chart(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    title: str,
    filename: str,
) -> str:
    """
    Create and save a line chart from a DataFrame.
    Returns the saved file path.
    """
    plt.figure(figsize=(10, 6))
    plt.plot(df[x_col].astype(str), df[y_col], marker="o")
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(y_col)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()

    file_path = OUTPUT_DIR / filename
    plt.savefig(file_path)
    plt.close()

    return str(file_path)


if __name__ == "__main__":
    import sys

    BASE_DIR = Path(__file__).resolve().parent.parent
    sys.path.append(str(BASE_DIR))

    from tools.sql_tool import run_sql_query

    test_query = """
    SELECT Country,
           SUM(Revenue) AS total_revenue
    FROM transactions
    GROUP BY Country
    ORDER BY total_revenue DESC
    LIMIT 10;
    """

    result = run_sql_query(test_query)

    chart_path = create_bar_chart(
        df=result,
        x_col="Country",
        y_col="total_revenue",
        title="Top 10 Countries by Revenue",
        filename="top_countries_by_revenue.png",
    )

    print("Visualization Tool test successful.")
    print("Chart saved at:", chart_path)