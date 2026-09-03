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


def _reason_from_notes(order: dict) -> str | None:
    """Recover an exit reason from the cycle note that recorded it."""
    sym = order.get("short_symbol")
    closed = order.get("closed_ts")
    if not sym or not closed:
        return None
    for run in journal.recent_runs(200):
        note = run.get("note") or ""
        if sym in note:
            for part in note.split(";"):
                if part.strip().startswith(sym):
                    text = part.split(":", 1)[-1].strip()
                    return text or None
    return None


def closed_positions(limit: int = 50) -> list[dict]:
    """Trades that are over, with what they returned and why they ended.

    The page had nowhere to show a finished trade. Win rate said "needs closed
    trades" while one sat in the journal, and the only evidence an exit rule
    had ever fired was a line buried in a cycle note.
    """
    journal.init()
    # A row that never filled is not a closed trade, whatever timestamp it
    # carries. Belt to close_order's braces: if a dead row is ever marked
    # closed again, it still cannot reach the panel or the realised total.
    rows = [o for o in journal.all_orders(500)
            if o.get("closed_ts")
            and str(o.get("status") or "") not in journal.DEAD_STATUSES]
    out = []
    for o in rows:
        credit = float(o.get("credit") or 0) * 100 * int(o.get("contracts") or 0)
        pnl = o.get("realised_pnl")
        pnl = float(pnl) if pnl is not None else None
        risk = float(o.get("max_loss_total") or 0)
        out.append({
            "underlying": o.get("underlying"),
            "kind": o.get("kind"),
            "short_symbol": o.get("short_symbol"),
            "long_symbol": o.get("long_symbol"),
            "contracts": o.get("contracts"),
            "opened_ts": o.get("ts"),
            "closed_ts": o.get("closed_ts"),
            "credit_total": credit,
            "max_loss_total": risk,
            "realised_pnl": pnl,
            # Of the premium collected, how much was actually kept.
            "kept_pct": (100.0 * pnl / credit) if (pnl is not None and credit) else None,
            # And against what was risked to earn it.
            "return_on_risk": (100.0 * pnl / risk) if (pnl is not None and risk) else None,
            # Rows closed before exit_reason existed still recorded WHY in
            # the cycle note. Reading it back is recovering the journal's own
            # words, not inventing a reason after the fact.
            "exit_reason": o.get("exit_reason") or _reason_from_notes(o),
        })
    out.sort(key=lambda r: r["closed_ts"] or "", reverse=True)
    return out[:limit]


def selectivity() -> dict:
    """How much of what it saw did it actually take.

    The funnel panel reports one cycle. That answers "what happened just now"
    and not "how selective is this thing", which is the claim the whole design
    rests on - and the journal has had the answer all along. Screening 265
    candidates to send 3 orders is the argument; a single cycle showing 8 and 1
    is an anecdote.

    Everything here is counted, never estimated.
    """
    journal.init()
    import sqlite3
    con = sqlite3.connect(journal.DB_PATH)

    cycles, screened = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(candidates), 0) FROM runs").fetchone()
    decisions = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    vetoed = con.execute(
        "SELECT COUNT(*) FROM decisions WHERE outcome = 'vetoed'").fetchone()[0]
    passed = con.execute(
        "SELECT COUNT(*) FROM decisions WHERE outcome IN "
        "('no trade', 'no candidates')").fetchone()[0]
    # An order only counts as taken if it was actually sent to the broker -
    # a dry run, an analysis pass and one that never arrived are all not trades.
    placeholders = ",".join("?" for _ in journal.DEAD_STATUSES)
    sent = con.execute(
        "SELECT COUNT(*) FROM orders WHERE LOWER(COALESCE(status,'')) "
        "NOT IN (%s)" % placeholders, journal.DEAD_STATUSES).fetchone()[0]

    return {
        "cycles": cycles,
        "screened": screened,
        "decisions": decisions,
        "vetoed": vetoed,
        "passed": passed,
        "sent": sent,
        "taken_pct": (100.0 * sent / screened) if screened else None,
    }


def broker_positions() -> list[dict]:
    """What Alpaca actually holds, as of the last cycle that could reach it.

    The dashboard used to show only our own order rows, so any drift between
    the journal and the account was invisible on the page - which is precisely
    what happened when a filled spread was wrongly marked closed. These are the
    broker's numbers.
    """
    return journal.broker_positions()


def positions() -> list[dict]:
    journal.init()
    return journal.open_spreads()


# An order that never reached Alpaca is not a decision worth showing. These
# rows exist because the same spread was re-attempted while the journal wrongly
# showed nothing open, and each attempt was refused by the duplicate guard. They
# are artefacts of a bug, not judgements the agent made, and listing five
# identical rows buries the vetoes that are the point of this page.
#
# Vetoes are NOT filtered. A refused trade is a real decision and stays.
_NOISE_ORDER_STATUS = ("not_filled",)


def decisions(limit: int = 100) -> list[dict]:
    """Every decision with its full reasoning, including the vetoed ones.

    Decisions whose order never reached the broker are dropped - see
    _NOISE_ORDER_STATUS. Vetoes stay: a refused trade is the point.
    """
    journal.init()
    dead = {o["decision_id"] for o in journal.all_orders(500)
            if (o.get("status") or "").lower() in _NOISE_ORDER_STATUS
            and o.get("decision_id") is not None}

    # One cycle reached one judgement, so one run gets one row. The duplicate
    # guard used to record a second decision instead of correcting the first,
    # which put every affected cycle on the page twice - once approved, once
    # "duplicate skipped". That is fixed at the source, but rows written while
    # it was broken are still in the journal, and a display that quietly
    # doubles a cycle is worth defending against whatever the cause. The
    # LAST row for a run is the one that survives: it is how the cycle
    # actually resolved.
    seen_runs: set = set()
    out: list[dict] = []
    for d in journal.recent_decisions(limit):        # newest first
        if d["id"] in dead:
            continue
        run = d.get("run_id")
        if run is not None:
            if run in seen_runs:
                continue
            seen_runs.add(run)
        out.append(_decode(d, "candidate_json", "risk_reasons", "risk_vetoes"))
    return out


def orders(limit: int = 100) -> list[dict]:
    journal.init()
    return journal.all_orders(limit)


def runs(limit: int = 50) -> list[dict]:
    journal.init()
    return [_decode(r, "context_json", "shortlist_json", "eliminated_json")
            for r in journal.recent_runs(limit)]


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
