from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from .main import index
from .telemetry import configure
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
import time

app=FastAPI(title="Claim Triage Dashboard")
configure("dashboard")
FastAPIInstrumentor.instrument_app(app)
STARTED = time.monotonic()

@app.get("/",response_class=HTMLResponse)
def dashboard(): return index()

@app.get("/healthz")
def health(): return {"status": "ok", "uptime_seconds": int(time.monotonic()-STARTED)}

@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return ("# TYPE dashboard_up gauge\ndashboard_up 1\n"
            f"# TYPE dashboard_uptime_seconds gauge\ndashboard_uptime_seconds {int(time.monotonic()-STARTED)}\n")
