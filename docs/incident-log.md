# Development incident log

## 2026-10-30 — Evaluation batch crashed before writing metrics

- **Symptom:** `python -m app.evaluation` raised `TypeError` while aggregating groundedness; FAISS also warned that relevance scores were outside the expected 0–1 range.
- **Cause:** the boolean expression returned a set instead of a boolean, and the FAISS convenience method normalized inner-product distances with the wrong relevance function.
- **Recovery:** explicitly convert the groundedness condition to `bool`; switch to raw FAISS distances and map distances into a bounded similarity score. Re-ran the 20-scenario batch successfully; report is in `eval/latest-report.json`.

## 2026-11-08 — Corpus rebuild rejected a duplicate clause ID

- **Symptom:** the fixed-dimension FAISS rebuild stopped because two passages shared `CAR-1.5`.
- **Cause:** the clause continued across a page break; the parser emitted each page fragment as a separate vector with the same citation ID.
- **Recovery:** merge page-split passages by stable clause ID and retain the source page list. Rebuilt the 52-chunk corpus and FAISS index, then reran the 22-scenario evaluation successfully.

## 2026-11-14 — Review agent died on every 10th run

- **Symptom:** under repeated submissions, runs stayed at `running` forever and the review agent process had exited. Before it died, a successful run was sometimes returned as a "reviewer unavailable" escalation.
- **Cause:** `record_live_quality()` samples every 10th run and called `record_event(rid, ..., **quality)` where `quality` also contained `run_id`, raising `TypeError: got multiple values for argument 'run_id'`. In the failure path this exception escaped the handler and killed the process; Compose had no restart policy.
- **Recovery:** stop passing `run_id` twice; catch per-message exceptions in both agent loops so one message cannot kill a worker; add `restart: unless-stopped` to the application services. Re-ran 26 consecutive claims: all completed, both agents alive, live quality samples recorded.

## 2026-11-14 — Load test recorded no end-to-end samples

- **Symptom:** Locust reported only submit and poll requests; the `submission-to-result` sample never appeared, so the SLO could not be measured.
- **Cause:** `locustfile.py` called `poll.success()` on a request made without `catch_response=True`, raising `LocustError` in every user before the RUN event fired.
- **Recovery:** poll inside `with self.client.get(..., catch_response=True)`. Results are in `docs/benchmark-results.md`.

## 2026-11-14 — Reclaimed claims always failed

- **Symptom:** after a simulated intake-worker crash, the claim was reclaimed after 60 s but immediately marked `failed`.
- **Cause:** a missing `AGENT_RETRIES` import broke the reclaim path, and the 20 s wall-clock bound was measured from submission time, which is always over 60 s for a reclaimed message.
- **Recovery:** add the import and time the bound from when the worker starts the task. A reclaimed claim now completes normally.

## 2026-11-15 — PR review: model text could contradict the decision

- **Symptom:** in code review, a teammate showed that an advisory with the correct position and citations but the text "Approve compensation of ten thousand rupees" passed output validation on a declined claim, and that text replaced the recommendation shown to the officer. "Pay the passenger 10000 rupees" also passed, because amounts were only checked after "INR", "Rs" or "₹".
- **Cause:** when the exact-text match was relaxed to allow real-model wording, the validator checked route, citations, clauses and prefixed amounts, but not what the explanation actually claimed.
- **Recovery:** the rule engine's recommendation is now always the authoritative text; model wording is stored separately as `model_explanation`. The validator also rejects amounts written in words, amounts written as "10000 rupees", and, for any route other than pay-as-computed, sentences saying compensation is owed or approved. Checked against 11 contradictory explanations (all rejected), 5 correct paraphrases (all accepted) and 31,774 generated claims (no correct rule text rejected).

## 2026-11-15 — PR review: declines for 24 hours to two weeks notice

- **Symptom:** a claim with notice between 24 hours and two weeks, where the passenger refused the offered alternate flight, was declined under CAR 3.3.1.
- **Cause:** CAR 3.3.1 requires an alternate flight or refund "as acceptable to the passenger". An alternate the passenger did not accept does not show that obligation was met.
- **Recovery:** this case now routes to `request_further_evidence`, asking whether a refund was offered. Scenario E14 was updated to match. The same review found that Locust counted non-200 status polls as successes; they are now recorded as failures (checked against a fake API returning HTTP 500: 96 of 96 polls reported as failures).

