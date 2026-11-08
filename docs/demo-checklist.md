# Demonstration checklist

Use synthetic claims only. The service recommends a route for an officer; it never authorizes payment.

1. **Clean start:** from a clean clone, run `docker compose up --build`. Confirm API, client, agents, Redis, model stub, Prometheus, Grafana, and Jaeger are healthy.
2. **Valid routed recommendation:** submit the cancellation example from the browser client. Confirm it reaches `request_further_evidence` and displays clause citations and the officer decision note.
3. **Trace one run:** open `/v1/runs/{run_id}/trace`. Confirm the claim, LangGraph tool calls and queries, passage IDs, linked conditions, prompt version, model tokens/cost, retries, hand-off, verifier result, and final passage text are present. Open the same run's spans in Jaeger by correlation ID.
4. **Malformed request:** submit an invalid field or unknown field. Confirm HTTP 422 with field errors, an entry in Redis `quarantine`, and no new message in `claim_queue`.
5. **Safe escalation:** submit an out-of-scope baggage claim or set `cause_disputed=true`. Confirm `human_escalation`, with no unsupported entitlement conclusion.
6. **Self-verification:** set `STUB_UNSUPPORTED_OUTPUT=true`, recreate the model stub, and submit a valid claim. Confirm the unretrieved citation is rejected and the result escalates. Restore the setting to `false` and recreate the stub.
7. **Prompt injection and toxicity:** submit instruction-like or abusive free text. Confirm the request is quarantined before a run is queued.
8. **Circuit breaker:** set `STUB_FAIL=true`, recreate the stub, and submit claims until the provider circuit opens. Confirm bounded retries, the open state in Redis, and safe escalation. Restore `STUB_FAIL=false` after the cooldown.
9. **Load and scale:** follow [benchmark-protocol.md](benchmark-protocol.md) to measure the declared end-to-end SLO at 20 users and compare one versus three worker replicas. Record results; do not infer them from the API submit time.
10. **Section 4.1 case:** submit 90-minute cancellation notice, airport arrival, and an accepted alternate four hours later. Confirm cancellation compensation is not recommended and the airport-wait facilities question is sent for confirmation.
