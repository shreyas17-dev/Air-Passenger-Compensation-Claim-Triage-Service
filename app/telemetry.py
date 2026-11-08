"""OpenTelemetry setup shared by API and agent processes."""
import os
from functools import wraps

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


def configure(service_name):
    if trace.get_tracer_provider().__class__.__name__ != "ProxyTracerProvider":
        return trace.get_tracer(__name__)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(__name__)


def traced(agent_name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(item, *args, **kwargs):
            context = propagate.extract(item.get("trace_context", {}))
            tracer = trace.get_tracer(agent_name)
            with tracer.start_as_current_span(f"agent.{agent_name}", context=context) as span:
                rid = item.get("run_id", "")
                span.set_attribute("correlation_id", rid)
                span.set_attribute("agent.name", agent_name)
                return fn(item, *args, **kwargs)
        return wrapped
    return decorate
