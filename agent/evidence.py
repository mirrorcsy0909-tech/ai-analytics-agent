"""JSON evidence shared by tool messages and deterministic answer checks."""

import hashlib
import json
from pathlib import Path


def seal_evidence(evidence):
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, allow_nan=False)
    evidence = {**evidence, "id": evidence["kind"] + "_" + hashlib.sha256(payload.encode()).hexdigest()[:16]}
    return json.dumps(evidence, ensure_ascii=False, allow_nan=False), evidence


def table_evidence(frame, *, kind="sql", **metadata):
    # Only expose the same displayed rows to the model and answer renderer.
    rows = json.loads(frame.head(20).to_json(orient="records", double_precision=15))
    evidence = {"kind": kind, "columns": list(frame.columns), "rows": rows,
                "row_count": len(frame), "displayed_rows": len(rows),
                "currency": None, **metadata}
    if kind == "chart":
        evidence["png_sha256"] = hashlib.sha256(Path(metadata["path"]).read_bytes()).hexdigest()
    return seal_evidence(evidence)
