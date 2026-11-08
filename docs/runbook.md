# Runbook

## Redis unavailable / queues not progressing

- **Symptom:** `/healthz` returns 503, submission returns 5xx, or queue depth rises without completed runs.
- **Confirm:** `docker compose ps`; `docker compose logs redis intake-agent review-agent`; inspect Prometheus `claim_queue_depth`.
- **Recover:** restore Redis health first, then restart the affected agent (`docker compose restart intake-agent` or `review-agent`). Redis AOF persists queue streams; the consumer groups reclaim pending entries idle for 60 seconds. Check each affected run status before asking the caller to resubmit.

## Model provider unavailable / circuit open

- **Symptom:** runs finish at `human_escalation` with reviewer failure; `provider:open` is false in Redis.
- **Confirm:** inspect `/v1/runs/{run_id}/trace` for model retries and reviewer fallback; inspect model-stub/provider logs and `agent_failures_total`.
- **Recover:** for the stub, restore `STUB_FAIL=false`; for an OpenAI-compatible provider, verify runtime-injected base URL, key, and model name. Wait for the configured breaker cooldown, then submit one canary claim and inspect citations before resuming.

## Prompt injection / unsupported model output

Input instruction-like text is quarantined before enqueue. For output-filter demonstrations, set `STUB_UNSUPPORTED_OUTPUT=true` before starting the stub; the reviewer should reject the fabricated citation and return human escalation. Restore it to `false` after the exercise.

## SLO

The declared target is p95 submission-to-final-result <= 5 seconds for stub-backed runs at 20 concurrent clients. Measure with the `benchmark` Compose profile and report the measured concurrency, end-to-end `RUN` percentile, RPS, and bottleneck; do not report API submission latency as run latency.
