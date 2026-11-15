from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis import Redis
from opentelemetry import propagate
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from .telemetry import configure

logging.basicConfig(level=logging.INFO, format="%(message)s")

ROOT = Path(__file__).resolve().parent.parent
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis = Redis.from_url(REDIS_URL, decode_responses=True)
START = time.monotonic()
app = FastAPI(title="Air Passenger Claim Triage", version="0.1.0",
              description="Evidence-grounded triage recommendations; an officer decides.")
if os.getenv("SERVICE_ROLE", "api") == "api":
    configure("claim-api")
    FastAPIInstrumentor.instrument_app(app)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8080", "http://127.0.0.1:8080"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Client-Id"])


@app.exception_handler(RequestValidationError)
async def quarantine_validation_error(request: Request, exc: RequestValidationError):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    errors = [{"field": ".".join(str(x) for x in e.get("loc", ())), "error": e.get("msg", "invalid value")} for e in exc.errors()]
    redis.rpush("quarantine", json.dumps({"at": now(), "field_errors": errors, "payload": payload}, default=str))
    return JSONResponse(status_code=422, content={"detail": errors, "quarantined": True})


class Claim(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    claim_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    disruption: Literal["denied_boarding", "cancellation", "delay", "downgrading"]
    flight_number: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    scheduled_departure: datetime
    actual_departure: datetime | None = None
    block_time_minutes: int = Field(ge=1, le=1800, strict=True)
    notice_minutes: int | None = Field(default=None, ge=0, le=525600, strict=True)
    alternate_flight_provided: bool | None = Field(default=None, strict=True)
    alternate_accepted: bool | None = Field(default=None, strict=True)
    alternate_delay_minutes: int | None = Field(default=None, ge=0, le=10080, strict=True)
    booked_one_way_basic_fare_inr: float | None = Field(default=None, ge=0, le=10000000, strict=True)
    airline_fuel_charge_inr: float | None = Field(default=None, ge=0, le=1000000, strict=True)
    passenger_reported_at_airport: bool | None = Field(default=None, strict=True)
    contact_details_provided: bool | None = Field(default=None, strict=True)
    cause: str | None = Field(default=None, max_length=2000, strict=True)
    cause_disputed: bool = Field(default=False, strict=True)
    free_text: str = Field(min_length=1, max_length=5000, strict=True)

    @model_validator(mode="after")
    def cross_fields(self):
        if self.alternate_accepted is True and self.alternate_flight_provided is not True:
            raise ValueError("alternate_accepted=true requires alternate_flight_provided=true")
        if self.alternate_delay_minutes is not None and self.alternate_flight_provided is not True:
            raise ValueError("alternate_delay_minutes requires alternate_flight_provided=true")
        if self.scheduled_departure.tzinfo is None:
            raise ValueError("scheduled_departure must include a timezone offset")
        if self.actual_departure and self.actual_departure.tzinfo is None:
            raise ValueError("actual_departure must include a timezone offset")
        if self.actual_departure and self.actual_departure < self.scheduled_departure:
            raise ValueError("actual_departure cannot precede scheduled_departure")
        if (self.booked_one_way_basic_fare_inr is None) != (self.airline_fuel_charge_inr is None):
            raise ValueError("basic fare and fuel charge must be supplied together")
        return self


class Feedback(BaseModel):
    satisfactory: bool
    reason: str = Field(min_length=1, max_length=2000)


def now():
    return datetime.now(timezone.utc).isoformat()


def record_event(run_id, event, **fields):
    data = {"ts": now(), "run_id": run_id, "correlation_id": fields.get("correlation_id", run_id), "event": event, **fields}
    raw = json.dumps(data)
    redis.rpush(f"events:{run_id}", raw)
    redis.rpush("events:all", raw)
    logging.info(raw)
    if fields.get("agent") and fields.get("latency_ms") is not None:
        agent = fields["agent"]
        step = fields.get("step", event)
        metric_key = f"agent:step-latencies:{agent}:{step}"
        redis.lpush(metric_key, float(fields["latency_ms"]))
        redis.ltrim(metric_key, 0, 999)


@app.get("/healthz")
def health():
    try:
        redis.ping()
        return {"status": "ok", "queue": "ok", "uptime_seconds": int(time.monotonic() - START)}
    except Exception:
        raise HTTPException(503, "queue unavailable")


@app.post("/v1/claims", status_code=202)
def submit(claim: Claim, x_client_id: str = Header(default="anonymous", min_length=1, max_length=128,
                                                       pattern=r"^[A-Za-z0-9_.:@-]+$")):
    free_fields = {"free_text": claim.free_text, "cause": claim.cause or ""}
    injection = re.compile(r"(?i)(ignore\s+(all|any|previous|prior|the|your)\s+(\w+\s+)?(instructions|rules|prompt)"
                           r"|reveal\s+(the\s+)?(system\s+)?prompt|override\s+(the\s+)?(rules|instructions|policy)"
                           r"|disregard.{0,40}(instructions|rules)|forget.{0,30}(instructions|rules)|developer message|jailbreak"
                           r"|^\s*(system|assistant|developer)\s*:|\n\s*(system|assistant|developer)\s*:|\bsystem\s*:\s*you\b"
                           r"|you are now|act as (an?|the)\b|admin mode|developer mode|(follow|obey) (these|the following|my) (new )?instructions"
                           r"|(output|return|respond with|set)\s+(the\s+)?(route|position|decision)\b|pay-as-computed|decline_with_cited_provision"
                           r"|<\s*/?\s*(system|instructions?)\s*>)")
    toxicity = re.compile(r"(?i)\b(fuck|shit|bitch|asshole|bastard)\b")
    errors = []
    for field, value in free_fields.items():
        if injection.search(value):
            errors.append({"field": field, "error": "instruction-like content rejected"})
        if toxicity.search(value):
            errors.append({"field": field, "error": "abusive language flagged for human review"})
    if errors:
        redis.rpush("quarantine", json.dumps({"at": now(), "field_errors": errors, "payload": claim.model_dump(mode="json")}))
        raise HTTPException(422, detail={"message": "Request quarantined by input guard", "field_errors": errors})
    minute = int(time.time() // 60)
    count = redis.incr(f"rate:{x_client_id}:{minute}")
    redis.expire(f"rate:{x_client_id}:{minute}", 120)
    if count > int(os.getenv("CLAIM_RATE_LIMIT_PER_MINUTE", "30")):
        raise HTTPException(429, "client rate limit exceeded")
    rid = str(uuid.uuid4())
    payload = claim.model_dump(mode="json")
    trace_context = {}
    propagate.inject(trace_context)
    with redis.pipeline(transaction=True) as pipeline:
        pipeline.hset(f"run:{rid}", mapping={"status": "queued", "created_at": now(), "client_id": x_client_id, "claim": json.dumps(payload)})
        pipeline.xadd("claim_queue", {"payload": json.dumps({"run_id": rid, "claim": payload,
            "trace_context": trace_context, "submitted_monotonic": time.monotonic()})})
        pipeline.execute()
    redis.incr("runs:submitted")
    record_event(rid, "submitted", correlation_id=rid)
    return {"run_id": rid, "status": "queued", "status_url": f"/v1/runs/{rid}", "correlation_id": rid}


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str):
    d = redis.hgetall(f"run:{run_id}")
    if not d:
        raise HTTPException(404, "run not found")
    out = {"run_id": run_id, "status": d.get("status"), "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}
    if d.get("result"):
        out["result"] = json.loads(d["result"])
    if d.get("error"):
        out["error"] = d["error"]
    return out


@app.get("/v1/runs/{run_id}/trace")
def trace(run_id: str):
    state = redis.hgetall(f"run:{run_id}")
    if not state:
        raise HTTPException(404, "run not found")
    return {"run_id": run_id, "claim": json.loads(state["claim"]),
            "result": json.loads(state["result"]) if state.get("result") else None,
            "events": [json.loads(x) for x in redis.lrange(f"events:{run_id}", 0, -1)]}


@app.post("/v1/runs/{run_id}/feedback")
def feedback(run_id: str, body: Feedback):
    if not redis.exists(f"run:{run_id}"):
        raise HTTPException(404, "run not found")
    entry = {"run_id": run_id, "satisfactory": body.satisfactory, "reason": body.reason, "at": now()}
    redis.rpush("feedback", json.dumps(entry))
    redis.incr("feedback:total")
    redis.incr("feedback:" + str(body.satisfactory).lower())
    return {"saved": True, "feedback": entry}


@app.get("/metrics")
def metrics():
    total = int(redis.llen("events:all"))
    def pending(stream, group):
        try:
            return int(redis.xpending(stream, group).get("pending", 0))
        except Exception:
            return 0
    queue = pending("claim_queue", "intake") + pending("review_queue", "review")
    done = int(redis.get("runs:completed") or 0)
    failed = int(redis.get("runs:failed") or 0)
    submitted = int(redis.get("runs:submitted") or 0)
    review_fallbacks = int(redis.get("agent:failures:entitlement_reviewer") or 0)
    body = ("# TYPE claim_runs_total counter\n" f"claim_runs_total {done}\n"
            f"claim_runs_submitted_total {submitted}\nclaim_runs_failed_total {failed}\n"
            f"claim_runs_fallback_total {review_fallbacks}\nclaim_queue_depth {queue}\n"
            f"claim_events_total {total}\nclaim_uptime_seconds {int(time.monotonic()-START)}\n")
    try:
        manifest = json.loads((ROOT / "corpus" / "manifest.json").read_text(encoding="utf8"))
        body += f"corpus_chunk_count {int(manifest.get('chunk_count', 0))}\n"
        body += f"corpus_ingest_seconds {float(manifest.get('ingest_seconds', 0))}\n"
        body += f"corpus_ingest_chunks_per_second {float(manifest.get('chunks_per_second', 0))}\n"
        body += f"corpus_build_timestamp_seconds {datetime.fromisoformat(manifest['built_at'].replace('Z','+00:00')).timestamp() if manifest.get('built_at') else 0}\n"
    except Exception:
        body += "corpus_chunk_count 0\ncorpus_ingest_seconds 0\ncorpus_ingest_chunks_per_second 0\ncorpus_build_timestamp_seconds 0\n"
    try:
        index_manifest = json.loads((ROOT / "corpus" / "faiss_index" / "manifest.json").read_text(encoding="utf8"))
        body += f"corpus_index_chunk_count {int(index_manifest.get('chunk_count', 0))}\n"
        body += f"corpus_index_build_seconds {float(index_manifest.get('build_seconds', 0))}\n"
        body += f"corpus_index_vectors_per_second {float(index_manifest.get('vectors_per_second', 0))}\n"
        body += f"corpus_index_build_timestamp_seconds {datetime.fromisoformat(index_manifest['built_at'].replace('Z','+00:00')).timestamp() if index_manifest.get('built_at') else 0}\n"
    except Exception:
        body += "corpus_index_chunk_count 0\ncorpus_index_build_seconds 0\ncorpus_index_vectors_per_second 0\ncorpus_index_build_timestamp_seconds 0\n"
    for role in ("intake", "entitlement_reviewer"):
        success = int(redis.get(f"agent:success:{role}") or 0)
        errors = int(redis.get(f"agent:failures:{role}") or 0)
        body += f'agent_success_total{{agent="{role}"}} {success}\nagent_failures_total{{agent="{role}"}} {errors}\n'
        m = redis.hgetall(f"agent:metrics:{role}")
        attempts = max(success+errors, 1)
        runs = max(int(m.get("runs", 0)), 1)
        body += f'agent_success_rate{{agent="{role}"}} {success/attempts:.4f}\n'
        body += f'agent_avg_steps{{agent="{role}"}} {float(m.get("steps", 0))/runs:.4f}\n'
        body += f'agent_avg_tool_calls{{agent="{role}"}} {float(m.get("tool_calls", 0))/runs:.4f}\n'
        body += f'agent_avg_latency_ms{{agent="{role}"}} {float(m.get("latency_ms_total", 0))/runs:.4f}\n'
        body += f'agent_uptime_seconds{{agent="{role}"}} {int(redis.get("agent:uptime:"+role) or 0)}\n'
        retries = int(redis.get(f"agent:retries:{role}") or 0)
        body += f'agent_retries_total{{agent="{role}"}} {retries}\n'
        retried_runs = int(redis.scard(f"agent:retried_runs:{role}"))
        body += f'agent_retry_rate{{agent="{role}"}} {retried_runs/attempts:.4f}\n'
        for key in redis.scan_iter(f"agent:step-latencies:{role}:*"):
            step = key.rsplit(":", 1)[-1]
            latencies = sorted(float(value) for value in redis.lrange(key, 0, -1))
            for percentile, q in (("p50", .50), ("p95", .95)):
                value = latencies[min(len(latencies)-1, max(0, math.ceil(q*len(latencies))-1))] if latencies else 0
                label = step.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                body += f'agent_step_latency_ms_{percentile}{{agent="{role}",step="{label}"}} {value:.4f}\n'
    body += f"feedback_positive_total {int(redis.get('feedback:true') or 0)}\nfeedback_negative_total {int(redis.get('feedback:false') or 0)}\n"
    samples = sorted(float(x) for x in redis.lrange("latencies:run", 0, -1))
    def pct(q):
        return samples[min(len(samples)-1, max(0, math.ceil(q*len(samples))-1))] if samples else 0
    body += f"claim_latency_ms_p50 {pct(.50)}\nclaim_latency_ms_p95 {pct(.95)}\n"
    body += f"model_prompt_tokens_total {int(redis.hget('tokens:prompt','count') or 0)}\nmodel_completion_tokens_total {int(redis.hget('tokens:completion','count') or 0)}\n"
    cost = float(redis.hget('cost:total','usd') or 0)
    body += f"model_cost_usd_total {cost:.8f}\nmodel_cost_usd_per_100_runs {cost*100/max(done,1):.8f}\n"
    costs_by_step = {step: float(value) for step, value in redis.hgetall("cost:by-step").items()}
    for step, value in costs_by_step.items():
        body += f'model_cost_step_usd{{step="{step}"}} {value:.8f}\n'
    if costs_by_step:
        top_step, top_cost = max(costs_by_step.items(), key=lambda pair: pair[1])
        body += f'model_cost_largest_step_usd{{step="{top_step}"}} {top_cost:.8f}\n'
    else:
        body += 'model_cost_largest_step_usd{step="none"} 0\n'
    for key, value in redis.hgetall("tokens:by-step").items():
        usage, step = key.split(":", 1)
        body += f'model_{usage}_tokens_step_total{{step="{step}"}} {int(value)}\n'
    body += f"retrieval_cache_hits_total {int(redis.get('retrieval:cache_hits') or 0)}\nretrieval_cache_misses_total {int(redis.get('retrieval:cache_misses') or 0)}\n"
    samples = [json.loads(x) for x in redis.lrange("quality:samples", 0, 19)]
    for metric in ("groundedness", "context_relevance", "faithfulness", "hallucination_rate"):
        value = sum(float(s[metric]) for s in samples)/len(samples) if samples else 0
        body += f"live_quality_{metric} {value:.4f}\n"
    return HTMLResponse(body, media_type="text/plain; version=0.0.4")


@app.get("/v1/quality")
def quality():
    return {"samples": [json.loads(x) for x in redis.lrange("quality:samples", 0, 99)]}


@app.get("/v1/costs")
def costs():
    completed = int(redis.get("runs:completed") or 0)
    total = float(redis.hget("cost:total", "usd") or 0)
    by_step = {step: float(value) for step, value in redis.hgetall("cost:by-step").items()}
    return {"currency": "USD", "runs_completed": completed, "total_cost": total,
            "cost_per_100_runs": total*100/max(completed, 1), "by_step": by_step,
            "tokens_by_step": redis.hgetall("tokens:by-step"),
            "largest_cost_step": max(by_step, key=by_step.get) if by_step else None,
            "prompt_tokens": int(redis.hget("tokens:prompt", "count") or 0),
            "completion_tokens": int(redis.hget("tokens:completion", "count") or 0)}


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "app" / "static" / "index.html").read_text(encoding="utf8")
