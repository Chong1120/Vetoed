"""
payload.py - the dashboard's data, built with nothing but the standard library.

Both consumers read from here so they can never drift apart:

    dashboard/api.py          serves these live over HTTP  (local)
    scripts/export_static.py  freezes them into data.json   (GitHub Pages)

That matters because the static export is the submitted demo URL. If the
exporter reimplemented this logic it would quietly diverge from what the live
dashboard shows, and the frozen page would be subtly wrong.

Deliberately free of FastAPI so the exporter needs no dependencies at all -
`python scripts/export_static.py` runs on a bare CI runner.
"""

from __future__ import annotations

import json
import os

from agent import journal


def _decode(row: dict, *fields: str) -> dict:
    """Parse JSON-encoded columns in place, leaving bad values untouched."""
    row = dict(row)
    for f in fields:
        try:
            row[f] = json.loads(row.get(f) or "null")
        except (json.JSONDecodeError, TypeError):
            pass
    return row


def summary() -> dict:
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

    return {
        "equity_start": start,
        "equity_latest": latest,
        "equity_change": (latest - start) if (start and latest) else None,
        "realised_pnl": realised,
        "open_positions": len(open_rows),
        "orders_total": len(orders),
        "orders_filled": len(filled),
        "last_run": runs[0] if runs else None,
        "open_risk": sum(float(r.get("max_loss_total") or 0) for r in open_rows),
    }


def equity() -> list[dict]:
    journal.init()
    return journal.equity_curve()


def positions() -> list[dict]:
    journal.init()
    return journal.open_spreads()


def decisions(limit: int = 100) -> list[dict]:
    """Every decision with its full reasoning, including the vetoed ones."""
    journal.init()
    return [_decode(d, "candidate_json", "risk_reasons", "risk_vetoes")
            for d in journal.recent_decisions(limit)]


def orders(limit: int = 100) -> list[dict]:
    journal.init()
    return journal.all_orders(limit)


def runs(limit: int = 50) -> list[dict]:
    journal.init()
    return [_decode(r, "context_json") for r in journal.recent_runs(limit)]


# --------------------------------------------------------------------------- #
# health - for unattended operation
# --------------------------------------------------------------------------- #

HEALTH_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "journal", "health.json")


def health() -> dict:
    """Is the agent alive, and what did it last do?

    Read-only, like everything else in this package. There is deliberately no
    route anywhere in the dashboard that can place, cancel, or modify an
    order - a health check that can trade is not a health check.

    Combines three independent sources so a lie in one is visible against the
    others: the heartbeat file the loop writes, the cycle lock, and the
    journal's own most recent run.
    """
    import json as _json
    from datetime import datetime, timezone

    from agent import runlock

    beat: dict = {}
    try:
        with open(HEALTH_FILE, encoding="utf-8") as fh:
            beat = _json.load(fh)
    except Exception:
        beat = {}

    journal.init()
    runs = journal.recent_runs(1)
    last_run = runs[0] if runs else None

    age = None
    if beat.get("updated_at"):
        try:
            seen = datetime.fromisoformat(str(beat["updated_at"]))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - seen).total_seconds()
        except Exception:
            age = None

    interval = beat.get("poll_interval_minutes")
    # Stale means "we should have heard from it by now": two intervals plus a
    # cycle's worth of slack, rather than an arbitrary constant.
    stale_after = (interval * 60 * 2 + 300) if interval else 5400
    return {
        "heartbeat": beat,
        "heartbeat_age_seconds": age,
        "stale": (age is None) or (age > stale_after),
        "cycle_in_flight": runlock.is_held(),
        "last_run": last_run,
        "open_positions": len(journal.open_spreads()),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
