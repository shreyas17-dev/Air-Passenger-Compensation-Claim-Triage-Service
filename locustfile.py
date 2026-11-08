import time
import uuid

from locust import HttpUser, task, between


class ClaimUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task
    def submit_and_wait(self):
        started = time.monotonic()
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
        started = time.monotonic()
        while time.monotonic() - started < 30:
            poll = self.client.get(f"/v1/runs/{run_id}", name="complete claim run")
            result = poll.json()
            if result.get("status") == "completed":
                poll.success()
                self.environment.events.request.fire(request_type="RUN", name="submission-to-result",
                    response_time=(time.monotonic()-started)*1000, response_length=0, exception=None)
                return
            if result.get("status") == "failed":
                poll.failure("run failed")
                return
            time.sleep(.1)
        self.environment.events.request.fire(request_type="RUN", name="submit-to-result timeout", response_time=30000, response_length=0, exception=TimeoutError())
