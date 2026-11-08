# Development incident log

## 2026-10-30 — Evaluation batch crashed before writing metrics

- **Symptom:** `python -m app.evaluation` raised `TypeError` while aggregating groundedness; FAISS also warned that relevance scores were outside the expected 0–1 range.
- **Cause:** the boolean expression returned a set instead of a boolean, and the FAISS convenience method normalized inner-product distances with the wrong relevance function.
- **Recovery:** explicitly convert the groundedness condition to `bool`; switch to raw FAISS distances and map distances into a bounded similarity score. Re-ran the 20-scenario batch successfully; report is in `eval/latest-report.json`.

## 2026-11-08 — Corpus rebuild rejected a duplicate clause ID

- **Symptom:** the fixed-dimension FAISS rebuild stopped because two passages shared `CAR-1.5`.
- **Cause:** the clause continued across a page break; the parser emitted each page fragment as a separate vector with the same citation ID.
- **Recovery:** merge page-split passages by stable clause ID and retain the source page list. Rebuilt the 52-chunk corpus and FAISS index, then reran the 22-scenario evaluation successfully.
