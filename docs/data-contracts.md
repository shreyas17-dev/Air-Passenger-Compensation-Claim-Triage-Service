# Data contracts (v1)

## POST /v1/claims

Accepts `application/json`, returns HTTP 202 with `{run_id,status,status_url,correlation_id}`. Required: `disruption` (`denied_boarding|cancellation|delay|downgrading`), `flight_number` (2–12 letters/numbers/hyphens), `scheduled_departure` (ISO-8601 datetime), `block_time_minutes` (1–1800), `free_text` (1–5000 chars). Optional fields: actual departure datetime; notice minutes (0–525600); alternate provided/accepted booleans; alternate delay (0–10080); basic fare in INR (0–10000000) and airline fuel charge in INR (0–1000000), which must be supplied together; reported-at-airport/contact booleans; cause (<=2000 chars); cause_disputed boolean; claim_id. An accepted alternate cannot be absent, and alternate delay requires an offered alternate. Actual departure may not precede scheduled departure. Rejections return 422 with field errors and are recorded in Redis quarantine; they do not queue a run.

## GET /v1/runs/{run_id}

Returns run ID, status (`queued|running|completed|failed`), timestamps, and completed result or safe error.

## GET /v1/runs/{run_id}/trace

Returns ordered structured events keyed by correlation/run ID.

## POST /v1/runs/{run_id}/feedback

Accepts `{satisfactory:boolean,reason:string}`; persists the feedback event. Returns `{saved,feedback}`.
