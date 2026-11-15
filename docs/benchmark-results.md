# Benchmark results

Target, declared before measuring (see [benchmark-protocol.md](benchmark-protocol.md)): **p95 from submission to final result ≤ 5 s** for stub-backed runs at 20 concurrent users.

## Environment and caveats

- Measured on 2026-11-14 with Locust 2.x, `-r 10`, 45–60 s per run, using the `submission-to-result` RUN sample (starts before `POST /v1/claims`, stops when the run reaches `completed`).
- The model is the stub with `STUB_MODEL_DELAY_SECONDS=0.15`.
- These runs used the same services (Redis, API, intake and review agents, stub) started as native processes on one Linux VM, because Docker Hub was unreachable from that machine. **Re-run the same protocol under `docker compose` on the demo machine and replace these numbers**; container networking adds some overhead.

## Worker scale (20 users)

| Intake / review workers | Completed runs/s | p50 | p95 | Target met |
| --- | ---: | ---: | ---: | --- |
| 1 / 1 | 5.42 | 3500 ms | 3700 ms | yes |
| 3 / 3 | 15.46 | 1100 ms | 1300 ms | yes |

Scaling to three workers per agent gives 2.9× throughput and cuts p95 by 65%.

## Where the target fails

| Intake / review workers | Users | Completed runs/s | p95 | Target met |
| --- | ---: | ---: | ---: | --- |
| 1 / 1 | 20 | 5.42 | 3700 ms | yes |
| 1 / 1 | 30 | 5.27 | 5500 ms | **no** |
| 1 / 1 | 40 | 5.26 | 7300 ms | no |
| 3 / 3 | 60 | 14.55 | 3900 ms | yes |
| 3 / 3 | 80 | 14.67 | 5200 ms | **no** |
| 3 / 3 | 100 | 14.31 | 6600 ms | no |

Throughput is flat once saturated (~5.3 runs/s per reviewer), so extra users only add queueing time. The target fails at about **30 users with one worker per agent** and about **80 users with three**.

## Bottleneck

Per-agent average task time: intake ≈ 43 ms, review ≈ 185 ms (of which 150 ms is the model call). The **review agent's model call** is the bottleneck. Confirmation: 1 intake + 3 review workers at 40 users gives 15.2 runs/s and p95 2600 ms — the same as 3 + 3 — so scaling intake adds nothing. With a real provider, the provider's latency and rate limit become the ceiling, which is why the stub is used for load tests.

## Retrieval cache (3 / 3 workers, 20 users)

| Cache | Completed runs/s | p50 | p95 | Intake avg task time | Hit ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| off | 15.14 | 1100 ms | 1300 ms | 54 ms | – |
| on | 15.78 | 1100 ms | 1200 ms | 38 ms | 99.7% |

The cache cuts intake time by ~30% but end-to-end p95 by only ~100 ms, because retrieval is not the bottleneck. It does not change model cost, since only retrievals are cached; caching whole runs for identical claims would be the next step if cost mattered.

## Cost

With the stub, token counts are recorded (about 4,700 prompt and 170 completion tokens per run, measured over 728 runs, all in the `entitlement_review` step) but the per-1K rates default to 0. Set `MODEL_INPUT_COST_PER_1K` / `MODEL_OUTPUT_COST_PER_1K` to your provider's rates and read `/v1/costs` for cost per 100 runs; the single step that makes a model call (`entitlement_review`) is the only cost driver.
