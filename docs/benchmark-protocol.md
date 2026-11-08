# Benchmark protocol

The end-to-end target was fixed before collecting measurements: p95 from claim submission through final result is at most 5 seconds for stub-backed runs at 20 concurrent users. The Locust `RUN` sample starts before `POST /v1/claims` and stops only after the run reaches `completed`.

## Compare worker scale

Run the same duration and user count at one worker per agent and then three workers per agent. Save each run's Locust `stats.csv`, and record the completed-RPS, p50/p95 of `RUN,submission-to-result`, failures, concurrency, and whether the p95 target was met. Inspect cAdvisor and agent metrics to identify the bottleneck if throughput does not improve.

```sh
docker compose up --build -d --scale intake-agent=1 --scale review-agent=1
docker compose --profile benchmark run --rm locust --headless -u 20 -r 5 --run-time 2m --csv eval/scale-1
docker compose up -d --scale intake-agent=3 --scale review-agent=3
docker compose --profile benchmark run --rm locust --headless -u 20 -r 5 --run-time 2m --csv eval/scale-3
```

## Compare retrieval cache

Use two fresh Compose project names, one with `CACHE_RETRIEVALS=true` and one with it false. Keep all other settings, users, ramp-up, duration, and corpus constant. Save Locust `stats.csv`, `/metrics`, and `/v1/costs` for each phase. Do not compare cumulative Redis counters from a reused project.

The report helper compares end-to-end latency and completed RPS, and accepts optional cache-metric and cost snapshots:

```sh
python -m app.benchmark_report --baseline eval/cache-off_stats.csv --candidate eval/cache-on_stats.csv --baseline-label cache-off --candidate-label cache-on --baseline-metrics eval/cache-off-metrics.txt --candidate-metrics eval/cache-on-metrics.txt --baseline-cost eval/cache-off-costs.json --candidate-cost eval/cache-on-costs.json
```

Record the concurrency at which the declared p95 target first fails. A run that cannot pull its images or complete the workload is an environment failure, not a measured SLO result.
