"""Measure the unchanged tools offline; failures are Phase 2 work, not hidden.

Run from the repository root: python3 eval/phase0_baseline.py
No model, API key, .env file, or external service is used. Historical tests use
a temporary backup of the local database. Boundary cases use synthetic data.
Exit code 1 means at least one recorded requirement failed; see the JSON report.
"""

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import math
import platform
import runpy
import sqlite3
import sys
import tempfile
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

import pandas as pd

from tools import ab_test_tool, sql_tool, visualization_tool


def require(condition, message):
    # Keep checks active even if Python is started with -O.
    if not condition:
        raise AssertionError(message)


def close(actual, expected, label):
    require(math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-12),
            f"{label}: expected {expected}, got {actual}")


def require_value_error(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("Expected an explicit ValueError for invalid input.")


def create_fixture(path):
    """Eight invented transactions, including a refund and a missing customer."""
    rows = [
        ("1001", "P1", "Red mug", 2, "2024-01-03", 10., 1., "UK", 20., "2024-01"),
        ("1002", "P2", "Blue bowl", 3, "2024-01-05", 5., 2., "US", 15., "2024-01"),
        ("1003", "P1", "Red mug", 1, "2024-02-02", 10., 1., "UK", 10., "2024-02"),
        ("1004", "P3", "Laptop case", 3, "2024-02-04", 20., None, "DE", 60., "2024-02"),
        ("C1005", "P2", "Blue bowl", -1, "2024-02-06", 5., 2., "US", -5., "2024-02"),
        ("1006", "P2", "Blue bowl", 4, "2024-03-03", 5., 3., "US", 20., "2024-03"),
        ("1007", "P1", "Red mug", 1, "2025-01-03", 10., 4., "UK", 10., "2025-01"),
        ("1008", "P4", "Update guide", 2, "2025-01-05", 4., 4., "UK", 8., "2025-01"),
    ]
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE transactions (
            InvoiceNo TEXT, StockCode TEXT, Description TEXT, Quantity INTEGER,
            InvoiceDate TEXT, UnitPrice REAL, CustomerID REAL, Country TEXT,
            Revenue REAL, InvoiceMonth TEXT)""")
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()


def check_sql(query, columns, expected_rows):
    result = sql_tool.run_sql_query(query)
    require(list(result.columns) == columns, "Unexpected result columns.")
    actual = [list(row) for row in result.itertuples(index=False, name=None)]
    require(actual == expected_rows, f"Expected {expected_rows!r}, got {actual!r}")


def check_guard(query, expected):
    actual = sql_tool.is_safe_select_query(query)
    require(actual is expected,
            f"Expected SQL guard to return {expected}, got {actual}: {query}")


def check_ab(counts, alternative="larger"):
    """Independent pooled-z reference uses math.erfc, not statsmodels."""
    cc, ct, tc, tt = counts
    result = ab_test_tool.run_ab_test(cc, ct, tc, tt, alternative=alternative)
    control, treatment = cc / ct, tc / tt
    lift = treatment - control
    pooled = (cc + tc) / (ct + tt)
    z = lift / math.sqrt(pooled * (1 - pooled) * (1 / ct + 1 / tt))
    upper_tail = .5 * math.erfc(z / math.sqrt(2))
    expected_p = upper_tail if alternative == "larger" else math.erfc(abs(z) / math.sqrt(2))
    for key, expected in {
        "control_conversion_rate": control,
        "treatment_conversion_rate": treatment,
        "absolute_lift": lift,
        "relative_lift": lift / control,
        "z_statistic": z,
        "p_value": expected_p,
    }.items():
        close(result[key], expected, key)
    se = math.sqrt(treatment * (1 - treatment) / tt + control * (1 - control) / ct)
    low, high = result["confidence_interval_95"]
    close(low, lift - 1.959963984540054 * se, "CI lower")
    close(high, lift + 1.959963984540054 * se, "CI upper")
    require(bool(result["statistically_significant"]) == (expected_p < .05),
            "Significance flag differs from the p-value threshold.")


def check_zero_control():
    result = ab_test_tool.run_ab_test(0, 1000, 10, 1000)
    require(result["relative_lift"] is None,
            "Relative lift must explicitly be undefined when the control rate is zero.")
    require(math.isfinite(result["p_value"]), "The p-value must be finite.")


def check_chart(kind):
    frame = pd.DataFrame({"label": ["2024-01", "2024-02", "2025-01"],
                          "revenue": [35., 65., 18.]})
    function = (visualization_tool.create_bar_chart if kind == "bar"
                else visualization_tool.create_line_chart)
    draw_name = "bar" if kind == "bar" else "plot"
    with patch.object(visualization_tool.plt, draw_name,
                      wraps=getattr(visualization_tool.plt, draw_name)) as draw:
        path = Path(function(frame, "label", "revenue", "Synthetic baseline", f"{kind}.png"))
        x_values, y_values = draw.call_args.args[:2]
        require(list(x_values) == list(frame["label"]), "Chart changed category order.")
        require(list(y_values) == list(frame["revenue"]), "Chart values differ from data.")
    with path.open("rb") as image:
        require(image.read(8) == b"\x89PNG\r\n\x1a\n", "Invalid PNG signature.")
    require(path.stat().st_size > 1000, "Chart file is unexpectedly small.")


def check_missing_column():
    try:
        visualization_tool.create_bar_chart(pd.DataFrame({"x": ["a"]}),
                                             "x", "missing", "Missing", "missing.png")
    except (KeyError, ValueError):
        return
    raise AssertionError("Missing column should produce an explicit error.")


def check_chart_path():
    frame = pd.DataFrame({"x": ["a"], "y": [1.]})
    try:
        path = visualization_tool.create_bar_chart(frame, "x", "y", "Path", "../escaped.png")
    except ValueError:
        return
    require(Path(path).resolve().is_relative_to(visualization_tool.OUTPUT_DIR.resolve()),
            "Filename escaped the configured chart output directory.")


def source_hashes():
    files = [p for folder in ["agent", "app", "tools", "scripts"]
             for p in sorted((BASE_DIR / folder).glob("*.py"))]
    files += [BASE_DIR / "eval/eval_harness.py", BASE_DIR / "requirements.txt"]
    return {str(p.relative_to(BASE_DIR)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files}


def environment():
    versions = {}
    for name in ["pandas", "numpy", "matplotlib", "scipy", "statsmodels", "streamlit",
                 "python-dotenv", "langchain", "langchain-openai", "langgraph", "langsmith", "sqlglot"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "executable": sys.executable, "packages": versions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=sql_tool.DB_PATH)
    parser.add_argument("--output", type=Path,
                        default=BASE_DIR / "outputs/phase0_latest.json")
    args = parser.parse_args()
    results = []
    before_hashes = source_hashes()

    def record(case_id, category, function):
        start = time.perf_counter()
        case = {"id": case_id, "category": category}
        with warnings.catch_warnings(record=True) as caught, contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("always")
            try:
                function()
                case["status"] = "passed"
            except Exception as error:
                case.update(status="failed", error_type=type(error).__name__, error=str(error))
            finally:
                visualization_tool.plt.close("all")
            case["warnings"] = [str(w.message) for w in caught]
        case["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
        results.append(case)
        print(f"{case['status'].upper():6} {category}/{case_id}")

    with tempfile.TemporaryDirectory(prefix="analytics-phase0-") as temp:
        root = Path(temp)
        charts = root / "charts"
        charts.mkdir()
        with patch.object(visualization_tool, "OUTPUT_DIR", charts):
            if args.database.is_file():
                copy_path = root / "historical.db"
                with contextlib.closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    with contextlib.closing(sqlite3.connect(copy_path)) as target:
                        source.backup(target)
                with contextlib.closing(sqlite3.connect(copy_path)) as conn:
                    database_info = {"available": True,
                                     "transaction_rows": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]}
                harness = runpy.run_path(str(BASE_DIR / "eval/eval_harness.py"))
                with patch.object(sql_tool, "DB_PATH", copy_path):
                    for name in ["test_sql_tool", "test_sql_safety", "test_visualization_tool", "test_ab_test_tool"]:
                        record(name, "original_harness", harness[name])
            else:
                database_info = {"available": False, "transaction_rows": None}
                for name in ["test_sql_tool", "test_sql_safety", "test_visualization_tool", "test_ab_test_tool"]:
                    results.append({"id": name, "category": "original_harness", "status": "skipped",
                                    "reason": "Local database unavailable; synthetic checks run separately."})

            fixture = root / "fixture.db"
            create_fixture(fixture)
            with patch.object(sql_tool, "DB_PATH", fixture):
                cases = [
                    ("country_totals", "SELECT Country, SUM(Revenue) AS revenue FROM transactions GROUP BY Country ORDER BY revenue DESC",
                     ["Country", "revenue"], [["DE", 60.], ["UK", 48.], ["US", 30.]]),
                    ("product_totals", "SELECT Description, SUM(Revenue) AS revenue FROM transactions GROUP BY Description ORDER BY revenue DESC",
                     ["Description", "revenue"], [["Laptop case", 60.], ["Red mug", 40.], ["Blue bowl", 30.], ["Update guide", 8.]]),
                    ("customers_exclude_null", "SELECT CustomerID, SUM(Revenue) AS revenue FROM transactions WHERE CustomerID IS NOT NULL GROUP BY CustomerID ORDER BY revenue DESC",
                     ["CustomerID", "revenue"], [[1., 30.], [3., 20.], [4., 18.], [2., 10.]]),
                    ("monthly_refunds_and_years", "SELECT InvoiceMonth, SUM(Revenue) AS revenue FROM transactions GROUP BY InvoiceMonth ORDER BY InvoiceMonth",
                     ["InvoiceMonth", "revenue"], [["2024-01", 35.], ["2024-02", 65.], ["2024-03", 20.], ["2025-01", 18.]]),
                    ("empty_result", "SELECT Country FROM transactions WHERE Country = 'ZZ'", ["Country"], []),
                    ("keyword_inside_literal", "SELECT Description FROM transactions WHERE Description = 'Update guide'",
                     ["Description"], [["Update guide"]]),
                    ("keyword_inside_alias", "SELECT SUM(Revenue) AS total_updates FROM transactions",
                     ["total_updates"], [[138.]]),
                    ("leading_comment", "-- retail analysis\nSELECT COUNT(*) AS n FROM transactions", ["n"], [[8]]),
                    ("readonly_cte", "WITH totals AS (SELECT SUM(Revenue) AS revenue FROM transactions) SELECT revenue FROM totals",
                     ["revenue"], [[138.]]),
                ]
                for name, query, columns, rows in cases:
                    record(name, "sql_fixture", lambda q=query, c=columns, r=rows: check_sql(q, c, r))
                record("reject_multiple_select_statements", "sql_guard",
                       lambda: check_guard("SELECT 1; SELECT 2;", False))

            for name, counts, alternative in [
                ("small_uplift", (1200, 10000, 1250, 10000), "larger"),
                ("strong_uplift", (100, 1000, 150, 1000), "larger"),
                ("treatment_decline", (150, 1000, 100, 1000), "larger"),
                ("two_sided", (120, 1000, 160, 1000), "two-sided"),
                ("equal_rates", (100, 1000, 100, 1000), "larger"),
            ]:
                record(name, "ab_numeric", lambda c=counts, a=alternative: check_ab(c, a))
            invalid = [
                ("zero_total", {"control_total": 0}),
                ("negative_total", {"control_total": -1000}),
                ("negative_conversions", {"control_conversions": -1}),
                ("conversions_exceed_total", {"treatment_conversions": 1001}),
                ("fractional_conversions", {"control_conversions": 1.5}),
                ("invalid_alpha", {"alpha": 1.2}),
                ("zero_alpha", {"alpha": 0.}),
                ("invalid_alternative", {"alternative": "invalid"}),
            ]
            for name, overrides in invalid:
                kwargs = dict(control_conversions=100, control_total=1000,
                              treatment_conversions=150, treatment_total=1000)
                kwargs.update(overrides)
                record(name, "ab_validation",
                       lambda kw=kwargs: require_value_error(lambda: ab_test_tool.run_ab_test(**kw)))
            record("zero_control_rate", "ab_edge", check_zero_control)

            record("bar_values_and_png", "visualization", lambda: check_chart("bar"))
            record("line_values_and_png", "visualization", lambda: check_chart("line"))
            record("missing_column", "visualization", check_missing_column)
            record("empty_data", "visualization", lambda: require_value_error(
                lambda: visualization_tool.create_bar_chart(pd.DataFrame({"x": [], "y": []}),
                                                             "x", "y", "Empty", "empty.png")))
            record("nonnumeric_values", "visualization", lambda: require_value_error(
                lambda: visualization_tool.create_bar_chart(pd.DataFrame({"x": ["a"], "y": ["bad"]}),
                                                             "x", "y", "Text", "text.png")))
            record("filename_containment", "visualization", check_chart_path)

    require(source_hashes() == before_hashes, "Evaluation changed an existing source file.")
    counts = {s: sum(r["status"] == s for r in results) for s in ["passed", "failed", "skipped"]}
    report = {
        "schema_version": 1,
        "benchmark": "phase0-tools-v1",
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Offline tool checks only. No natural-language agent invocation.",
        "end_to_end_task_success_rate": None,
        "model_latency_ms": None,
        "token_usage": None,
        "api_requests": 0,
        "timing_note": "Local check durations include assertions and chart rendering; not model or service latency.",
        "fixture": {"kind": "synthetic", "rows": 8, "total_revenue": 138.},
        "historical_database": database_info,
        "environment": environment(),
        "source_sha256": before_hashes,
        "summary": {"total": len(results), **counts},
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(f"\n{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped")
    print(f"Report: {args.output}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
