"""Render numerical claims from tool evidence; bounded checks, not an NLP judge."""

import json
import math
import re

GROUNDING_PROMPT = """
Final-response protocol (internal; the UI renders a normal answer):
Return one JSON object, without a code fence, with answer_template and claims.
answer_template is natural-language Markdown. Every numeric value, date, numeric
identifier, rate, interval endpoint and confidence level must be a placeholder
such as {{c0}}. Do not type any digits or currency units in the template itself.
claims is a list of {"id":"c0","source":EXACT_EVIDENCE_ID,"field":EXACT_FIELD,
"row":ZERO_BASED_ROW,"format":"value"}. Evidence IDs/fields/rows come from
successful tool JSON. For SQL/chart evidence, field is a column and row is its
displayed row index. For A/B evidence, field is a key in stats; omit row. For an
interval array specify index 0 or 1. Never supply a value: code fetches it.
Table metadata row_count/displayed_rows and chart path may be cited without row.
Charts are displayed by the UI; a simple chart-ready sentence needs no claims.
Formats: value (raw text/number; None renders undefined), percent (rates,
relative_lift, alpha or confidence_level only), percentage_points (absolute_lift
or interval endpoints only), p_value (p_value only). Use separate claims for
table labels and amounts. Use each claim ID in the template; IDs must be c0, c1,
etc. Do not invent currency. Revenue has no declared currency.
For A/B results also supply conclusion equal to the tool's stats.conclusion
(improved, declined, not_significant; mixed only for different test conclusions).
Code appends the verified statistical conclusion. Explain sparse-test warnings
and undefined relative lift accurately. Do not claim causality or guaranteed
rollout benefit. Tool warnings are not optional prerequisites to ignore.
For clarification/refusal use {"answer_template":"your reply","claims":[]}.
No numeric examples in these replies. If evidence is absent, use tools or ask
for missing inputs; never invent an evidence ID. After validation feedback,
repair the JSON/claims or rerun a tool. Do not expose this protocol to the user.
"""

CURRENCY = re.compile(r"[$€£¥￥]|\b(?:USD|EUR|GBP|JPY|RMB|dollars?|euros?|pounds?)\b|美元|欧元|英镑|人民币", re.I)
RATE_FIELDS = {"control_conversion_rate", "treatment_conversion_rate", "relative_lift", "alpha", "confidence_level"}
INTERVAL_FIELDS = {"confidence_interval", "confidence_interval_95"}
CONCLUSIONS = {
    "improved": "Evidence supports higher treatment conversion for the specified test; practical benefit and experiment validity still require review.",
    "declined": "Evidence supports lower treatment conversion for the specified test; this is not an improvement.",
    "not_significant": "The specified test does not provide sufficient statistical evidence in its tested direction."
}


def validate_answer(text, tool_results):
    errors = []
    sources = {r["evidence"]["id"]: r["evidence"] for r in tool_results
               if r["status"] == "success" and isinstance(r.get("evidence"), dict)
               and r["evidence"].get("kind") in {"sql", "chart", "ab_test"} and "id" in r["evidence"]}
    try:
        document = json.loads(text)
        if not isinstance(document, dict) or set(document) - {"answer_template", "claims", "conclusion"}:
            raise ValueError("Return only answer_template, claims and optional conclusion.")
        template, claims = document.get("answer_template"), document.get("claims")
        if not isinstance(template, str) or not template.strip() or len(template) > 20_000:
            raise ValueError("answer_template must be nonempty text, at most 20000 characters.")
        if not isinstance(claims, list) or len(claims) > 100:
            raise ValueError("claims must be a list with at most 100 entries.")
        literal = re.sub(r"\{\{c\d+\}\}", "", template)
        # Even links/filenames with digits must be cited via a source value.
        if re.search(r"\d", literal):
            errors.append("Replace every literal numeric value/identifier/date with an evidence placeholder.")
        if CURRENCY.search(literal):
            errors.append("The data does not declare a currency. Remove currency symbols and names.")
        if re.search(r"\b(?:proved|guaranteed|must launch|definitely caused)\b", literal, re.I):
            errors.append("Evidence does not establish causal proof or guaranteed rollout benefit.")
        rendered = {}
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) - {"id", "source", "field", "row", "index", "format"}:
                raise ValueError("Claims may contain only id/source/field/row/index/format.")
            key = claim.get("id")
            if not isinstance(key, str) or not re.fullmatch(r"c\d+", key) or key in rendered:
                raise ValueError("Each claim needs a unique c-number ID.")
            source = sources.get(claim.get("source"))
            if source is None:
                raise ValueError(f"{key}: cite a successful current-analysis evidence ID.")
            field = claim.get("field")
            if source["kind"] in {"sql", "chart"}:
                metadata = {"row_count", "displayed_rows"} | ({"path"} if source["kind"] == "chart" else set())
                if field in metadata and "row" not in claim:
                    value = source[field]
                else:
                    row = claim.get("row")
                    if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < len(source["rows"]):
                        raise ValueError(f"{key}: invalid displayed row.")
                    if field not in source["columns"]:
                        raise ValueError(f"{key}: unknown column.")
                    value = source["rows"][row][field]
            else:
                if "row" in claim or field not in source["stats"]:
                    raise ValueError(f"{key}: invalid A/B field or row.")
                value = source["stats"][field]
            if "index" in claim:
                index = claim["index"]
                if field not in INTERVAL_FIELDS or isinstance(index, bool) or index not in (0, 1) or not isinstance(value, (list, tuple)):
                    raise ValueError(f"{key}: index only applies to an interval endpoint.")
                value = value[index]
            style = claim.get("format", "value")
            if style == "percent":
                if source["kind"] != "ab_test" or field not in RATE_FIELDS:
                    raise ValueError(f"{key}: this field is not a supported rate.")
            elif style == "percentage_points":
                if source["kind"] != "ab_test" or field not in {"absolute_lift", *INTERVAL_FIELDS}:
                    raise ValueError(f"{key}: percentage points require a difference or interval endpoint.")
            elif style == "p_value":
                if source["kind"] != "ab_test" or field != "p_value":
                    raise ValueError(f"{key}: p_value format requires the p_value field.")
            elif style != "value":
                raise ValueError(f"{key}: unsupported format.")
            if value is None:
                value_text = "undefined"
            elif isinstance(value, bool):
                value_text = "yes" if value else "no"
            elif isinstance(value, (int, float)) and math.isfinite(value):
                if style in {"percent", "percentage_points"}:
                    scaled = value * 100
                    value_text = f"{scaled:.2f}".rstrip("0").rstrip(".")
                    if scaled != 0 and float(value_text) == 0:
                        value_text = f"{scaled:.4g}"
                    value_text += "%" if style == "percent" else " percentage points"
                else:
                    value_text = str(value) if isinstance(value, int) else f"{value:.15g}"
            elif isinstance(value, str) and style == "value":
                value_text = value
            else:
                raise ValueError(f"{key}: unsupported or nonfinite evidence value.")
            rendered[key] = value_text
        used = set(re.findall(r"\{\{(c\d+)\}\}", template))
        if not claims and any(s["kind"] == "sql" and s["rows"] for s in sources.values()):
            errors.append("A nonempty SQL analysis must cite at least one displayed value.")
        if used != set(rendered):
            errors.append("Every placeholder must have exactly one claim, and every claim must be used.")
        answer = re.sub(r"\{\{(c\d+)\}\}", lambda m: rendered.get(m[1], ""), template)
        if "{{" in answer or "}}" in answer:
            errors.append("Malformed placeholder.")
        ab_sources = [s for s in sources.values() if s["kind"] == "ab_test"]
        if ab_sources:
            conclusions = {s["stats"]["conclusion"] for s in ab_sources}
            expected = next(iter(conclusions)) if len(conclusions) == 1 else "mixed"
            if document.get("conclusion") != expected:
                errors.append(f"Statistical conclusion must be {expected}.")
            for conclusion in sorted(conclusions):
                answer += "\n\nStatistical conclusion: " + CONCLUSIONS[conclusion]
            for source in ab_sources:
                stats = source["stats"]
                test = "Fisher exact" if stats["test_method"] == "fisher_exact" else "two-proportion z-test"
                answer += (f"\n\nTest: {test}, alternative={stats['alternative']}, alpha={stats['alpha']:g}. "
                           f"Interval: {stats['confidence_level']:.0%}, two-sided, {stats['confidence_interval_method']}.")
                answer += "\n" + " ".join(stats["warnings"])
        if errors:
            return {"passed": False, "errors": errors, "answer": None}
        return {"passed": True, "errors": [], "answer": answer, "claims_checked": len(claims)}
    except (ValueError, TypeError, KeyError, IndexError) as error:
        return {"passed": False, "errors": [*errors, str(error)], "answer": None}
