# Data contracts (v1)

All JSON interfaces use UTF-8 and `application/json`. OpenAPI is available at `/openapi.json` and `/docs`.

## POST /v1/claims

Accepts `application/json`, returns HTTP 202 with `{run_id,status,status_url,correlation_id}`. Required: `disruption` (`denied_boarding|cancellation|delay|downgrading`), `flight_number` (2–12 letters/numbers/hyphens), `scheduled_departure` (ISO-8601 datetime), `block_time_minutes` (1–1800), `free_text` (1–5000 chars). Optional fields: actual departure datetime; notice minutes (0–525600); alternate provided/accepted booleans; alternate delay (0–10080); basic fare in INR (0–10000000) and airline fuel charge in INR (0–1000000), which must be supplied together; reported-at-airport/contact booleans; cause (<=2000 chars); cause_disputed boolean; claim_id. An accepted alternate cannot be absent, and alternate delay requires an offered alternate. Actual departure may not precede scheduled departure. Rejections return 422 with field errors and are recorded in Redis quarantine; they do not queue a run.

The optional `X-Client-Id` header selects the per-client rate-limit bucket; omission uses `anonymous`. A successful body is:

```json
{"run_id":"<uuid>","status":"queued","status_url":"/v1/runs/<uuid>","correlation_id":"<uuid>"}
```

## GET /v1/runs/{run_id}

Returns `{run_id,status,created_at,updated_at}` and, when completed, `result`. Result positions are `pay-as-computed`, `request_further_evidence`, `decline_with_cited_provision`, or `human_escalation`. Results include route, basis, recommendation, clause citations, authority note, retrieved passages, prompt version, token usage, and latency where available. Provider or verification failure returns a safe human-escalation result; an intake failure returns status `failed`. Unknown IDs return 404.

## GET /v1/runs/{run_id}/trace

Returns `{run_id,claim,result,events}`. Events are ordered JSON records for submission, agent starts/steps, tool calls and results, queries, passage IDs, model calls, prompt version, token counts/cost, handoff, verification, retry, and completion. The final result includes all retrieved passage text and citation metadata so the run can be reconstructed by its ID. Unknown IDs return 404.

## POST /v1/runs/{run_id}/feedback

Accepts `{satisfactory:boolean,reason:string}` where reason is 1–2000 characters; persists the feedback event and aggregates positive/negative counts. Returns `{saved,feedback}`. Unknown run IDs return 404.

## GET /v1/quality

Returns `{samples:[...]}` with up to 100 automatically sampled live runs. Each sample has `run_id`, timestamp, groundedness, context relevance, faithfulness, and hallucination rate in the range 0–1.

## GET /v1/costs

Returns aggregate USD cost, cost per 100 completed runs, prompt/completion token totals, per-step cost, and the largest cost step.

## GET /healthz

Returns `{status:"ok",queue:"ok",uptime_seconds}` when Redis is reachable; otherwise returns HTTP 503.

## GET /metrics

Returns Prometheus text exposition for run counts, queue depth, latency percentiles, per-agent success/retry/step metrics, corpus/index freshness and throughput, model cost/token totals, cache counters, and live quality metrics.

## GET /

Serves the browser client as HTML. The dashboard service also exposes `/healthz` and `/metrics`.

## Internal model-stub interface

`POST /v1/advise` accepts `{prompt:string,payload:object}` and returns `{output,usage:{prompt_tokens,completion_tokens}}`. It is an internal development/load-test dependency, not a public claim interface. `STUB_FAIL` and `STUB_UNSUPPORTED_OUTPUT` exercise bounded retry and output verification.
