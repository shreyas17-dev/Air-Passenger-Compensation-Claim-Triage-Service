"""Reviewer agent: determine a route only from handed-off evidence, verify citations."""
import json
import logging
import os
import re
import socket
import time

from redis import Redis
from .main import REDIS_URL, record_event
from .agents import adjudicate, search_corpus, verify
from .provider import call_advisor
from .telemetry import configure, traced

logging.basicConfig(level=logging.INFO, format="%(message)s")
r = Redis.from_url(REDIS_URL, decode_responses=True)
configure("review-agent")
PROCESS_STARTED = time.monotonic()
PROMPT_VERSION = "triage-review-v1"


@traced("entitlement_reviewer")
def process(item):
    rid, claim, facts = item["run_id"], item["claim"], item["facts"]
    started = item.get("started", time.monotonic())
    review_started = time.monotonic()
    review_tool_calls = 1
    try:
        record_event(rid, "agent_started", agent="entitlement_reviewer", prompt_version=PROMPT_VERSION)
        retrieved = item["passages"]
        result = adjudicate(claim, facts, retrieved)
        model_call = call_advisor(claim, retrieved, result, rid, "entitlement_review")
        record_event(rid, "model_call", agent="entitlement_reviewer", step=model_call["step"],
                     provider=model_call["provider"], prompt_version=model_call["prompt_version"],
                     prompt_tokens=model_call["prompt_tokens"], completion_tokens=model_call["completion_tokens"],
                     cost=model_call["cost"], latency_ms=model_call["latency_ms"])
        r.hincrbyfloat(f"cost:run:{rid}", "usd", model_call["cost"])
        r.hincrbyfloat("cost:total", "usd", model_call["cost"])
        r.hincrby("tokens:prompt", "count", model_call["prompt_tokens"])
        r.hincrby("tokens:completion", "count", model_call["completion_tokens"])
        advisory = model_call["output"]
        if advisory.get("position") != result.get("position") or advisory.get("route") != result.get("route"):
            record_event(rid, "provider_output_verification", agent="entitlement_reviewer", verified=False, reason="route_mismatch", fallback="human_escalation")
            raise ValueError("provider output requested an unsupported determination")
        advisory_text = advisory.get("recommendation", "")
        if advisory_text != result.get("recommendation"):
            record_event(rid, "provider_output_verification", agent="entitlement_reviewer", verified=False, reason="unsupported_recommendation", fallback="human_escalation")
            raise ValueError("provider output did not match the verified rule-grounded recommendation")
        if re.search(r"(?i)(payment is authorized|guaranteed compensation|pay immediately|ignore (all|previous) instructions|system prompt)", advisory_text):
            raise ValueError("provider output failed authorization or injection filter")
        if re.search(r"(?i)\b(fuck|shit|bitch|asshole|bastard)\b", advisory_text):
            raise ValueError("provider output failed toxicity filter")
        expected_citations = {c["source_id"] for c in result.get("citations", [])}
        if set(advisory.get("citation_ids", [])) != expected_citations or expected_citations - {p["id"] for p in retrieved}:
            record_event(rid, "provider_output_verification", agent="entitlement_reviewer", verified=False, reason="citation_not_retrieved", fallback="human_escalation")
            raise ValueError("provider output contains unsupported citations")
        result["recommendation"] = advisory.get("recommendation", result["recommendation"])
        result["token_usage"] = {"prompt_tokens": model_call["prompt_tokens"],
                                 "completion_tokens": model_call["completion_tokens"], "cost": model_call["cost"]}
        result["prompt_version"] = model_call["prompt_version"]
        ok, reason = verify(result, retrieved)
        record_event(rid, "verification", agent="entitlement_reviewer", verified=ok, reason=reason,
                     citations=[c["source_id"] for c in result.get("citations", [])])
        if not ok:
            query = f"conditions exception exemption {claim.get('disruption')} alternate notice facilities"
            retry = search_corpus(query, strategy="condition_focused", limit=8)
            review_tool_calls += 1
            retrieved = list({p["id"]: p for p in retrieved + retry}.values())
            result = adjudicate(claim, facts, retrieved)
            result["token_usage"] = {"prompt_tokens": model_call["prompt_tokens"], "completion_tokens": model_call["completion_tokens"], "cost": model_call["cost"]}
            result["prompt_version"] = model_call["prompt_version"]
            ok, reason = verify(result, retrieved)
            record_event(rid, "verification_retry", query=query, verified=ok, reason=reason,
                         returned=[p["id"] for p in retry])
        if not ok:
            result = {"position": "human_escalation", "route": "human_review", "basis": "verification failed",
                      "recommendation": "Could not verify an evidence-grounded answer. Officer review required.",
                      "citations": [], "authority_note": "No payment is authorised."}
        result.update({"run_id": rid, "correlation_id": rid, "prompt_version": model_call["prompt_version"],
                       "retrieved_passages": [{"source_id": p["id"], "clause": p["clause"], "text": p["text"]} for p in retrieved],
                       "latency_ms": round((time.monotonic()-started)*1000, 2),
                       "agent_steps": item.get("intake_steps", 1) + 1,
                       "tool_calls": item.get("intake_tool_calls", 0) + review_tool_calls})
        r.hset(f"run:{rid}", mapping={"status": "completed", "updated_at": time.time(), "result": json.dumps(result)})
        r.incr("runs:completed")
        r.incr("agent:success:entitlement_reviewer")
        r.hincrby("agent:metrics:entitlement_reviewer", "steps", 1)
        r.hincrby("agent:metrics:entitlement_reviewer", "tool_calls", review_tool_calls)
        r.hincrbyfloat("agent:metrics:entitlement_reviewer", "latency_ms_total", round((time.monotonic()-review_started)*1000, 2))
        r.hincrby("agent:metrics:entitlement_reviewer", "runs", 1)
        r.lpush("latencies:run", result["latency_ms"])
        r.ltrim("latencies:run", 0, 9999)
        if int(r.incr("quality:sample_counter")) % 10 == 0:
            retrieved_ids = {p["id"] for p in retrieved}
            citation_ids = {c["source_id"] for c in result.get("citations", [])}
            quality = {"at": time.time(), "run_id": rid,
                       "groundedness": float(ok and citation_ids.issubset(retrieved_ids)),
                       "context_relevance": len(citation_ids & retrieved_ids)/max(len(citation_ids), 1),
                       "faithfulness": float(ok), "hallucination_rate": float(not ok)}
            r.lpush("quality:samples", json.dumps(quality))
            r.ltrim("quality:samples", 0, 999)
            record_event(rid, "quality_sample", **quality)
        record_event(rid, "run_completed", position=result["position"], latency_ms=result["latency_ms"])
    except Exception as e:
        logging.exception(json.dumps({"run_id": rid, "event": "review_failed"}))
        # Reviewer failure is an escalation, never a confident partial result.
        failed_usage = {"prompt_tokens": model_call["prompt_tokens"], "completion_tokens": model_call["completion_tokens"], "cost": model_call["cost"]} if "model_call" in locals() else {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}
        fallback = {"position": "human_escalation", "route": "human_review", "basis": "reviewer unavailable",
                    "recommendation": "Automated review did not complete; customer relations officer review required.",
                    "citations": [], "authority_note": "No payment is authorised.",
                    "run_id": rid, "correlation_id": rid, "prompt_version": os.getenv("PROMPT_VERSION", PROMPT_VERSION),
                    "token_usage": failed_usage,
                    "retrieved_passages": item.get("passages", []), "latency_ms": round((time.monotonic()-started)*1000, 2)}
        r.hset(f"run:{rid}", mapping={"status": "completed", "updated_at": time.time(), "result": json.dumps(fallback)})
        r.incr("agent:failures:entitlement_reviewer")
        r.incr("runs:completed")
        r.lpush("latencies:run", fallback["latency_ms"])
        r.ltrim("latencies:run", 0, 9999)
        record_event(rid, "agent_failed", agent="entitlement_reviewer", error=type(e).__name__, fallback="human_escalation")


def main():
    logging.info(json.dumps({"event": "entitlement_reviewer_started"}))
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    try:
        r.xgroup_create("review_queue", "review", id="0", mkstream=True)
    except Exception:
        pass
    while True:
        try:
            reclaimed = r.xautoclaim("review_queue", "review", consumer, min_idle_time=60000, start_id="0", count=1)
            messages = reclaimed[1]
        except Exception:
            messages = []
        if not messages:
            batch = r.xreadgroup("review", consumer, {"review_queue": ">"}, count=1, block=5000)
            messages = batch[0][1] if batch else []
        r.set("agent:uptime:entitlement_reviewer", int(time.monotonic()-PROCESS_STARTED))
        for msg_id, fields in messages:
            try:
                process(json.loads(fields["payload"]))
            finally:
                r.xack("review_queue", "review", msg_id)


if __name__ == "__main__":
    main()
