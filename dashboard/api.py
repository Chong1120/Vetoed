"""
dashboard/api.py - FastAPI read-only view over the decision journal.

Read-only by design: this process can display the agent's history but has no
route that places, cancels, or modifies an order. The dashboard cannot trade.

Every response body comes from dashboard/payload.py, which is also what
scripts/export_static.py freezes into the static GitHub Pages build - so the
hosted snapshot and this live view can never disagree.

    .venv\Scripts\python.exe -m uvicorn dashboard.api:app --reload --port 8000
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard import payload

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="Vetoed", docs_url="/api/docs")


@app.get("/api/summary")
def summary() -> JSONResponse:
    return JSONResponse(payload.summary())


@app.get("/api/equity")
def equity() -> JSONResponse:
    return JSONResponse(payload.equity())


@app.get("/api/decisions")
def decisions(limit: int = 100) -> JSONResponse:
    return JSONResponse(payload.decisions(limit))


@app.get("/api/orders")
def orders(limit: int = 100) -> JSONResponse:
    return JSONResponse(payload.orders(limit))


@app.get("/api/positions")
def positions() -> JSONResponse:
    return JSONResponse(payload.positions())


@app.get("/api/broker_positions")
def broker_positions() -> JSONResponse:
    """What Alpaca holds, not what we believe it holds."""
    return JSONResponse(payload.broker_positions())


@app.get("/api/runs")
def runs(limit: int = 50) -> JSONResponse:
    return JSONResponse(payload.runs(limit))


@app.get("/api/health")
def health() -> JSONResponse:
    """Liveness for unattended operation. Returns 503 when the agent is stale,
    so a plain uptime monitor can watch this URL without parsing the body."""
    data = payload.health()
    return JSONResponse(data, status_code=503 if data.get("stale") else 200)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC, "index.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
