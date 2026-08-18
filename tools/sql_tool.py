import sqlite3
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "analytics.db"


def is_safe_select_query(query: str) -> bool:
    """
    Allow only SELECT queries.
    This prevents the agent from accidentally modifying or deleting data.
    """
    query_lower = query.strip().lower()

    blocked_keywords = [
        "drop",
        "delete",
        "insert",
        "update",
        "alter",
        "create",
        "replace",
        "truncate",
    ]

    if not query_lower.startswith("select"):
        return False

    for keyword in blocked_keywords:
        if keyword in query_lower:
            return False

    return True


def run_sql_query(query: str) -> pd.DataFrame:
    """
    Run a SQL query against the analytics database and return a DataFrame.
    """
    if not is_safe_select_query(query):
        raise ValueError("Only safe SELECT queries are allowed.")

    conn = sqlite3.connect(DB_PATH)
    result = pd.read_sql_query(query, conn)
    conn.close()

    return result


if __name__ == "__main__":
    test_query = """
    SELECT Country,
           SUM(Revenue) AS total_revenue
    FROM transactions
    GROUP BY Country
    ORDER BY total_revenue DESC
    LIMIT 5;
    """

    df = run_sql_query(test_query)

    print("SQL Tool test successful.")
    print(df)