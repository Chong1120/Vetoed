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
from datetime import datetime, timezone

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


@app.get("/api/selectivity")
def selectivity() -> JSONResponse:
    """Cumulative funnel - how much of what it saw did it take."""
    return JSONResponse(payload.selectivity())


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


# --------------------------------------------------------------------------- #
# Live view - LOCAL ONLY, and deliberately so.
#
# This asks Alpaca directly rather than reading the journal, so it is current
# to the second instead of to the last cycle. It exists only in this process,
# which holds the API credentials in its own environment and never ships them
# anywhere.
#
# The published GitHub Pages build CANNOT have this. A static page has no
# server, so for the browser to call Alpaca the keys would have to be embedded
# in the page itself - readable by anyone who opens it, and sufficient to place
# trades in the account. The static snapshot is the correct trade-off there:
# the URL never sleeps, and it leaks nothing.
#
# Read-only, like the rest of this module. No route here can trade.
# --------------------------------------------------------------------------- #

@app.get("/api/live")
def live() -> JSONResponse:
    """Positions and account, straight from Alpaca. 503 if unavailable."""
    # Same as every other entry point: read .env when running locally. In CI
    # and on Pages there is no .env and no credentials, which is the point -
    # this endpoint then reports unavailable and the page uses the snapshot.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        return JSONResponse({"available": False,
                             "reason": "no credentials in this environment"},
                            status_code=503)
    if os.getenv("ALPACA_PAPER_TRADE", "").strip().lower() != "true":
        # Same fail-closed rule the agent uses. A dashboard is not a reason to
        # relax it, and reading a live-money account here is not intended.
        return JSONResponse({"available": False,
                             "reason": "ALPACA_PAPER_TRADE is not 'true'"},
                            status_code=503)
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()
        positions = client.get_all_positions()
        # The broker's own equity series, at 15-minute resolution. Ours is one
        # point per cycle and nothing overnight.
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            hist = client.get_portfolio_history(
                GetPortfolioHistoryRequest(period="1W", timeframe="15Min"))
        except Exception:
            hist = None                     # never fail the whole call for it
    except Exception as exc:                        # network, auth, rate limit
        return JSONResponse({"available": False,
                             "reason": "%s: %s" % (type(exc).__name__, exc)},
                            status_code=503)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    series = []
    if hist is not None:
        # Alpaca pads with zeros where the account did not exist yet; plotting
        # those draws a cliff to the axis that never happened.
        for t, v in zip(getattr(hist, "timestamp", None) or [],
                        getattr(hist, "equity", None) or []):
            e = num(v)
            if e and e > 0:
                series.append({"t": int(t), "equity": e})

    return JSONResponse({
        "available": True,
        "equity_series": series,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "equity": num(acct.equity),
        "last_equity": num(acct.last_equity),
        "cash": num(acct.cash),
        "buying_power": num(acct.buying_power),
        "positions": [{
            "symbol": p.symbol,
            "qty": num(p.qty),
            "avg_price": num(p.avg_entry_price),
            "current_price": num(p.current_price),
            "market_value": num(p.market_value),
            "unrealised": num(p.unrealized_pl),
        } for p in positions],
    })
