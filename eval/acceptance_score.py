"""Rescore archived acceptance locally, with separate answer-hash reviews."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from eval.acceptance import ROOT, answer_digest, check_turn, digest, summarize
except ModuleNotFoundError:
    from acceptance import ROOT, answer_digest, check_turn, digest, summarize


def score(recording_path, suite_path, reviews_path=None):
    suite = json.loads(Path(suite_path).read_text())
    report = json.loads(Path(recording_path).read_text())
    if report.get("suite_sha256") != digest(suite_path):
        raise ValueError("Recording does not match the frozen acceptance suite.")
    tasks = {t["id"]: t for t in suite["tasks"]}
    seen = set()
    turns = {}
    for case in report["cases"]:
        if case["id"] in seen or case["id"] not in tasks:
            raise ValueError("Duplicate or unknown acceptance scenario.")
        seen.add(case["id"])
        specifications = tasks[case["id"]]["turns"]
        for index, turn in enumerate(case["turns"], 1):
            if index > len(specifications) or turn.get("turn") != index:
                raise ValueError("Invalid turn numbering or extra turns.")
            specification = specifications[index - 1]
            if turn.get("question") != specification["question"]:
                raise ValueError("Recorded acceptance question was changed.")
            turn["checks"] = check_turn(turn, specification)
            turn["review"] = None
            turns[(case["id"], index)] = turn
    if reviews_path:
        reviews = json.loads(Path(reviews_path).read_text())
        if reviews.get("recording_sha256") != digest(recording_path) or reviews.get("suite_sha256") != digest(suite_path):
            raise ValueError("Reviews do not match the frozen recording and suite.")
        reviewed = set()
        for review in reviews["reviews"]:
            key = (review["id"], review["turn"])
            if key in reviewed or key not in turns:
                raise ValueError("Duplicate or unknown review entry.")
            if not isinstance(review.get("passed"), bool) or review.get("answer_sha256") != answer_digest(turns[key]):
                raise ValueError("Review is missing a boolean decision or has a stale answer hash.")
            reviewed.add(key)
            turns[key]["review"] = review
    return {"schema_version": 1, "scored_at_utc": datetime.now(timezone.utc).isoformat(),
            "recording_sha256": digest(recording_path), "suite_sha256": digest(suite_path),
            "scorer_sha256": digest(__file__), "reviews_sha256": digest(reviews_path) if reviews_path else None,
            "configured_model": report["configured_model"], "summary": summarize(report, suite),
            "scope": "New synthetic acceptance checks plus hash-bound Codex review, not an external benchmark or production accuracy estimate.",
            "categories": {category: {"turns": sum(len(c["turns"]) for c in report["cases"] if c["category"] == category)}
                           for category in sorted({c["category"] for c in report["cases"]})},
            "turns": [{"id": case["id"], "turn": turn["turn"], "checks": turn["checks"],
                       "review": turn["review"], "answer_sha256": answer_digest(turn)}
                      for case in report["cases"] for turn in case["turns"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=ROOT / "eval/acceptance_v1.json")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/acceptance_score.json")
    args = parser.parse_args()
    result = score(args.report, args.suite, args.reviews)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"]))
    return 0 if result["summary"]["strict_success_rate"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
