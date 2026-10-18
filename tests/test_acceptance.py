"""Check acceptance references and prevent optimistic acceptance scoring."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.acceptance import ROOT, check_turn, create_fixture, digest, equal, summarize
from eval.acceptance_score import score
from tools import sql_tool
from tools.ab_test_tool import run_ab_test

SUITE = json.loads((ROOT / "eval/acceptance_v1.json").read_text())


class AcceptanceTests(unittest.TestCase):
    def test_frozen_table_references_execute_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "fixture.db"
            create_fixture(database, SUITE)
            before = digest(database)
            with patch.object(sql_tool, "DB_PATH", database):
                for task in SUITE["tasks"]:
                    for turn in task["turns"]:
                        if "oracle_sql" not in turn:
                            continue
                        frame = sql_tool.run_sql_query(turn["oracle_sql"])
                        self.assertTrue(equal(frame.values.tolist(), turn["expected"]["rows"]), task["id"])
            self.assertEqual(digest(database), before)

    def test_ab_tools_match_independently_frozen_probability_references(self):
        for task in SUITE["tasks"]:
            for turn in task["turns"]:
                expected = turn["expected"]
                if expected["kind"] != "ab":
                    continue
                actual = run_ab_test(**expected["counts"], alpha=expected["alpha"],
                                     alternative=expected["alternative"])
                for field in ("p_value", "absolute_lift", "test_method", "conclusion"):
                    self.assertTrue(equal(actual[field], expected[field]), (task["id"], field))

    def test_last_successful_table_must_match_reference(self):
        specification = SUITE["tasks"][0]["turns"][0]
        expected = specification["expected"]
        evidence = {"kind": "sql", "columns": expected["columns"],
                    "rows": [dict(zip(expected["columns"], row)) for row in expected["rows"]]}
        entry = {"status": "success", "evidence": evidence}
        turn = {"fixture_unchanged": True, "workflow": {"status": "completed",
                "tool_results": [entry], "validation": [{"kind": "claim_grounding", "passed": True}]}}
        self.assertTrue(all(check_turn(turn, specification).values()))
        wrong = copy.deepcopy(entry)
        wrong["evidence"]["rows"][0][expected["columns"][1]] += 10
        turn["workflow"]["tool_results"].append(wrong)
        self.assertFalse(check_turn(turn, specification)["reference_table"])

    def test_missing_or_stale_narrative_review_never_becomes_success(self):
        turn = {"answer": "Answer", "checks": {"reference_table": True}, "duration_ms": 1}
        report = {"cases": [{"turns": [turn]}]}
        summary = summarize(report, SUITE)
        self.assertEqual(summary["deterministic_passed_turns"], 1)
        self.assertEqual(summary["reviewed_turns"], 0)
        self.assertIsNone(summary["strict_success_rate"])
        turn["review"] = {"passed": True, "answer_sha256": "stale"}
        self.assertEqual(summarize(report, SUITE)["reviewed_turns"], 0)

    def test_archived_acceptance_rescores_without_network(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")) as network:
            result = score(ROOT / "eval/results/acceptance_v1_first_run.json",
                           ROOT / "eval/acceptance_v1.json", ROOT / "eval/results/acceptance_v1_reviews.json")
        network.assert_not_called()
        self.assertEqual(result["summary"]["strict_passed_turns"], 25)
        self.assertEqual(result["summary"]["strict_success_rate"], 1)

    def test_acceptance_rejects_stale_answer_review(self):
        reviews = json.loads((ROOT / "eval/results/acceptance_v1_reviews.json").read_text())
        reviews["reviews"][0]["answer_sha256"] = "stale"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reviews.json"
            path.write_text(json.dumps(reviews))
            with self.assertRaises(ValueError):
                score(ROOT / "eval/results/acceptance_v1_first_run.json", ROOT / "eval/acceptance_v1.json", path)


if __name__ == "__main__":
    unittest.main()
