"""Security, statistics, plotting and grounded-answer regression checks."""

import json
import math
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from phase0_baseline import create_fixture
from agent.answer_validation import validate_answer
from agent.workflow import build_workflow
from agent.analytics_agent import build_agent
from tools import sql_tool, visualization_tool
from tools.ab_test_tool import run_ab_test
from test_workflow import ScriptedModel, call


class SQLReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.db"
        create_fixture(self.path)
        self.patcher = patch.object(sql_tool, "DB_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_native_readonly_blocks_writes_even_if_ast_filter_is_bypassed(self):
        with patch.object(sql_tool, "is_safe_select_query", return_value=True):
            with self.assertRaises(sqlite3.DatabaseError):
                sql_tool.run_sql_query("DELETE FROM transactions")
        self.assertEqual(sql_tool.run_sql_query("SELECT COUNT(*) AS n FROM transactions").iloc[0, 0], 8)

    def test_write_metadata_extension_and_external_access_are_rejected(self):
        for query in ["SELECT 1; SELECT 2", "PRAGMA table_info(transactions)",
                      "ATTACH DATABASE 'external.db' AS extra", "SELECT * FROM sqlite_master",
                      "SELECT load_extension('x')", "SELECT readfile('x')", "SELECT writefile('x','a')",
                      "SELECT randomblob(999999999)", "SELECT * FROM pragma_table_info('transactions')",
                      "WITH t AS (SELECT 1) DELETE FROM transactions", "SELECT * FROM other.transactions"]:
            with self.subTest(query=query):
                self.assertFalse(sql_tool.is_safe_select_query(query))

    def test_comment_cte_union_and_keyword_literals_are_legal(self):
        for query in ["-- comment\nSELECT COUNT(*) AS n FROM transactions",
                      "WITH t AS (SELECT SUM(Revenue) AS v FROM transactions) SELECT v FROM t",
                      "SELECT 'drop; update' AS label UNION ALL SELECT 'create'",
                      "SELECT REPLACE(Description, 'Update', 'Guide') AS label FROM transactions LIMIT 1"]:
            with self.subTest(query=query):
                self.assertTrue(sql_tool.is_safe_select_query(query))
                self.assertFalse(sql_tool.run_sql_query(query).empty)

    def test_row_budget_is_explicit_not_silent_truncation(self):
        with self.assertRaisesRegex(ValueError, "exceeds 2 rows"):
            sql_tool.run_sql_query("SELECT Country FROM transactions", max_rows=2)

    def test_unbounded_recursive_query_is_interrupted(self):
        query = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n"
        with self.assertRaises(TimeoutError):
            sql_tool.run_sql_query(query, timeout_seconds=.005)

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.db"
        with patch.object(sql_tool, "DB_PATH", missing):
            with self.assertRaises(sqlite3.OperationalError):
                sql_tool.run_sql_query("SELECT 1")
        self.assertFalse(missing.exists())

    def test_duplicate_column_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unique names"):
            sql_tool.run_sql_query("SELECT 1 AS same, 2 AS same")

    def test_nonfinite_query_results_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            sql_tool.run_sql_query("SELECT 1e999 AS revenue")


class StatisticalReliabilityTests(unittest.TestCase):
    def test_zero_control_fisher_matches_independent_hypergeometric_probability(self):
        result = run_ab_test(0, 1000, 10, 1000)
        expected = math.prod((1000 - i) / (2000 - i) for i in range(10))
        self.assertAlmostEqual(result["p_value"], expected, places=12)
        self.assertIsNone(result["relative_lift"])
        self.assertIsNone(result["z_statistic"])
        self.assertEqual(result["test_method"], "fisher_exact")
        self.assertEqual(result["confidence_interval_method"], "newcombe_wilson")

    def test_degenerate_all_zero_and_all_one_groups_are_finite(self):
        for counts in [(0, 10, 0, 10), (10, 10, 10, 10)]:
            with self.subTest(counts=counts):
                result = run_ab_test(*counts)
                self.assertEqual(result["p_value"], 1.)
                self.assertFalse(result["statistically_significant"])
                self.assertTrue(all(math.isfinite(v) for v in result["confidence_interval"]))

    def test_lower_conversion_is_not_recommended_as_improvement(self):
        result = run_ab_test(200, 1000, 100, 1000, alternative="smaller")
        self.assertTrue(result["statistically_significant"])
        self.assertEqual(result["conclusion"], "declined")
        self.assertLess(result["absolute_lift"], 0)
        self.assertIn("lower treatment conversion", result["recommendation"])

    def test_nondefault_alpha_is_not_mislabeled_as_95_percent(self):
        result = run_ab_test(100, 1000, 150, 1000, alpha=.1)
        self.assertAlmostEqual(result["confidence_level"], .9)
        self.assertNotIn("confidence_interval_95", result)
        self.assertEqual(result["confidence_interval_sidedness"], "two-sided")

    def test_booleans_nan_and_nonintegral_counts_are_invalid(self):
        for counts in [(True, 100, 20, 100), (10., 100, 20, 100), (-1, 100, 20, 100)]:
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                run_ab_test(*counts)
        for alpha in [float("nan"), float("inf"), True]:
            with self.subTest(alpha=alpha), self.assertRaises(ValueError):
                run_ab_test(10, 100, 20, 100, alpha=alpha)

    def test_sparse_pooled_expectations_also_avoid_z_approximation(self):
        result = run_ab_test(5, 10, 99000, 100000)
        self.assertEqual(result["test_method"], "fisher_exact")


class ChartReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "charts"
        self.patcher = patch.object(visualization_tool, "OUTPUT_DIR", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.frame = pd.DataFrame({"x": ["a", "b"], "y": [2., -1.]})

    def draw(self):
        return visualization_tool.create_bar_chart(self.frame, "x", "y", "Test", "same.png")

    def test_concurrent_calls_have_distinct_valid_pngs(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda _: self.draw(), range(3)))
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(Path(p).resolve().is_relative_to(self.root.resolve()) for p in paths))
        self.assertTrue(all(Path(p).read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for p in paths))

    def test_nonfinite_boolean_missing_and_duplicate_categories_are_rejected(self):
        frames = [pd.DataFrame({"x": ["a"], "y": [v]}) for v in [float("nan"), float("inf"), True]]
        frames += [pd.DataFrame({"x": [None], "y": [1]}), pd.DataFrame({"x": ["a", "a"], "y": [1, 2]})]
        for frame in frames:
            with self.subTest(frame=str(frame)), self.assertRaises(ValueError):
                visualization_tool.create_line_chart(frame, "x", "y", "Bad", "bad.png")

    def test_symlink_and_path_escape_are_rejected(self):
        self.root.mkdir()
        (self.root / "link.png").symlink_to(Path(self.temp.name) / "outside.png")
        for name in ["../outside.png", "/tmp/outside.png", "folder\\outside.png", "link.png", "not.svg"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                visualization_tool.create_bar_chart(self.frame, "x", "y", "Bad", name)

    def test_failed_save_cleans_file_and_figure(self):
        before = visualization_tool.plt.get_fignums()
        with patch("matplotlib.figure.Figure.savefig", side_effect=OSError("Full disk")):
            with self.assertRaises(OSError):
                self.draw()
        self.assertEqual(list(self.root.glob("*.png")), [])
        self.assertEqual(visualization_tool.plt.get_fignums(), before)


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {"kind": "sql", "id": "sql_example", "columns": ["Country", "revenue"],
                         "rows": [{"Country": "DE", "revenue": 60.}], "row_count": 1, "displayed_rows": 1, "currency": None}
        self.results = [{"status": "success", "evidence": self.evidence}]
        self.claim = {"id": "c0", "source": "sql_example", "field": "revenue", "row": 0}

    def check(self, template, claims=None, results=None, **extras):
        return validate_answer(json.dumps({"answer_template": template, "claims": claims if claims is not None else [self.claim], **extras}),
                               self.results if results is None else results)

    def test_values_are_fetched_from_evidence_instead_of_model_numbers(self):
        result = self.check("Revenue is {{c0}}.")
        self.assertTrue(result["passed"])
        self.assertEqual(result["answer"], "Revenue is 60.")

    def test_currency_literals_and_unsupported_claim_values_fail(self):
        for template in ["Revenue is ${{c0}}.", "Revenue is {{c0}} USD.", "Revenue is 999."]:
            with self.subTest(template=template):
                self.assertFalse(self.check(template)["passed"])
        self.assertFalse(self.check("Revenue is {{c0}}", [{**self.claim, "value": 999}])["passed"])

    def test_failed_sources_unknown_sources_rows_and_fields_are_not_evidence(self):
        for changed in [{"source": "invented"}, {"row": 99}, {"row": True}, {"field": "missing"}]:
            with self.subTest(changed=changed):
                self.assertFalse(self.check("{{c0}}", [{**self.claim, **changed}])["passed"])
        self.assertFalse(self.check("{{c0}}", results=[{"status": "error", "evidence": self.evidence}])["passed"])

    def test_money_cannot_be_formatted_as_a_rate(self):
        self.assertFalse(self.check("{{c0}}", [{**self.claim, "format": "percent"}])["passed"])

    def test_statistical_conclusion_and_undefined_relative_lift(self):
        stats = run_ab_test(0, 1000, 10, 1000)
        results = [{"status": "success", "evidence": {"kind": "ab_test", "id": "ab_example", "stats": stats}}]
        claim = {"id": "c0", "source": "ab_example", "field": "relative_lift", "format": "percent"}
        result = self.check("Relative lift is {{c0}}.", [claim], results, conclusion="improved")
        self.assertTrue(result["passed"])
        self.assertIn("undefined", result["answer"])
        self.assertFalse(self.check("Relative lift is {{c0}}.", [claim], results, conclusion="not_significant")["passed"])

    def test_tiny_nonzero_rates_are_not_rounded_to_zero(self):
        stats = run_ab_test(1, 1000000, 2, 1000000)
        results = [{"status": "success", "evidence": {"kind": "ab_test", "id": "ab_tiny", "stats": stats}}]
        claim = {"id": "c0", "source": "ab_tiny", "field": "control_conversion_rate", "format": "percent"}
        checked = self.check("Control rate: {{c0}}", [claim], results, conclusion=stats["conclusion"])
        self.assertTrue(checked["passed"])
        self.assertIn("0.0001%", checked["answer"])

    def test_wrong_or_unused_placeholders_fail(self):
        self.assertFalse(self.check("{{c1}}")["passed"])
        self.assertFalse(self.check("No amounts here")["passed"])
        self.assertFalse(self.check("{{broken}}", [])["passed"])

    def test_no_evidence_clarification_has_no_fabricated_example_counts(self):
        self.assertTrue(self.check("Please provide control and treatment counts.", [], [])["passed"])
        self.assertFalse(self.check("For example, 100 out of 1000.", [], [])["passed"])

    def test_invalid_answer_is_repaired_within_the_same_budget(self):
        evidence = self.evidence

        @tool(response_format="content_and_artifact")
        def query(value: str) -> tuple[str, dict]:
            """Return trusted synthetic evidence."""
            return json.dumps(evidence), evidence

        bad = json.dumps({"answer_template": "Revenue is ${{c0}}.", "claims": [self.claim]})
        good = json.dumps({"answer_template": "Revenue is {{c0}}.", "claims": [self.claim]})
        graph = build_workflow(model=ScriptedModel(call("query"), AIMessage(content=bad), AIMessage(content=good)),
                               tools=[query], system_prompt="Use evidence", answer_validator=validate_answer)
        result = graph.invoke({"messages": [("user", "Revenue?")]})
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["final_answer"], "Revenue is 60.")
        self.assertEqual([v["passed"] for v in result["validation"] if v["kind"] == "claim_grounding"], [False, True])

    def test_repeated_answer_failure_stops_without_exposing_raw_json(self):
        graph = build_workflow(model=ScriptedModel(AIMessage(content="bad"), AIMessage(content="bad")),
                               tools=[], system_prompt="Use evidence", max_retries=1, answer_validator=validate_answer)
        result = graph.invoke({"messages": [("user", "Analyze")]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retry_count"], 1)
        self.assertIn("No verified result", result["final_answer"])


class ProductionIntegrationTests(unittest.TestCase):
    def model(self, name, arguments, field, style="value", row=None):
        class EvidenceModel:
            def bind_tools(self, tools):
                return self

            def invoke(self, messages, config=None):
                if messages[-1].type == "human":
                    return call(name, arguments)
                evidence = messages[-1].artifact
                claim = {"id": "c0", "source": evidence["id"], "field": field, "format": style}
                if row is not None:
                    claim["row"] = row
                answer = {"answer_template": "Result: {{c0}}.", "claims": [claim]}
                if evidence["kind"] == "ab_test":
                    answer["conclusion"] = evidence["stats"]["conclusion"]
                return AIMessage(content=json.dumps(answer))
        return EvidenceModel()

    def test_real_sql_tool_artifact_reaches_production_answer_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            create_fixture(db)
            model = self.model("query_retail_database", {"sql_query": "SELECT SUM(Revenue) AS revenue FROM transactions"}, "revenue", row=0)
            with patch.object(sql_tool, "DB_PATH", db):
                result = build_agent(model=model).invoke({"messages": [("user", "Total revenue?")]})
        self.assertEqual(result["final_answer"], "Result: 138.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_results"][0]["evidence"]["kind"], "sql")

    def test_real_zero_control_tool_completes_with_undefined_relative_lift(self):
        model = self.model("run_ab_test_analysis", dict(control_conversions=0, control_total=1000,
                           treatment_conversions=10, treatment_total=1000), "relative_lift", "percent")
        result = build_agent(model=model).invoke({"messages": [("user", "Compare conversion rates")]})
        self.assertEqual(result["status"], "completed")
        self.assertIn("undefined", result["final_answer"])
        self.assertIn("Fisher exact", result["final_answer"])


if __name__ == "__main__":
    unittest.main()
