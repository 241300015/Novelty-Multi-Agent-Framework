"""Summarize structural observations; semantic judgments remain manual."""
import json
from pathlib import Path

root = Path("docs/experiments/planner-concept-stage/review-comparison-20261109")
summary = {}
for directory in sorted(root.iterdir()):
    if not directory.is_dir():
        continue
    payload = json.loads((directory / "input.json").read_text())
    source = [payload["point"]["claim"], *payload["point"]["technical_features"]]
    results = {}
    for version in ("old", "new"):
        path = directory / f"{version}-response.json"
        if not path.exists():
            results[version] = {"status": "missing"}
            continue
        response = json.loads(path.read_text())
        choice = response["choices"][0]
        try:
            result = json.loads(choice["message"]["content"])
            decisions = result["decisions"]
            ids = [str(item["candidate"]) for item in decisions]
            details = {"status": choice["finish_reason"], "candidate_count": len(payload["candidates"]),
                       "decision_count": len(decisions),
                       "candidate_coverage_exact": sorted(ids) == sorted(payload["candidates"]),
                       "missing": result["missing"], "total_tokens": response.get("usage", {}).get("total_tokens")}
            if version == "old":
                details["empty_alias_rejected"] = sum(not payload["candidates"][str(x["candidate"])]["alias"]
                                                        and not x["aliases_belong"] for x in decisions)
            else:
                details["verbatim_source_quotes"] = sum(any(x.get("source_quote") and x["source_quote"] in text
                                                            for text in source) for x in decisions)
                mismatch = []
                for item in decisions:
                    candidate = payload["candidates"][str(item["candidate"])]
                    for field, key in (("terms", "term"), ("alias", "alias")):
                        output_field = "aliases" if field == "alias" else "terms"
                        actual = [x[key] for x in item[output_field]]
                        if sorted(actual) != sorted(candidate[field]):
                            mismatch.append({"candidate": item["candidate"], "field": field,
                                             "expected": candidate[field], "actual": actual})
                details["field_coverage_mismatches"] = mismatch
                details["alias_decisions"] = [{"candidate": x["candidate"], **alias}
                                              for x in decisions for alias in x["aliases"]]
            results[version] = details
        except (ValueError, KeyError, TypeError) as exc:
            results[version] = {"status": "invalid", "error": str(exc)}
    summary[directory.name] = results
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(summary, ensure_ascii=False, indent=2))
