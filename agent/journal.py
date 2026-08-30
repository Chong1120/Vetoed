"""
journal.py - the structured decision journal (SQLite).

Every run, every LLM decision with its full rationale, every risk veto, and
every order is written here. This is the audit trail: it is what the dashboard
renders and what the demo video shows. Nothing the agent does is invisible.

Deliberately append-only in spirit - rows are inserted, and only order/position
status is ever updated. We never delete a decision, including the bad ones.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "journal", "trades.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    market_open   INTEGER NOT NULL,
    feed          TEXT,
    equity        REAL,
    day_pnl       REAL,
    halted        INTEGER DEFAULT 0,
    candidates    INTEGER DEFAULT 0,
    note          TEXT,
    context_json  TEXT          -- per-underlying spot/IV/RV for the dashboard
);

CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    ts             TEXT NOT NULL,
    underlying     TEXT,
    kind           TEXT,
    candidate_json TEXT,
    llm_action     TEXT,
    llm_confidence REAL,
    llm_rationale  TEXT,
    llm_raw        TEXT,
    llm_error      TEXT,
    risk_approved  INTEGER,
    risk_contracts INTEGER,
    risk_reasons   TEXT,
    risk_vetoes    TEXT,
    outcome        TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     INTEGER REFERENCES decisions(id),
    ts              TEXT NOT NULL,
    alpaca_order_id TEXT,
    client_order_id TEXT,
    underlying      TEXT,
    kind            TEXT,
    short_symbol    TEXT,
    long_symbol     TEXT,
    contracts       INTEGER,
    limit_price     REAL,
    credit          REAL,
    max_loss_total  REAL,
    status          TEXT,
    entry_short_delta REAL,
    entry_dte       INTEGER,
    filled_qty      REAL DEFAULT 0,
    fill_price      REAL,
    raw_json        TEXT,
    closed_ts       TEXT,
    realised_pnl    REAL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    equity       REAL,
    last_equity  REAL,
    cash         REAL,
    buying_power REAL,
    open_positions INTEGER
);

CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_decision ON orders(decision_id);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: str = DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first schema shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so attempt and ignore the duplicate error.
MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN entry_short_delta REAL",
    "ALTER TABLE orders ADD COLUMN entry_dte INTEGER",
    "ALTER TABLE runs ADD COLUMN context_json TEXT",
]


def init(path: str = DB_PATH) -> None:
    with connect(path) as c:
        c.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already present


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #

def start_run(market_open: bool, feed: str | None, equity: float | None,
              day_pnl: float | None, halted: bool, candidates: int,
              note: str = "", context: dict | None = None,
              path: str = DB_PATH) -> int:
    """Open a run row.

    `context` is the screener's own output - per-underlying spot, IV, realised
    vol and contracts examined. Stored so the dashboard can show WHY a cycle
    produced the candidates it did, including the cycles that produced none.
    """
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO runs (ts, market_open, feed, equity, day_pnl, halted,"
            " candidates, note, context_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (now(), int(market_open), feed, equity, day_pnl, int(halted),
             candidates, note,
             json.dumps(context.get("underlyings", {})) if context else None))
        return int(cur.lastrowid)


def record_decision(run_id: int, candidate: dict | None, llm: dict | None,
                    risk: dict | None, llm_raw: str = "", llm_error: str = "",
                    outcome: str = "", path: str = DB_PATH) -> int:
    candidate = candidate or {}
    llm = llm or {}
    risk = risk or {}
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO decisions (run_id, ts, underlying, kind,"
            " candidate_json, llm_action, llm_confidence, llm_rationale,"
            " llm_raw, llm_error, risk_approved, risk_contracts, risk_reasons,"
            " risk_vetoes, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, now(), candidate.get("underlying"), candidate.get("kind"),
             json.dumps(candidate), llm.get("action"), llm.get("confidence"),
             llm.get("rationale"), llm_raw, llm_error,
             int(bool(risk.get("approved"))), risk.get("contracts", 0),
             json.dumps(risk.get("reasons", [])),
             json.dumps(risk.get("vetoes", [])), outcome))
        return int(cur.lastrowid)


def record_order(decision_id: int, candidate: dict, contracts: int,
                 limit_price: float, max_loss_total: float, result: dict,
                 path: str = DB_PATH) -> int:
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO orders (decision_id, ts, alpaca_order_id,"
            " client_order_id, underlying, kind, short_symbol, long_symbol,"
            " contracts, limit_price, credit, max_loss_total, status,"
            " entry_short_delta, entry_dte, filled_qty, fill_price, raw_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, now(), result.get("id"),
             result.get("client_order_id"), candidate.get("underlying"),
             candidate.get("kind"), candidate.get("short_symbol"),
             candidate.get("long_symbol"), contracts, limit_price,
             candidate.get("credit"), max_loss_total,
             str(result.get("status")),
             abs(float(candidate.get("short_delta") or 0)) or None,
             candidate.get("dte"),
             float(result.get("filled_qty") or 0),
             result.get("filled_avg_price"), json.dumps(result)[:20000]))
        return int(cur.lastrowid)


def update_order_status(alpaca_order_id: str, status: str,
                        filled_qty: float | None = None,
                        fill_price: float | None = None,
                        path: str = DB_PATH) -> None:
    with connect(path) as c:
        c.execute(
            "UPDATE orders SET status=?, filled_qty=COALESCE(?, filled_qty),"
            " fill_price=COALESCE(?, fill_price) WHERE alpaca_order_id=?",
            (status, filled_qty, fill_price, alpaca_order_id))


def close_order(alpaca_order_id: str, realised_pnl: float,
                path: str = DB_PATH) -> None:
    with connect(path) as c:
        c.execute("UPDATE orders SET closed_ts=?, realised_pnl=? "
                  "WHERE alpaca_order_id=?",
                  (now(), realised_pnl, alpaca_order_id))


def snapshot_equity(equity: float, last_equity: float, cash: float,
                    buying_power: float, open_positions: int,
                    path: str = DB_PATH) -> None:
    with connect(path) as c:
        c.execute(
            "INSERT INTO equity_snapshots (ts, equity, last_equity, cash,"
            " buying_power, open_positions) VALUES (?,?,?,?,?,?)",
            (now(), equity, last_equity, cash, buying_power, open_positions))


# --------------------------------------------------------------------------- #
# reads (used by the dashboard)
# --------------------------------------------------------------------------- #

def _rows(sql: str, args=(), path: str = DB_PATH) -> list[dict]:
    with connect(path) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def recent_runs(limit: int = 50, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,), path)


def recent_decisions(limit: int = 200, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?",
                 (limit,), path)


def all_orders(limit: int = 200, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,), path)


# Statuses that mean the order is not, and never will be, live risk.
#
#   dry_run    simulated; never left this machine
#   failed     the broker saw it and rejected it
#   not_filled reconcile.py checked with the broker and found nothing
#
# NOTE what is deliberately ABSENT: 'uncertain'. When a submission times out
# we do not know whether Alpaca received it, and the asymmetry is not close -
# counting a position that does not exist costs one skipped trade, while
# missing one that does exist can double a position. Uncertain orders are
# therefore treated as live risk until reconcile.py resolves them against the
# broker, at which point they become 'not_filled' or a confirmed position.
DEAD_STATUSES = ("canceled", "cancelled", "rejected", "expired",
                 "dry_run", "failed", "not_filled")


def open_spreads(path: str = DB_PATH) -> list[dict]:
    """Live orders that have not been closed - our real open risk.

    risk.py reads this to enforce the concentration limit and the portfolio
    risk cap, so a simulated or rejected order counted here would invent risk
    that does not exist and veto real trades. See DEAD_STATUSES above for what
    is excluded, and why 'uncertain' is not.
    """
    placeholders = ",".join("?" for _ in DEAD_STATUSES)
    return _rows(
        "SELECT * FROM orders WHERE closed_ts IS NULL"
        " AND LOWER(COALESCE(status,'')) NOT IN (%s)"
        " ORDER BY id DESC" % placeholders, DEAD_STATUSES, path)


def equity_curve(limit: int = 1000, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT ts, equity, open_positions FROM equity_snapshots"
                 " ORDER BY id ASC LIMIT ?", (limit,), path)
