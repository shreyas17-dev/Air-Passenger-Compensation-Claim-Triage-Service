"""Intake agent: classify/extract, select retrieval tools, then hand off evidence."""
import json
import logging
import os
import socket
import time

from redis import Redis
from .main import REDIS_URL, record_event
from .agents import classify_extract, lookup_clause, lookup_claim_field
from .agent_graph import retrieve_with_agent
from .telemetry import configure, traced

logging.basicConfig(level=logging.INFO, format="%(message)s")
r = Redis.from_url(REDIS_URL, decode_responses=True)
configure("intake-agent")
PROCESS_STARTED = time.monotonic()
PROMPT_VERSION = "triage-intake-v1"


@traced("intake")
def process(item):
    rid, claim = item["run_id"], item["claim"]
    started = item.get("submitted_monotonic", time.monotonic())
    r.hset(f"run:{rid}", mapping={"status": "running", "updated_at": time.time()})
    try:
        record_event(rid, "agent_started", agent="intake", prompt_version=PROMPT_VERSION)
        facts = classify_extract(claim)
        fields = {key: lookup_claim_field(claim, key) for key in ("notice_minutes", "block_time_minutes", "alternate_flight_provided", "alternate_accepted", "contact_details_provided", "passenger_reported_at_airport")}
        record_event(rid, "agent_step", agent="intake", step="classify_extract", latency_ms=round((time.monotonic()-started)*1000, 2), fields=facts)
        record_event(rid, "tool_call", tool="claim_field_lookup", fields=fields)
        t = time.monotonic()
        passages, tool_calls, agent_steps, tool_events = retrieve_with_agent(claim)
        retrieved = {p["id"]: p for p in passages}
        record_event(rid, "agent_tool_loop", framework="LangGraph", agent="intake",
                     steps=agent_steps, tool_calls=tool_calls, retrieved=list(retrieved),
                     latency_ms=round((time.monotonic()-t)*1000, 2))
        for tool_event in tool_events:
            record_event(rid, "agent_tool", agent="intake", **tool_event)
        if time.monotonic() - started > float(os.getenv("AGENT_MAX_WALL_SECONDS", "20")):
            raise TimeoutError("intake agent exceeded wall-clock bound")
        trace_context = {}
        from opentelemetry import propagate
        propagate.inject(trace_context)
        handoff = {"run_id": rid, "claim": claim, "facts": facts, "passages": list(retrieved.values()), "started": started, "intake_steps": agent_steps, "intake_tool_calls": tool_calls, "trace_context": trace_context}
        r.xadd("review_queue", {"payload": json.dumps(handoff)})
        record_event(rid, "agent_handoff", from_agent="intake", to_agent="entitlement_reviewer",
                     evidence_ids=list(retrieved), reason="extracted facts and linked conditions ready")
        r.incr("agent:success:intake")
        r.hincrby("agent:metrics:intake", "steps", agent_steps)
        r.hincrby("agent:metrics:intake", "tool_calls", tool_calls)
        r.hincrby("agent:metrics:intake", "latency_ms_total", round((time.monotonic()-started)*1000))
        r.hincrby("agent:metrics:intake", "runs", 1)
    except Exception as e:
        logging.exception(json.dumps({"run_id": rid, "event": "intake_failed"}))
        r.hset(f"run:{rid}", mapping={"status": "failed", "updated_at": time.time(), "error": "Intake agent failed; resubmit or request officer review."})
        r.incr("runs:failed")
        r.incr("agent:failures:intake")
        record_event(rid, "agent_failed", agent="intake", error=type(e).__name__)


def main():
    logging.info(json.dumps({"event": "intake_agent_started"}))
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    try:
        r.xgroup_create("claim_queue", "intake", id="0", mkstream=True)
    except Exception:
        pass
    while True:
        # Recover abandoned deliveries after a crashed worker before reading new work.
        try:
            reclaimed = r.xautoclaim("claim_queue", "intake", consumer, min_idle_time=60000, start_id="0", count=1)
            messages = reclaimed[1]
        except Exception:
            messages = []
        if not messages:
            batch = r.xreadgroup("intake", consumer, {"claim_queue": ">"}, count=1, block=5000)
            messages = batch[0][1] if batch else []
        r.set("agent:uptime:intake", int(time.monotonic()-PROCESS_STARTED))
        for msg_id, fields in messages:
            try:
                process(json.loads(fields["payload"]))
            finally:
                r.xack("claim_queue", "intake", msg_id)


if __name__ == "__main__":
    main()
