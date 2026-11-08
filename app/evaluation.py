"""Fixed-batch groundedness evaluation with explicit before/after strategies."""
import json
import os
import time
from pathlib import Path

from .agents import adjudicate, verify, search_corpus, lookup_clause, classify_extract

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "eval" / "scenarios.json"
CLAUSES = {"cancellation": ["CAR-3.3.1", "CAR-3.3.2", "CAR-3.3.3", "CAR-3.3.4", "CAR-1.4-1.5", "CAR-3.8.1"],
           "delay": ["CAR-3.4.1", "CAR-3.4.2", "CAR-3.4.3", "CAR-3.4.4", "CAR-1.4-1.5", "CAR-3.8.1"],
           "denied_boarding": ["CAR-3.2.1", "CAR-3.2.2", "CAR-3.2.3", "CAR-1.4-1.5"],
           "downgrading": ["CAR-3.5.1"]}


def run_variant(cases, improved):
    records = []
    for case in cases:
        claim = case["claim"]
        docs = {p["id"]: p for p in search_corpus(claim["disruption"] + " compensation amount", limit=4)}
        if improved:
            docs.update({p["id"]: p for p in search_corpus(claim["disruption"] + " condition exception alternate facilities notice meals contact", limit=8)})
            for cid in CLAUSES[claim["disruption"]]:
                p = lookup_clause(cid)
                if p:
                    docs[cid] = p
        evidence = list(docs.values())
        result = adjudicate(claim, classify_extract(claim), evidence)
        valid, _ = verify(result, evidence)
        citations = {c["source_id"] for c in result.get("citations", [])}
        support = set(case["supporting_passages"])
        # Groundedness: verified citations for determinations, or a safe no-claim escalation.
        grounded = int(bool(valid and citations.issubset(docs) and (citations or result["position"] == "human_escalation")))
        relevance = len(citations & support) / max(len(citations), 1)
        faithfulness = int(valid and citations.issubset(docs))
        hallucination = int(not valid or bool(citations - docs.keys()) or result["position"] != case["expected_position"])
        records.append({"id": case["id"], "expected": case["expected_position"], "actual": result["position"],
                        "groundedness": grounded, "context_relevance": relevance, "faithfulness": faithfulness,
                        "hallucination": hallucination, "citation_ids": sorted(citations),
                        "pass": result["position"] == case["expected_position"]})
    n = max(len(records), 1)
    metrics = {"groundedness": sum(x["groundedness"] for x in records)/n,
               "context_relevance": sum(x["context_relevance"] for x in records)/n,
               "faithfulness": sum(x["faithfulness"] for x in records)/n,
               "hallucination_rate": sum(x["hallucination"] for x in records)/n,
               "accuracy": sum(x["pass"] for x in records)/n,
               "scenario_count": len(records)}
    return metrics, records


def evaluate():
    # Disable cache during the fixed comparison so both variants exercise retrieval equally.
    os.environ["CACHE_RETRIEVALS"] = "false"
    cases = json.loads(EVAL_FILE.read_text(encoding="utf8"))
    before, before_cases = run_variant(cases, False)
    after, after_cases = run_variant(cases, True)
    metrics = {"before": {"prompt_version": "triage-v1", "strategy": "single entitlement search", "metrics": before, "cases": before_cases},
               "after": {"prompt_version": "triage-v2", "strategy": "condition and cross-reference search", "metrics": after, "cases": after_cases},
               "delta": {key: round(after[key]-before[key], 4) for key in before if key != "scenario_count"}}
    report = {"created_at": time.time(), "metric_definitions": {
        "groundedness": "fraction of routes supported by retrieved passages and verifier; no-claim escalation is safe",
        "context_relevance": "per-case fraction of citations matching scenario-declared supporting clause IDs, averaged",
        "faithfulness": "fraction whose citations resolve exactly to retrieved text and clause metadata",
        "hallucination_rate": "fraction with unverified citations or output route different from expected",
        "accuracy": "fraction matching expected route"}, **metrics}
    (ROOT / "eval" / "latest-report.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    evaluate()
