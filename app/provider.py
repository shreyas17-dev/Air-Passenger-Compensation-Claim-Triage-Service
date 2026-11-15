"""Provider boundary with deadlines, bounded retries, shared circuit breaker and cost accounting."""
import json
import os
import time

import httpx

from .main import REDIS_URL, record_event
from redis import Redis
from .worker_metrics import AGENT_RETRIES

r = Redis.from_url(REDIS_URL, decode_responses=True)

# Appended to every prompt version so a real model returns what the reviewer checks.
OUTPUT_CONTRACT = (
    "\n\nThe payload's rule_result is the verified decision. Return only a JSON object with keys: "
    "position and route (copy exactly from rule_result), citation_ids (exactly the source_id values in "
    "rule_result.citations), basis (copy from rule_result), and recommendation (a plain explanation for the "
    "officer, at most 120 words). Mention only clauses listed in rule_result.citations and only the amount "
    "in rule_result.calculation, if any. Never state that payment is authorised."
)


def call_advisor(claim, passages, rule_result, run_id, step_name):
    call_started = time.monotonic()
    provider = os.getenv("MODEL_PROVIDER", "stub").strip().lower()
    if r.get("provider:circuit") == "open":
        raise RuntimeError("provider circuit is open")
    deadline = call_started + float(os.getenv("MODEL_MAX_TOTAL_SECONDS", "12"))
    per_try_timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "8"))
    retry_count = 0
    prompt_version = os.getenv("PROMPT_VERSION", "triage-v2")
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", f"{prompt_version}.txt")
    try:
        prompt = open(prompt_path, encoding="utf8").read()
    except OSError:
        prompt_version, prompt = "triage-v2", "Use source evidence only; officer decides; return a JSON triage recommendation."
    payload = {"claim": claim, "evidence": passages, "rule_result": rule_result}
    if provider == "stub":
        url = os.getenv("STUB_PROVIDER_URL")
        if not url:
            raise RuntimeError("STUB_PROVIDER_URL is required when MODEL_PROVIDER=stub")
        response, usage, err = None, {}, None
        for attempt in range(3):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("provider total deadline exceeded")
                res = httpx.post(url, json={"prompt": prompt, "payload": payload},
                                 timeout=min(per_try_timeout, remaining))
                res.raise_for_status()
                body = res.json()
                response, usage = body["output"], body.get("usage", {})
                break
            except Exception as exc:
                err = exc
                if attempt < 2:
                    retry_count += 1
                    r.incr("agent:retries:entitlement_reviewer")
                    r.sadd("agent:retried_runs:entitlement_reviewer", run_id)
                    AGENT_RETRIES.labels("entitlement_reviewer").inc()
                    delay = min(.2 * (2 ** attempt), .8, max(0, deadline-time.monotonic()))
                    record_event(run_id, "provider_retry", agent="entitlement_reviewer", step=step_name,
                                 attempt=attempt+1, error=type(exc).__name__, backoff_seconds=delay)
                    time.sleep(delay)
        if response is None:
            failures = r.incr("provider:failures")
            if failures >= 3:
                r.set("provider:circuit", "open", ex=int(os.getenv("MODEL_CIRCUIT_COOLDOWN_SECONDS", "30")))
            raise RuntimeError("stub provider failed after bounded retries") from err
    elif provider == "openai_compatible":
        if r.get("provider:circuit") == "open":
            raise RuntimeError("provider circuit is open")
        api_base = os.environ.get("MODEL_API_BASE", "").rstrip("/")
        api_key = os.environ.get("MODEL_API_KEY", "")
        model = os.environ.get("MODEL_NAME", "")
        if not api_base or not api_key or not model:
            raise RuntimeError("OpenAI-compatible provider runtime configuration is incomplete")
        response = None
        err = None
        for attempt in range(3):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("provider total deadline exceeded")
                res = httpx.post(api_base + "/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "messages": [{"role": "system", "content": prompt + OUTPUT_CONTRACT},
                         {"role": "user", "content": json.dumps(payload)}], "response_format": {"type": "json_object"}},
                    timeout=min(per_try_timeout, remaining))
                res.raise_for_status()
                body = res.json()
                response = json.loads(body["choices"][0]["message"]["content"])
                usage = body.get("usage", {})
                r.set("provider:failures", 0)
                r.set("provider:circuit", "closed")
                break
            except Exception as exc:
                err = exc
                if attempt < 2:
                    retry_count += 1
                    r.incr("agent:retries:entitlement_reviewer")
                    r.sadd("agent:retried_runs:entitlement_reviewer", run_id)
                    AGENT_RETRIES.labels("entitlement_reviewer").inc()
                    delay = min(.25 * (2 ** attempt), 1.0, max(0, deadline-time.monotonic()))
                    record_event(run_id, "provider_retry", agent="entitlement_reviewer", step=step_name,
                                 attempt=attempt+1, error=type(exc).__name__, backoff_seconds=delay)
                    time.sleep(delay)
        if response is None:
            failures = r.incr("provider:failures")
            if failures >= 3:
                r.set("provider:circuit", "open", ex=int(os.getenv("MODEL_CIRCUIT_COOLDOWN_SECONDS", "30")))
            raise RuntimeError("provider failed after bounded retries") from err
    else:
        raise RuntimeError("MODEL_PROVIDER must be stub or openai_compatible")
    input_rate = float(os.getenv("MODEL_INPUT_COST_PER_1K", "0"))
    output_rate = float(os.getenv("MODEL_OUTPUT_COST_PER_1K", "0"))
    in_tokens, out_tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    cost = in_tokens/1000*input_rate + out_tokens/1000*output_rate
    r.set("provider:failures", 0)
    r.set("provider:circuit", "closed")
    return {"output": response, "prompt_version": prompt_version, "provider": provider,
            "prompt_tokens": in_tokens, "completion_tokens": out_tokens, "cost": cost, "step": step_name,
            "retry_count": retry_count,
            "latency_ms": round((time.monotonic()-call_started)*1000, 2)}
