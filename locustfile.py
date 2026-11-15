import time
import uuid

from locust import HttpUser, task, between


class ClaimUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task
    def submit_and_wait(self):
        submitted_at = time.monotonic()
        client_id = f"load-{uuid.uuid4()}"
        claim = {"disruption": "cancellation", "flight_number": "AI101",
                 "scheduled_departure": "2026-11-01T12:00:00+05:30", "block_time_minutes": 120,
                 "notice_minutes": 90, "alternate_flight_provided": True, "alternate_accepted": True,
                 "alternate_delay_minutes": 240, "passenger_reported_at_airport": True,
                 "contact_details_provided": True, "cause": "operational", "cause_disputed": False,
                 "free_text": "Flight cancelled. I accepted an alternate four hours later. Please review meals during my wait."}
        with self.client.post("/v1/claims", json=claim, headers={"X-Client-Id": client_id}, catch_response=True) as response:
            if response.status_code != 202:
                response.failure(f"submit HTTP {response.status_code}")
                return
            run_id = response.json()["run_id"]
        while time.monotonic() - submitted_at < 30:
            # catch_response=True is required before calling success()/failure().
            with self.client.get(f"/v1/runs/{run_id}", name="poll claim run", catch_response=True) as poll:
                if poll.status_code != 200:
                    # Count API errors as failures; never let them show as successful polls.
                    poll.failure(f"poll HTTP {poll.status_code}")
                    time.sleep(.1)
                    continue
                try:
                    status = poll.json().get("status")
                except ValueError:
                    poll.failure("poll returned invalid JSON")
                    time.sleep(.1)
                    continue
                if status == "failed":
                    poll.failure("run failed")
                    self.environment.events.request.fire(request_type="RUN", name="submission-to-result",
                        response_time=(time.monotonic()-submitted_at)*1000, response_length=0,
                        exception=RuntimeError("run failed"))
                    return
                poll.success()
            if status == "completed":
                self.environment.events.request.fire(request_type="RUN", name="submission-to-result",
                    response_time=(time.monotonic()-submitted_at)*1000, response_length=0, exception=None)
                return
            time.sleep(.1)
        self.environment.events.request.fire(request_type="RUN", name="submission-to-result timeout", response_time=30000, response_length=0, exception=TimeoutError())
