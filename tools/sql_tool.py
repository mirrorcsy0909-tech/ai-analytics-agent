"""Bounded read-only SQLite analytics with AST and database enforcement."""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pandas as pd
import numpy as np
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "analytics.db"
MAX_ROWS = 1000
SAFE_FUNCTIONS = frozenset("""
    abs avg ceil ceiling char coalesce concat concat_ws count cume_dist date datetime
    dense_rank exp first_value floor format glob group_concat hex ifnull iif instr
    julianday lag last_value lead length like ln log log10 log2 lower ltrim max min
    mod nth_value ntile nullif percent_rank pow power printf rank replace round
    row_number rtrim sign sqrt strftime substr substring sum time total trim typeof
    unicode unixepoch upper
""".split())


def is_safe_select_query(query: str) -> bool:
    """Parse one read query, including CTEs; SQLite enforces policy at execution."""
    if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
        return False
    try:
        statements = [s for s in sqlglot.parse(query, read="sqlite") if s is not None]
        if len(statements) != 1:
            return False
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery)):
            return False
        if any(isinstance(n, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Pragma)) for n in tree.walk()):
            return False
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.db or table.catalog or table.name.lower() not in {"transactions", *ctes}:
                return False
        for function in tree.find_all(exp.Anonymous):
            if function.name.lower() not in SAFE_FUNCTIONS:
                return False
        return True
    except (SqlglotError, RecursionError, ValueError):
        return False


def _authorize(action, arg1, arg2, database, source):
    if action == sqlite3.SQLITE_READ:
        # SQLite's COUNT(*) optimization can report database=None, column="".
        return sqlite3.SQLITE_OK if arg1.lower() == "transactions" and database in {"main", None} else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (arg2 or arg1 or "").lower() in SAFE_FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY


def run_sql_query(query: str, *, max_rows: int = MAX_ROWS, timeout_seconds: float = 3.0) -> pd.DataFrame:
    if not is_safe_select_query(query):
        raise ValueError("Only one read-only SELECT over transactions or local CTEs is allowed.")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"max_rows must be an integer between 1 and {MAX_ROWS}.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive.")
    uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=timeout_seconds)) as conn:
        conn.execute("PRAGMA query_only = ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20_000)
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + timeout_seconds
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            cursor = conn.execute(query)
            columns = [column[0] for column in cursor.description]
            if len(set(columns)) != len(columns):
                raise ValueError("Use unique names for result columns.")
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as error:
            if "interrupted" in str(error).lower():
                raise TimeoutError("SQL execution exceeded its time budget.") from error
            raise
        if len(rows) > max_rows:
            raise ValueError(f"Query exceeds {max_rows} rows; aggregate the data or add LIMIT.")
    frame = pd.DataFrame.from_records(rows, columns=columns)
    for column in frame.select_dtypes(include="number"):
        if not np.isfinite(frame[column].dropna().to_numpy(dtype=float)).all():
            raise ValueError("Query produced nonfinite numeric results.")
    return frame
