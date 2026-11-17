# Benchmark protocol

The end-to-end target was fixed before collecting measurements: p95 from claim submission through final result is at most 5 seconds for stub-backed runs at 20 concurrent users. The Locust `RUN` sample starts before `POST /v1/claims` and stops only after the run reaches `completed`.

## Workload

`locustfile.py` generates schema-valid synthetic claims (no real passenger data) across all four disruption types, varying flight numbers, departure times, notice bands (under 24 h, 24 h to two weeks, two weeks or more, unknown), alternate-flight offers and acceptance, fares, and contact details, while respecting the API's cross-field rules. Free text names only the selected disruption and avoids the input-guard patterns, so benchmark claims are never quarantined. Set `LOCUST_SEED` for a reproducible mix. Each claim uses its own `X-Client-Id` so the per-client rate limit does not distort results.

At the end of a run Locust prints, and writes to `eval/locust-summary.json` (override with `LOCUST_SUMMARY`), completed runs, failed runs, timeouts and HTTP errors counted separately, the outcome and disruption mix, and submission-to-result p50/p95/p99. `human_escalation` is a completed result, not a failure. Runs still in flight when the test stops are not counted.

Malformed and adversarial requests are a separate suite, so they never enter latency figures. Any failure in it means the API accepted or mishandled a request it must reject:

```sh
docker compose --profile benchmark run --rm locust -f /srv/locust_negative.py --host http://api:8000 --headless -u 2 -r 2 --run-time 30s
```

## Compare worker scale

Run the same duration and user count at one worker per agent and then three workers per agent. Save each run's Locust `stats.csv`, and record the completed-RPS, p50/p95 of `RUN,submission-to-result`, failures, concurrency, and whether the p95 target was met. Inspect cAdvisor and agent metrics to identify the bottleneck if throughput does not improve.

```sh
docker compose up --build -d --scale intake-agent=1 --scale review-agent=1
docker compose --profile benchmark run --rm locust -f /srv/locustfile.py --host http://api:8000 --headless -u 20 -r 5 --run-time 2m --csv eval/scale-1
docker compose up -d --scale intake-agent=3 --scale review-agent=3
docker compose --profile benchmark run --rm locust -f /srv/locustfile.py --host http://api:8000 --headless -u 20 -r 5 --run-time 2m --csv eval/scale-3
```

## Compare retrieval cache

Use two fresh Compose project names, one with `CACHE_RETRIEVALS=true` and one with it false. Keep all other settings, users, ramp-up, duration, and corpus constant. Save Locust `stats.csv`, `/metrics`, and `/v1/costs` for each phase. Do not compare cumulative Redis counters from a reused project.

The report helper compares end-to-end latency and completed RPS, and accepts optional cache-metric and cost snapshots:

```sh
python -m app.benchmark_report --baseline eval/cache-off_stats.csv --candidate eval/cache-on_stats.csv --baseline-label cache-off --candidate-label cache-on --baseline-metrics eval/cache-off-metrics.txt --candidate-metrics eval/cache-on-metrics.txt --baseline-cost eval/cache-off-costs.json --candidate-cost eval/cache-on-costs.json
```

Record the concurrency at which the declared p95 target first fails. A run that cannot pull its images or complete the workload is an environment failure, not a measured SLO result.
