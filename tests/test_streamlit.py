"""Exercise the actual Streamlit controls without making model calls."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest
from agent.observability import ObservationSettings


class FakeAgent:
    def __init__(self, fail_once=False, result=None):
        self.fail_once = fail_once
        self.calls = []
        self.result = result

    def invoke(self, data, config):
        self.calls.append((data, config))
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("Temporary outage")
        return self.result or {"status": "completed", "final_answer": "Verified example result", "artifacts": []}

    def get_state(self, config):
        return SimpleNamespace(next=("planner",))


class StreamlitTests(unittest.TestCase):
    def setUp(self):
        offline = patch("agent.observability.ObservationSettings.from_env", return_value=ObservationSettings())
        offline.start()
        self.addCleanup(offline.stop)
        self.path = str(Path(__file__).resolve().parents[1] / "app/streamlit_app.py")

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run()
        self.assertEqual(len(app.exception), 0)

    def test_initial_page_does_not_build_model(self):
        with patch("agent.analytics_agent.build_agent") as build:
            app = AppTest.from_file(self.path).run()
        self.assertEqual(len(app.exception), 0)
        build.assert_not_called()

    def test_run_preserves_result_and_clear_starts_new_session(self):
        fake = FakeAgent()
        with patch("agent.analytics_agent.build_agent", return_value=fake):
            app = AppTest.from_file(self.path).run()
            first_id = app.session_state["analysis_thread_id"]
            app.text_area[0].input("Analyze").run()
            self.click(app, "Run Analysis")
            self.assertEqual(app.session_state["last_analysis"]["final_answer"], "Verified example result")
            self.assertEqual(fake.calls[0][1]["configurable"]["thread_id"], first_id)
            self.click(app, "Clear conversation")
            self.assertNotEqual(app.session_state["analysis_thread_id"], first_id)
            self.assertNotIn("last_analysis", app.session_state)
            self.assertNotIn("analysis_agent", app.session_state)
            self.assertEqual(app.text_area[0].value, "")

    def test_separate_browsers_and_clear_own_their_agents(self):
        first, second, replacement = FakeAgent(), FakeAgent(), FakeAgent()
        with patch("agent.analytics_agent.build_agent", side_effect=[first, second, replacement]) as build:
            one = AppTest.from_file(self.path).run()
            two = AppTest.from_file(self.path).run()
            one.text_area[0].input("First conversation").run()
            self.click(one, "Run Analysis")
            two.text_area[0].input("Second conversation").run()
            self.click(two, "Run Analysis")
            self.assertIs(one.session_state["analysis_agent"], first)
            self.assertIs(two.session_state["analysis_agent"], second)
            self.click(one, "Clear conversation")
            one.text_area[0].input("Fresh conversation").run()
            self.click(one, "Run Analysis")
            self.assertIs(one.session_state["analysis_agent"], replacement)
            self.assertIs(two.session_state["analysis_agent"], second)
            two.text_area[0].input("Follow-up in second conversation").run()
            self.click(two, "Run Analysis")
            self.assertEqual(build.call_count, 3)
            self.assertEqual(len(first.calls), 1)
            self.assertEqual(len(second.calls), 2)
            self.assertEqual(second.calls[0][1]["configurable"], second.calls[1][1]["configurable"])

    def test_chart_snapshot_is_session_owned_and_clear_removes_it(self):
        root = Path(self.path).parents[1] / "outputs"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            path = Path(folder) / "chart.png"
            Image.new("RGB", (2, 2), "navy").save(path)
            original = path.read_bytes()
            result = {"status": "completed", "final_answer": "Verified chart", "artifacts": [str(path)],
                      "tool_results": [{"status": "success", "evidence": {
                          "kind": "chart", "path": str(path), "analysis_type": "countries",
                          "png_sha256": hashlib.sha256(original).hexdigest()}}]}
            with patch("agent.analytics_agent.build_agent", return_value=FakeAgent(result=result)):
                app = AppTest.from_file(self.path).run()
                app.text_area[0].input("Chart revenue").run()
                self.click(app, "Run Analysis")
                self.assertEqual(app.session_state["analysis_charts"][0]["data"], original)
                other = AppTest.from_file(self.path).run()
                self.assertNotIn("analysis_charts", other.session_state)
                self.assertEqual(len(other.get("imgs")), 0)
                path.unlink()
                app.run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(app.session_state["analysis_charts"][0]["data"], original)
                self.click(app, "Clear conversation")
                self.assertNotIn("analysis_charts", app.session_state)
                self.assertEqual(len(app.get("imgs")), 0)

    def test_failed_agent_construction_is_retryable_without_false_resume(self):
        with patch("agent.analytics_agent.build_agent", side_effect=[ValueError("Bad configuration"), FakeAgent()]):
            app = AppTest.from_file(self.path).run()
            app.text_area[0].input("Analyze").run()
            self.click(app, "Run Analysis")
            self.assertFalse(app.session_state["analysis_interrupted"])
            self.assertNotIn("analysis_agent", app.session_state)
            self.assertEqual(len(app.error), 1)
            self.click(app, "Run Analysis")
            self.assertEqual(len(app.error), 0)
            self.assertEqual(app.session_state["last_analysis"]["status"], "completed")

    def test_retry_resumes_pending_work_without_resending_question(self):
        fake = FakeAgent(fail_once=True)
        with patch("agent.analytics_agent.build_agent", return_value=fake):
            app = AppTest.from_file(self.path).run()
            app.text_area[0].input("Analyze").run()
            self.click(app, "Run Analysis")
            self.assertTrue(app.session_state["analysis_interrupted"])
            self.assertEqual(len(app.error), 1)
            self.click(app, "Retry interrupted analysis")
            self.assertIsNone(fake.calls[1][0])
            self.assertEqual(fake.calls[0][1]["configurable"], fake.calls[1][1]["configurable"])
            self.assertEqual(fake.calls[0][1]["recursion_limit"], fake.calls[1][1]["recursion_limit"])
            self.assertEqual(len(app.error), 0)
            self.assertEqual(app.session_state["last_analysis"]["status"], "completed")
            self.assertTrue(app.session_state["last_metrics"]["resumed"])


if __name__ == "__main__":
    unittest.main()
