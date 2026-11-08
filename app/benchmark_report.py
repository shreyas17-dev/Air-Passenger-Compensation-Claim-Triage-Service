"""Compare Locust end-to-end run CSVs without treating submit latency as run latency."""
import argparse
import csv
import json
from pathlib import Path


def locust_run(path):
    with Path(path).open(newline="", encoding="utf8-sig") as stream:
        rows = list(csv.DictReader(stream))
    row = next((r for r in rows if r.get("Type") == "RUN" and r.get("Name") == "submission-to-result"), None)
    if row is None:
        raise ValueError(f"{path} has no RUN,submission-to-result sample")
    def number(key):
        value = row.get(key, "")
        return float(value) if value else 0.0
    return {"requests": int(number("Request Count")), "failures": int(number("Failure Count")),
            "rps": number("Requests/s"), "p50_ms": number("50%"), "p95_ms": number("95%")}


def metrics(path):
    if not path:
        return None
    values = {}
    for line in Path(path).read_text(encoding="utf8").splitlines():
        if line.startswith("retrieval_cache_hits_total "):
            values["hits"] = float(line.rsplit(" ", 1)[-1])
        elif line.startswith("retrieval_cache_misses_total "):
            values["misses"] = float(line.rsplit(" ", 1)[-1])
    total = values.get("hits", 0)+values.get("misses", 0)
    values["hit_ratio"] = values.get("hits", 0)/total if total else 0.0
    return values


def cost(path):
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Locust stats.csv for the baseline run")
    parser.add_argument("--candidate", required=True, help="Locust stats.csv for the comparison run")
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    parser.add_argument("--baseline-users", type=int, default=20)
    parser.add_argument("--candidate-users", type=int, default=20)
    parser.add_argument("--slo-ms", type=float, default=5000)
    parser.add_argument("--baseline-metrics")
    parser.add_argument("--candidate-metrics")
    parser.add_argument("--baseline-cost")
    parser.add_argument("--candidate-cost")
    args = parser.parse_args()
    samples = [(args.baseline_label, args.baseline_users, locust_run(args.baseline)),
               (args.candidate_label, args.candidate_users, locust_run(args.candidate))]
    print("| Run | Users | Completed RPS | p50 ms | p95 ms | Failed | p95 SLO |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for label, users, run in samples:
        met = run["requests"] > 0 and run["p95_ms"] <= args.slo_ms and run["failures"] == 0
        print(f"| {label} | {users} | {run['rps']:.2f} | {run['p50_ms']:.0f} | {run['p95_ms']:.0f} | {run['failures']} | {'met' if met else 'missed'} |")
    base, candidate = samples[0][2], samples[1][2]
    print(f"\nCandidate p95 change: {candidate['p95_ms']-base['p95_ms']:.0f} ms; completed-RPS change: {candidate['rps']-base['rps']:.2f}.")
    base_metrics, candidate_metrics = metrics(args.baseline_metrics), metrics(args.candidate_metrics)
    if base_metrics is not None and candidate_metrics is not None:
        print(f"Cache hit ratio: {base_metrics['hit_ratio']:.1%} -> {candidate_metrics['hit_ratio']:.1%}.")
    base_cost, candidate_cost = cost(args.baseline_cost), cost(args.candidate_cost)
    if base_cost is not None and candidate_cost is not None:
        print(f"Cost per 100 runs: ${base_cost.get('cost_per_100_runs', 0):.6f} -> ${candidate_cost.get('cost_per_100_runs', 0):.6f}.")
        print(f"Largest candidate cost step: {candidate_cost.get('largest_cost_step') or 'none'}.")


if __name__ == "__main__":
    main()
