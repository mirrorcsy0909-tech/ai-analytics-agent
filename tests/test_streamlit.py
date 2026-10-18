"""Exercise the actual Streamlit controls without making model calls."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest
from agent.observability import ObservationSettings


class FakeAgent:
    def __init__(self, fail_once=False):
        self.fail_once = fail_once
        self.calls = []

    def invoke(self, data, config):
        self.calls.append((data, config))
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("Temporary outage")
        return {"status": "completed", "final_answer": "Verified example result", "artifacts": []}

    def get_state(self, config):
        return SimpleNamespace(next=("planner",))


class StreamlitTests(unittest.TestCase):
    def setUp(self):
        offline = patch("agent.observability.ObservationSettings.from_env", return_value=ObservationSettings())
        offline.start()
        self.addCleanup(offline.stop)
        st.cache_resource.clear()
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
