"""Deterministic model stub with configurable realistic latency and failure injection."""
import json
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest
from .telemetry import configure
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI(title="Model Stub")
configure("model-stub")
FastAPIInstrumentor.instrument_app(app)
REQUESTS = Counter("model_stub_requests_total", "Requests served by the model stub", ["result"])
LATENCY = Histogram("model_stub_request_duration_seconds", "Model stub response time")


class AdviceRequest(BaseModel):
    prompt: str
    payload: dict


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(generate_latest().decode("utf8"), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/advise")
def advise(request: AdviceRequest):
    started = time.monotonic()
    if os.getenv("STUB_FAIL", "false").lower() == "true":
        REQUESTS.labels("failed").inc()
        LATENCY.observe(time.monotonic()-started)
        raise HTTPException(503, "injected stub outage")
    time.sleep(max(0, float(os.getenv("STUB_MODEL_DELAY_SECONDS", "0.15"))))
    rule = request.payload["rule_result"]
    output = {"position": rule["position"], "route": rule["route"], "basis": rule["basis"],
              "recommendation": rule["recommendation"],
              "citation_ids": [c["source_id"] for c in rule.get("citations", [])]}
    if os.getenv("STUB_UNSUPPORTED_OUTPUT", "false").lower() == "true":
        output["citation_ids"].append("UNRETRIEVED-CLAUSE")
    REQUESTS.labels("success").inc()
    LATENCY.observe(time.monotonic()-started)
    return {"output": output,
            "usage": {"prompt_tokens": max(1, len(request.prompt + json.dumps(request.payload)) // 4),
                      "completion_tokens": max(1, len(json.dumps(output)) // 4)}}
