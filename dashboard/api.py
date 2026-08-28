"""
dashboard/api.py - FastAPI read-only view over the decision journal.

Read-only by design: this process can display the agent's history but has no
route that places, cancels, or modifies an order. The dashboard cannot trade.

    .venv\\Scripts\\python.exe -m uvicorn dashboard.api:app --reload --port 8000
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent import journal

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

app = FastAPI(title="Alpha Options Agent", docs_url="/api/docs")


@app.get("/api/summary")
def summary() -> JSONResponse:
    journal.init()
    curve = journal.equity_curve()
    orders = journal.all_orders()
    open_rows = journal.open_spreads()
    runs = journal.recent_runs(1)

    start = curve[0]["equity"] if curve else None
    latest = curve[-1]["equity"] if curve else None
    realised = sum(float(o["realised_pnl"] or 0) for o in orders
                   if o.get("realised_pnl") is not None)
    filled = [o for o in orders if (o.get("filled_qty") or 0) > 0]

    return JSONResponse({
        "equity_start": start,
        "equity_latest": latest,
        "equity_change": (latest - start) if (start and latest) else None,
        "realised_pnl": realised,
        "open_positions": len(open_rows),
        "orders_total": len(orders),
        "orders_filled": len(filled),
        "last_run": runs[0] if runs else None,
        "open_risk": sum(float(r.get("max_loss_total") or 0) for r in open_rows),
    })


@app.get("/api/equity")
def equity() -> JSONResponse:
    journal.init()
    return JSONResponse(journal.equity_curve())


@app.get("/api/decisions")
def decisions(limit: int = 100) -> JSONResponse:
    """Every decision with its full reasoning, including the vetoed ones."""
    journal.init()
    out = []
    for d in journal.recent_decisions(limit):
        d = dict(d)
        for field in ("candidate_json", "risk_reasons", "risk_vetoes"):
            try:
                d[field] = json.loads(d.get(field) or "null")
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(d)
    return JSONResponse(out)


@app.get("/api/orders")
def orders(limit: int = 100) -> JSONResponse:
    journal.init()
    return JSONResponse(journal.all_orders(limit))


@app.get("/api/positions")
def positions() -> JSONResponse:
    journal.init()
    return JSONResponse(journal.open_spreads())


@app.get("/api/runs")
def runs(limit: int = 50) -> JSONResponse:
    journal.init()
    return JSONResponse(journal.recent_runs(limit))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC, "index.html"))


if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
