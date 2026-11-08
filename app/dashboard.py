from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from .main import index
from .telemetry import configure
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
app=FastAPI(title="Claim Triage Dashboard")
configure("dashboard")
FastAPIInstrumentor.instrument_app(app)
@app.get("/",response_class=HTMLResponse)
def dashboard(): return index()
