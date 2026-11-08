"""Provider boundary with deadlines, bounded retries, shared circuit breaker and cost accounting."""
import json
import os
import time

import httpx

from .main import REDIS_URL
from redis import Redis

r = Redis.from_url(REDIS_URL, decode_responses=True)


def call_advisor(claim, passages, rule_result, run_id, step_name):
    call_started = time.monotonic()
    provider = os.getenv("MODEL_PROVIDER", "stub").strip().lower()
    if r.get("provider:open") == "false":
        raise RuntimeError("provider circuit is open")
    deadline = call_started + float(os.getenv("MODEL_MAX_TOTAL_SECONDS", "12"))
    per_try_timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "8"))
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
                    time.sleep(min(.2 * (2 ** attempt), .8, max(0, deadline-time.monotonic())))
        if response is None:
            failures = r.incr("provider:failures")
            if failures >= 3:
                r.set("provider:open", "false", ex=int(os.getenv("MODEL_CIRCUIT_COOLDOWN_SECONDS", "30")))
            raise RuntimeError("stub provider failed after bounded retries") from err
    elif provider == "openai_compatible":
        if r.get("provider:open" ) == "false":
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
                    json={"model": model, "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": json.dumps(payload)}], "response_format": {"type": "json_object"}},
                    timeout=min(per_try_timeout, remaining))
                res.raise_for_status()
                body = res.json()
                response = json.loads(body["choices"][0]["message"]["content"])
                usage = body.get("usage", {})
                r.set("provider:failures", 0)
                r.set("provider:open", "true")
                break
            except Exception as exc:
                err = exc
                if attempt < 2:
                    time.sleep(min(.25 * (2 ** attempt), 1.0, max(0, deadline-time.monotonic())))
        if response is None:
            failures = r.incr("provider:failures")
            if failures >= 3:
                r.set("provider:open", "false", ex=int(os.getenv("MODEL_CIRCUIT_COOLDOWN_SECONDS", "30")))
            raise RuntimeError("provider failed after bounded retries") from err
    else:
        raise RuntimeError("MODEL_PROVIDER must be stub or openai_compatible")
    input_rate = float(os.getenv("MODEL_INPUT_COST_PER_1K", "0"))
    output_rate = float(os.getenv("MODEL_OUTPUT_COST_PER_1K", "0"))
    in_tokens, out_tokens = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    cost = in_tokens/1000*input_rate + out_tokens/1000*output_rate
    r.set("provider:failures", 0)
    r.set("provider:open", "true")
    return {"output": response, "prompt_version": prompt_version, "provider": provider,
            "prompt_tokens": in_tokens, "completion_tokens": out_tokens, "cost": cost, "step": step_name,
            "latency_ms": round((time.monotonic()-call_started)*1000, 2)}
