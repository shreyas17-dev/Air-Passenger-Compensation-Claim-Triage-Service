"""Prometheus metrics for one intake or reviewer process."""
import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

AGENT = "intake" if os.getenv("SERVICE_ROLE") == "intake-agent" else "entitlement_reviewer"
REGISTRY = CollectorRegistry()
PROCESS_UPTIME = Gauge("agent_process_uptime_seconds", "Uptime of this agent process", ["agent"], registry=REGISTRY)
AGENT_RUNS = Counter("agent_process_runs_total", "Agent tasks by outcome", ["agent", "result"], registry=REGISTRY)
AGENT_RETRIES = Counter("agent_process_retries_total", "Additional attempts made by the agent", ["agent"], registry=REGISTRY)
AGENT_TOOL_CALLS = Counter("agent_process_tool_calls_total", "Agent tool calls", ["agent", "tool"], registry=REGISTRY)
AGENT_STEP_LATENCY = Histogram("agent_process_step_latency_seconds", "Agent step latency", ["agent", "step"], registry=REGISTRY)
AGENT_TASK_LATENCY = Histogram("agent_process_task_latency_seconds", "Agent task latency", ["agent"], registry=REGISTRY)


def start_metrics_server():
    port = int(os.getenv("AGENT_METRICS_PORT", "9101" if AGENT == "intake" else "9102"))
    start_http_server(port, addr="0.0.0.0", registry=REGISTRY)
