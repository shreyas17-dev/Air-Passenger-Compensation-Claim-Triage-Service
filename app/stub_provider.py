"""Deterministic model stub with configurable realistic latency and failure injection."""
import json
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .telemetry import configure
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = FastAPI(title="Model Stub")
configure("model-stub")
FastAPIInstrumentor.instrument_app(app)


class AdviceRequest(BaseModel):
    prompt: str
    payload: dict


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/v1/advise")
def advise(request: AdviceRequest):
    if os.getenv("STUB_FAIL", "false").lower() == "true":
        raise HTTPException(503, "injected stub outage")
    time.sleep(max(0, float(os.getenv("STUB_MODEL_DELAY_SECONDS", "0.15"))))
    rule = request.payload["rule_result"]
    output = {"position": rule["position"], "route": rule["route"], "basis": rule["basis"],
              "recommendation": rule["recommendation"],
              "citation_ids": [c["source_id"] for c in rule.get("citations", [])]}
    if os.getenv("STUB_UNSUPPORTED_OUTPUT", "false").lower() == "true":
        output["citation_ids"].append("UNRETRIEVED-CLAUSE")
    return {"output": output,
            "usage": {"prompt_tokens": max(1, len(request.prompt + json.dumps(request.payload)) // 4),
                      "completion_tokens": max(1, len(json.dumps(output)) // 4)}}
