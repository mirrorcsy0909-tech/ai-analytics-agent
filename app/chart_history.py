"""Capture only verified charts returned to this browser conversation."""

import hashlib
from pathlib import Path

MAX_CHARTS = 4
MAX_CHART_BYTES = 5_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHART_LABELS = {
    "countries": "Revenue by country",
    "products": "Revenue by product",
    "customers": "Revenue by customer",
    "monthly": "Monthly revenue",
}


def capture_charts(result, output_dir):
    """Return bounded, verified PNG bytes; never discover files by directory scan.

    A chart must appear in this invocation's artifacts and successful tool
    evidence. Snapshot its bytes so later file deletion or replacement cannot
    silently change what the conversation displays.
    """
    evidence_by_path = {}
    for entry in result.get("tool_results", []):
        evidence = entry.get("evidence")
        if (entry.get("status") == "success" and isinstance(evidence, dict)
                and evidence.get("kind") == "chart" and isinstance(evidence.get("path"), str)):
            evidence_by_path[evidence["path"]] = evidence
    root = Path(output_dir).resolve()
    captured, unavailable = [], 0
    for name in reversed(list(dict.fromkeys(result.get("artifacts", [])))):
        if len(captured) >= MAX_CHARTS:
            break
        try:
            evidence = evidence_by_path.get(name)
            path = Path(name)
            if (not evidence or path.is_symlink() or path.suffix.lower() != ".png"
                    or not path.resolve().is_relative_to(root) or not path.is_file()):
                raise ValueError("Unavailable chart")
            with path.open("rb") as source:
                data = source.read(MAX_CHART_BYTES + 1)
            digest = hashlib.sha256(data).hexdigest()
            if (len(data) > MAX_CHART_BYTES or not data.startswith(PNG_SIGNATURE)
                    or digest != evidence.get("png_sha256")):
                raise ValueError("Unavailable chart")
            captured.append({"data": data, "sha256": digest,
                             "caption": CHART_LABELS.get(evidence.get("analysis_type"), "Analysis chart")})
        except (OSError, ValueError, TypeError):
            unavailable += 1
    return captured, unavailable


def merge_chart_history(previous, current):
    """Keep the four most recent distinct chart snapshots in this conversation."""
    unique = {}
    for chart in [*current, *previous]:
        unique.setdefault(chart["sha256"], chart)
    return list(unique.values())[:MAX_CHARTS]
