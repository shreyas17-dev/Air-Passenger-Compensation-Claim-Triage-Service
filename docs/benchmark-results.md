# Benchmark results

Target, declared before measuring (see [benchmark-protocol.md](benchmark-protocol.md)): **p95 from submission to final result ≤ 5 s** for stub-backed runs at 20 concurrent users.

## Environment

- Measured on 2026-11-15 under Docker Compose (Docker Desktop on Windows) with the `benchmark` profile: `docker compose --profile benchmark run --rm locust -f /srv/locustfile.py --host http://api:8000 --headless -u <users> -r 10 --run-time <60s|45s> --only-summary`.
- The figure reported is the `RUN submission-to-result` sample: the timer starts before `POST /v1/claims` and stops when the run reaches `completed`.
- Model: the stub with `STUB_MODEL_DELAY_SECONDS=0.15`. All runs had 0 failures.

## Worker scale (20 users)

| Intake / review workers | Completed runs/s | p50 | p95 | Target met |
| --- | ---: | ---: | ---: | --- |
| 1 / 1 | 6.02 | 3200 ms | 3300 ms | yes |
| 3 / 3 | 17.79 | 970 ms | 1100 ms | yes |

Scaling to three workers per agent gives 2.95× throughput and cuts p95 by 67%.

## Where the target fails (3 / 3 workers)

| Users | Completed runs/s | p50 | p95 | p99 | `POST /v1/claims` p95 | Target met |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 20 | 17.79 | 970 ms | 1100 ms | 1100 ms | 16 ms | yes |
| 60 | 16.90 | 3300 ms | 3500 ms | 4300 ms | 150 ms | yes |
| 80 | 14.88 | 4600 ms | 6000 ms | 6500 ms | 1000 ms | **no** |

The target fails **between 60 and 80 concurrent users** with three workers per agent. With one worker per agent, throughput is capped at about 6 runs/s; at that rate p95 grows roughly in proportion to users, so the target would fail at about 30 users (estimated from the 20-user run, not measured separately).

## Bottleneck

- **One worker per agent:** the **review agent** is the limit. Each review waits about 150 ms for the model call, which caps one reviewer at about 6 runs/s; extra users only add queueing time.
- **Three workers per agent, 60+ users:** throughput *falls* as users rise (17.8 → 16.9 → 14.9 runs/s) while the API slows sharply (`POST /v1/claims` p95 16 → 150 → 1000 ms). Each Locust user polls `GET /v1/runs/{id}` every 100 ms, which is about 440 requests/s at 60 users, all served by the **single API process**. At this load the bottleneck moves from the review agent to the API. The fixes would be running more API workers (uvicorn `--workers` or `--scale api` behind a proxy), or slower or event-driven polling.

## Retrieval cache

Measured with the services run as native processes (3 / 3 workers, 20 users), not yet repeated under Docker:

| Cache | Completed runs/s | p95 | Intake avg task time | Hit ratio |
| --- | ---: | ---: | ---: | ---: |
| off | 15.14 | 1300 ms | 54 ms | – |
| on | 15.78 | 1200 ms | 38 ms | 99.7% |

The cache cuts intake time by about 30% but end-to-end p95 by only about 100 ms, because retrieval is not the bottleneck. It does not change model cost, since only retrievals are cached.

## Cost

With the stub, token counts are recorded (about 4,700 prompt and 170 completion tokens per run, all in the `entitlement_review` step) but the per-1K rates default to 0. Set `MODEL_INPUT_COST_PER_1K` / `MODEL_OUTPUT_COST_PER_1K` to your provider's rates and read `/v1/costs` for cost per 100 runs. The single step that makes a model call (`entitlement_review`) is the only cost driver.
