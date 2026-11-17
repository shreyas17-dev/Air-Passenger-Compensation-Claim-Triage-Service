"""End-to-end load test for the claim triage service.

Each simulated passenger submits a schema-valid synthetic claim and then polls
until the run reaches a final state, so the measured latency covers submission,
queueing, both agents and verification, not only the HTTP 202.

Locust rows:
  RUN  submission-to-result          one sample per claim that reached a final state
                                     (failure = run status "failed"); this exact
                                     name is parsed by app/benchmark_report.py
  RUN  submission-to-result timeout  claims that did not finish within RUN_TIMEOUT_SECONDS
  POST /v1/claims, GET poll claim run individual HTTP calls; non-2xx counted as failures

Configuration (all optional):
  -u / -r / --run-time  or LOCUST_USERS / LOCUST_SPAWN_RATE / LOCUST_RUN_TIME
  LOCUST_SEED              integer seed for reproducible claim mixes
  RUN_TIMEOUT_SECONDS      default 30
  POLL_INTERVAL_SECONDS    default 0.1 (kept equal to earlier measurements)
  THINK_MIN / THINK_MAX    pause between a passenger's claims, default 0.05 / 0.2 s
  LOCUST_SUMMARY           JSON outcome summary path, default eval/locust-summary.json

Malformed and adversarial requests live in locust_negative.py so a normal
benchmark never mixes them into the latency figures.
"""
import json
import os
import random
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from locust import HttpUser, between, events, task

IST = timezone(timedelta(hours=5, minutes=30))
RUN_TIMEOUT = float(os.getenv("RUN_TIMEOUT_SECONDS", "30"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "0.1"))
_seed = os.getenv("LOCUST_SEED")
_rng = random.Random(int(_seed)) if _seed else random.Random()
_rng_lock = threading.Lock()

# Fictional flight designators: real airline prefixes, invented numbers.
AIRLINE_PREFIXES = ["AI", "6E", "UK", "SG", "QP", "IX"]
DISRUPTION_WEIGHTS = {"cancellation": 0.55, "delay": 0.2, "denied_boarding": 0.15, "downgrading": 0.1}
CAUSES = ["operational", "technical fault", "crew shortage", "aircraft rotation", "scheduling change"]

# Free-text templates. Each mentions only its own disruption type (the intake agent
# escalates text that names a different category) and avoids the API's
# instruction-injection and abuse patterns.
TEXT = {
    "cancellation": [
        "My flight {flight} was cancelled {notice_text}. {alt_text}",
        "The airline cancelled {flight} {notice_text}. {alt_text} Please review my entitlement.",
        "{flight} was cancelled {notice_text}. {alt_text} I would like this checked.",
    ],
    "delay": [
        "My flight {flight} was delayed by about {delay_h} hours and I waited at the terminal.",
        "{flight} had a delay of roughly {delay_h} hours. I would like to know what facilities apply.",
    ],
    "denied_boarding": [
        "I was denied boarding on {flight} although I had a confirmed ticket and reported on time.",
        "The gate staff said {flight} was full and I was not allowed to board.",
    ],
    "downgrading": [
        "I was downgraded on {flight} from business to economy class without my agreement.",
        "My seat on {flight} was moved to a lower class than I booked; I was downgraded at the gate.",
    ],
}


def _notice_text(minutes):
    if minutes is None:
        return "but I do not remember when I was told"
    if minutes < 60:
        return f"only {minutes} minutes before departure"
    if minutes < 1440:
        return f"about {round(minutes / 60)} hours before departure"
    return f"{round(minutes / 1440)} days before departure"


def synthetic_claim(rng=None):
    """Return one schema-valid synthetic claim. No real passenger data is used."""
    rng = rng or _rng
    with _rng_lock:
        disruption = rng.choices(list(DISRUPTION_WEIGHTS), weights=list(DISRUPTION_WEIGHTS.values()))[0]
        flight = f"{rng.choice(AIRLINE_PREFIXES)}{rng.randint(100, 9999)}"
        departure = (datetime.now(IST) + timedelta(days=rng.randint(-30, 30), minutes=rng.randrange(0, 1440, 5))).replace(second=0, microsecond=0)
        block = rng.choice([45, 60, 75, 90, 110, 120, 150, 180, 240, 330])
        # Notice bands: < 24 h, 24 h to 2 weeks, 2 weeks or more, unknown.
        band = rng.choices(["short", "medium", "long", "unknown"], weights=[0.55, 0.2, 0.15, 0.1])[0]
        notice = {"short": rng.randint(15, 1439), "medium": rng.randint(1440, 20159),
                  "long": rng.randint(20160, 60000), "unknown": None}[band]
        alt_offered = rng.choices([True, False, None], weights=[0.55, 0.35, 0.1])[0]
        alt_accepted = rng.choice([True, False, None]) if alt_offered else (False if alt_offered is False and rng.random() < 0.5 else None)
        alt_delay = rng.randrange(60, 721, 30) if alt_offered and rng.random() < 0.6 else None
        with_fare = rng.random() < 0.5
        fare = float(rng.randrange(2500, 15001, 50)) if with_fare else None
        fuel = float(rng.randrange(300, 2501, 50)) if with_fare else None
        reported = rng.choices([True, False, None], weights=[0.6, 0.3, 0.1])[0]
        contact = rng.choices([True, False, None], weights=[0.8, 0.1, 0.1])[0]
        disputed = rng.random() < 0.05
        cause = rng.choice(CAUSES)
        template = rng.choice(TEXT[disruption])
        delay_h = rng.randint(2, 7)
    if alt_offered and alt_accepted:
        alt_text = "I accepted the alternate flight the airline offered."
    elif alt_offered and alt_accepted is False:
        alt_text = "An alternate flight was offered but it did not suit me."
    elif alt_offered is False:
        alt_text = "No alternate flight was offered."
    else:
        alt_text = "I am not sure whether an alternate flight was arranged."
    claim = {
        "claim_id": f"synthetic-{uuid.uuid4().hex[:12]}",
        "disruption": disruption,
        "flight_number": flight,
        "scheduled_departure": departure.isoformat(),
        "block_time_minutes": block,
        "notice_minutes": notice,
        "alternate_flight_provided": alt_offered,
        "alternate_accepted": alt_accepted,
        "alternate_delay_minutes": alt_delay,
        "booked_one_way_basic_fare_inr": fare,
        "airline_fuel_charge_inr": fuel,
        "passenger_reported_at_airport": reported,
        "contact_details_provided": contact,
        "cause": cause,
        "cause_disputed": disputed,
        "free_text": template.format(flight=flight, notice_text=_notice_text(notice), alt_text=alt_text, delay_h=delay_h),
    }
    if disruption == "delay" and rng.random() < 0.5:
        claim["actual_departure"] = (departure + timedelta(hours=delay_h)).isoformat()
    return claim


# ---- Outcome bookkeeping, separate from Locust's request stats ----
_outcomes_lock = threading.Lock()
OUTCOMES = Counter()
POSITIONS = Counter()
DISRUPTIONS = Counter()
HTTP_ERRORS = Counter()


def _count(counter, key):
    with _outcomes_lock:
        counter[key] += 1


def _fire(env, name, started, exception=None):
    env.events.request.fire(request_type="RUN", name=name, response_time=(time.monotonic() - started) * 1000,
                            response_length=0, exception=exception, context={})


class ClaimUser(HttpUser):
    """A passenger who files one claim at a time and waits for its outcome."""
    wait_time = between(float(os.getenv("THINK_MIN", "0.05")), float(os.getenv("THINK_MAX", "0.2")))

    @task
    def submit_and_wait(self):
        claim = synthetic_claim()
        _count(DISRUPTIONS, claim["disruption"])
        started = time.monotonic()
        # A fresh client id per claim keeps the per-client rate limit (30/min) from
        # turning the benchmark into a measurement of HTTP 429 responses.
        headers = {"X-Client-Id": f"load-{uuid.uuid4().hex}"}
        with self.client.post("/v1/claims", json=claim, headers=headers, catch_response=True) as response:
            if response.status_code != 202:
                _count(HTTP_ERRORS, f"submit {response.status_code}")
                _count(OUTCOMES, "submit_http_error")
                response.failure(f"submit HTTP {response.status_code}: {response.text[:200]}")
                return
            try:
                run_id = response.json()["run_id"]
            except (ValueError, KeyError):
                _count(OUTCOMES, "submit_http_error")
                response.failure("submit response had no run_id")
                return
        while time.monotonic() - started < RUN_TIMEOUT:
            time.sleep(POLL_INTERVAL)
            with self.client.get(f"/v1/runs/{run_id}", name="poll claim run", catch_response=True) as poll:
                if poll.status_code != 200:
                    _count(HTTP_ERRORS, f"poll {poll.status_code}")
                    poll.failure(f"poll HTTP {poll.status_code}")
                    continue
                try:
                    body = poll.json()
                except ValueError:
                    _count(HTTP_ERRORS, "poll invalid-json")
                    poll.failure("poll returned invalid JSON")
                    continue
                poll.success()
            status = body.get("status")
            if status == "completed":
                position = (body.get("result") or {}).get("position", "unknown")
                _count(POSITIONS, position)
                _count(OUTCOMES, "completed")
                _fire(self.environment, "submission-to-result", started)
                return
            if status == "failed":
                _count(OUTCOMES, "run_failed")
                _fire(self.environment, "submission-to-result", started, RuntimeError("run failed"))
                return
        _count(OUTCOMES, "timeout")
        _fire(self.environment, "submission-to-result timeout", started, TimeoutError(f"no final state after {RUN_TIMEOUT:.0f}s"))


@events.test_stop.add_listener
def _write_summary(environment, **_):
    with _outcomes_lock:
        finished = OUTCOMES["completed"] + OUTCOMES["run_failed"]
        attempted = sum(OUTCOMES.values())
        if not attempted:
            # Nothing from ClaimUser ran (e.g. locust_negative.py imported this module).
            return
        run = environment.stats.get("submission-to-result", "RUN")
        summary = {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "users": getattr(environment.runner, "target_user_count", None),
            "outcomes": dict(OUTCOMES), "positions": dict(POSITIONS),
            "disruptions": dict(DISRUPTIONS), "http_errors": dict(HTTP_ERRORS),
            "claims_attempted": attempted,
            "failure_rate": round((attempted - OUTCOMES["completed"]) / attempted, 4) if attempted else None,
            "submission_to_result_ms": {
                "samples": run.num_requests, "p50": run.get_response_time_percentile(0.5) if run.num_requests else None,
                "p95": run.get_response_time_percentile(0.95) if run.num_requests else None,
                "p99": run.get_response_time_percentile(0.99) if run.num_requests else None,
                "max": run.max_response_time if run.num_requests else None,
                "completed_per_second": round(run.total_rps, 3),
            },
            "claims_finished": finished,
            "note": "positions are triage outcomes of completed runs; human_escalation is a valid completed result",
        }
    print("\n=== claim outcome summary ===\n" + json.dumps(summary, indent=2))
    path = os.getenv("LOCUST_SUMMARY", "eval/locust-summary.json")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(summary, indent=2), encoding="utf8")
    except OSError as exc:
        print(f"could not write {path}: {exc}")
