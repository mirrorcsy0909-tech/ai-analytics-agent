"""Chart provenance, file changes and bounded browser snapshots."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.chart_history import MAX_CHARTS, capture_charts, merge_chart_history


class ChartHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "charts"
        self.root.mkdir()
        self.path = self.root / "chart.png"
        Image.new("RGB", (2, 2), "navy").save(self.path)
        self.data = self.path.read_bytes()

    def result(self, path=None, **evidence):
        path = str(path or self.path)
        return {"artifacts": [path], "tool_results": [{"status": "success", "evidence": {
            "kind": "chart", "path": path, "analysis_type": "monthly",
            "png_sha256": hashlib.sha256(self.data).hexdigest(), **evidence}}]}

    def test_only_invocation_artifacts_with_successful_evidence_are_captured(self):
        self.assertEqual(capture_charts({}, self.root), ([], 0))
        result = self.result()
        charts, unavailable = capture_charts(result, self.root)
        self.assertEqual(unavailable, 0)
        self.assertEqual(charts[0]["data"], self.data)
        self.assertEqual(charts[0]["caption"], "Monthly revenue")
        for entry in [[], [{"status": "error", "evidence": result["tool_results"][0]["evidence"]}]]:
            with self.subTest(entry=entry):
                self.assertEqual(capture_charts({**result, "tool_results": entry}, self.root), ([], 1))

    def test_changed_missing_external_and_symlink_files_are_not_displayed(self):
        result = self.result()
        self.path.write_bytes(self.data + b"changed")
        self.assertEqual(capture_charts(result, self.root), ([], 1))
        self.path.unlink()
        self.assertEqual(capture_charts(result, self.root), ([], 1))
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(self.data)
        self.assertEqual(capture_charts(self.result(outside), self.root), ([], 1))
        self.path.symlink_to(outside)
        self.assertEqual(capture_charts(result, self.root), ([], 1))

    def test_invalid_png_and_oversized_files_are_rejected_even_with_matching_hash(self):
        for data in [b"not a PNG", b"\x89PNG\r\n\x1a\n" + b"x" * 5_000_000]:
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                result = self.result(png_sha256=hashlib.sha256(data).hexdigest())
                self.assertEqual(capture_charts(result, self.root), ([], 1))

    def test_history_keeps_latest_distinct_snapshots_with_a_fixed_bound(self):
        previous = [{"sha256": str(n), "data": bytes([n])} for n in range(MAX_CHARTS)]
        current = [{"sha256": "new", "data": b"new"}, previous[0]]
        merged = merge_chart_history(previous, current)
        self.assertEqual([chart["sha256"] for chart in merged], ["new", "0", "1", "2"])
        self.assertEqual(len(previous), MAX_CHARTS)
