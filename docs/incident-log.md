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

