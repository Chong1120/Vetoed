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
    realised_pnl    REAL,
    exit_reason     TEXT
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

CREATE TABLE IF NOT EXISTS broker_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    qty         REAL,
    avg_price   REAL,
    market_value REAL,
    unrealised  REAL,
    current_price REAL
);

CREATE INDEX IF NOT EXISTS idx_broker_positions_ts ON broker_positions(ts);
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
    "ALTER TABLE orders ADD COLUMN exit_reason TEXT",
    "ALTER TABLE runs ADD COLUMN shortlist_json TEXT",
    "ALTER TABLE runs ADD COLUMN eliminated_json TEXT",
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

# Only what is needed to show the ranking. The full candidate carries 26
# fields; storing all of them for every cycle would bloat a journal that is
# committed to git on every run.
_SHORTLIST_FIELDS = ("underlying", "kind", "short_strike", "long_strike",
                     "dte", "vrp_edge", "pop", "credit", "score")


def _trim(cands: list[dict] | None) -> list[dict]:
    return [{k: c.get(k) for k in _SHORTLIST_FIELDS} for c in (cands or [])]


def start_run(market_open: bool, feed: str | None, equity: float | None,
              day_pnl: float | None, halted: bool, candidates: int,
              note: str = "", context: dict | None = None,
              shortlist: list[dict] | None = None,
              eliminated: dict | None = None,
              path: str = DB_PATH) -> int:
    """Open a run row.

    `context` is the screener's own output - per-underlying spot, IV, realised
    vol and contracts examined. Stored so the dashboard can show WHY a cycle
    produced the candidates it did, including the cycles that produced none.

    `shortlist` is every candidate the model was offered, not just the one it
    took. Without it the journal records a choice with nothing to compare it
    against, and "picks one item from a list it did not write" is a claim the
    page cannot show.

    `eliminated` is everything that never became a candidate at all - counted
    by the reason the screener threw it out. The log could previously show
    only one reason a trade did not happen, the position limit, because that
    is the sole veto raised after nomination. A reader saw an agent forever
    straining against a cap and never one turning work down on quality, which
    is what it spends almost all of its time doing.
    """
    with connect(path) as c:
        cur = c.execute(
            "INSERT INTO runs (ts, market_open, feed, equity, day_pnl, halted,"
            " candidates, note, context_json, shortlist_json, eliminated_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now(), int(market_open), feed, equity, day_pnl, int(halted),
             candidates, note,
             json.dumps(context.get("underlyings", {})) if context else None,
             json.dumps(_trim(shortlist)) if shortlist else None,
             json.dumps(eliminated) if eliminated else None))
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


def update_order_status(alpaca_order_id: str | None, status: str,
                        filled_qty: float | None = None,
                        fill_price: float | None = None,
                        row_id: int | None = None,
                        path: str = DB_PATH) -> None:
    """Set a status, matched by broker id where there is one, row id otherwise.

    Matching only on alpaca_order_id silently skipped the rows that needed it
    most. An order journalled "uncertain" has no broker id BY DEFINITION -
    uncertain means the submission never came back with one - so reconcile
    would decide the order never arrived, log "marking not-filled", and update
    nothing. The row sat at uncertain for ever and the dashboard showed it as
    an UNCONFIRMED position that the broker had never heard of.

    The row id is the journal's own primary key, so it always exists.
    """
    with connect(path) as c:
        if alpaca_order_id:
            c.execute(
                "UPDATE orders SET status=?, filled_qty=COALESCE(?, filled_qty),"
                " fill_price=COALESCE(?, fill_price) WHERE alpaca_order_id=?",
                (status, filled_qty, fill_price, alpaca_order_id))
        elif row_id is not None:
            c.execute(
                "UPDATE orders SET status=?, filled_qty=COALESCE(?, filled_qty),"
                " fill_price=COALESCE(?, fill_price) WHERE id=?",
                (status, filled_qty, fill_price, int(row_id)))


def set_decision_outcome(decision_id: int, outcome: str,
                         path: str = DB_PATH) -> None:
    """Correct a decision's outcome in place.

    A cycle reaches ONE judgement. When something downstream changes what
    became of it, that is the same decision resolving differently - not a
    second decision. Inserting a new row for it double-counts the cycle and
    makes the page show every judgement twice.
    """
    with connect(path) as c:
        c.execute("UPDATE decisions SET outcome=? WHERE id=?",
                  (outcome, decision_id))


def adopt_order(underlying: str, kind: str, short_symbol: str,
                long_symbol: str, contracts: int, credit: float,
                max_loss_total: float, path: str = DB_PATH) -> int | None:
    """Record a spread the broker holds that this journal has no row for.

    Written when a cycle's journal commit was lost: the trade happened, the
    record did not survive. Every value comes from the broker's own position.
    The row carries no decision, because the decision is gone and will not be
    invented; status is 'adopted' so it can never read as one journalled live.

    Idempotent on the leg pair - a second cycle finding the same orphan must
    not write it twice.
    """
    with connect(path) as c:
        dup = c.execute(
            "SELECT id FROM orders WHERE short_symbol=? AND long_symbol=? "
            "AND closed_ts IS NULL", (short_symbol, long_symbol)).fetchone()
        if dup:
            return None
        cur = c.execute(
            "INSERT INTO orders (decision_id, ts, alpaca_order_id,"
            " client_order_id, underlying, kind, short_symbol, long_symbol,"
            " contracts, limit_price, credit, max_loss_total, status,"
            " filled_qty, fill_price, raw_json)"
            " VALUES (NULL,?,NULL,NULL,?,?,?,?,?,?,?,?,'adopted',?,?,?)",
            (now(), underlying, kind, short_symbol, long_symbol, contracts,
             credit, credit, max_loss_total, contracts, credit,
             json.dumps({"adopted": "held at the broker with no journal row; "
                                    "fields are the broker's own position"})))
        return int(cur.lastrowid)


def close_order(alpaca_order_id: str | None, realised_pnl: float,
                reason: str = "", row_id: int | None = None,
                path: str = DB_PATH) -> None:
    """Record a close, and WHY.

    The reason was computed at the exit and then dropped, so the journal knew
    a position had closed for $484 and not whether that was a target hit or a
    stop taken - which is the more interesting half. Existing rows keep a NULL
    here; nothing invents a reason after the fact.

    Matched by broker id where there is one, row id otherwise - the same fault
    as update_order_status had, and for the same reason. An ADOPTED row is
    rebuilt from the broker's position data, not from an order we sent, so it
    never has an alpaca_order_id. A QQQ spread the broker had stopped holding
    sat open in the journal for two days while every cycle logged "no longer
    held at the broker - marking closed" and closed nothing.
    """
    with connect(path) as c:
        if alpaca_order_id:
            c.execute("UPDATE orders SET closed_ts=?, realised_pnl=?, exit_reason=? "
                      "WHERE alpaca_order_id=?",
                      (now(), realised_pnl, reason or None, alpaca_order_id))
        elif row_id is not None:
            c.execute("UPDATE orders SET closed_ts=?, realised_pnl=?, exit_reason=? "
                      "WHERE id=?",
                      (now(), realised_pnl, reason or None, int(row_id)))


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


# Newest by WHEN IT HAPPENED, not by when the row was written.
#
# Insertion order and chronology are the same thing right up until a row is
# recovered - a cycle whose journal commit was lost, reimported from the
# broker afterwards. That row carries the newest id and an older timestamp,
# and ordering by id made a 16:41 cycle outrank a 17:17 one: the funnel and
# the volatility cards read runs[0] as "the latest cycle" and would have shown
# a recovered row with no candidates in it. The id tiebreak keeps ordering
# stable when two rows share a timestamp.
def recent_runs(limit: int = 50, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM runs ORDER BY ts DESC, id DESC LIMIT ?",
                 (limit,), path)


def recent_decisions(limit: int = 200, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM decisions ORDER BY ts DESC, id DESC LIMIT ?",
                 (limit,), path)


def record_broker_positions(legs: dict, path: str = DB_PATH) -> None:
    """Snapshot what the broker holds, so the dashboard can show the account.

    The journal knows what we intended and what we sent. It did not know what
    Alpaca actually holds, so the dashboard could only ever show our own view
    of the world - which is exactly what drifted when a filled spread was
    wrongly marked closed. Storing the broker's own numbers each cycle means
    the published page reports the account, not our belief about it.

    Replaces the previous snapshot: this is current state, not history.
    """
    ts = now()
    with connect(path) as c:
        c.execute("DELETE FROM broker_positions")
        for sym, p in (legs or {}).items():
            def num(key):
                try:
                    return float(p.get(key))
                except (TypeError, ValueError):
                    return None
            c.execute(
                "INSERT INTO broker_positions (ts, symbol, qty, avg_price, "
                "market_value, unrealised, current_price) VALUES (?,?,?,?,?,?,?)",
                (ts, str(sym), num("qty"), num("avg_entry_price"),
                 num("market_value"), num("unrealized_pl"), num("current_price")))


def broker_positions(path: str = DB_PATH) -> list[dict]:
    """What Alpaca held as of the last cycle that could reach it."""
    return _rows("SELECT * FROM broker_positions ORDER BY symbol", (), path)


def all_orders(limit: int = 200, path: str = DB_PATH) -> list[dict]:
    return _rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,), path)


# Statuses that mean the order is not, and never will be, live risk.
#
#   dry_run    simulated; never left this machine
#   failed     the broker saw it and rejected it
#   not_filled reconcile.py checked with the broker and found nothing
#   analysis_only an analysis pass reached a judgement and withheld the entry;
#              no order was ever built, so there is nothing to hold risk
#
# NOTE what is deliberately ABSENT: 'uncertain'. When a submission times out
# we do not know whether Alpaca received it, and the asymmetry is not close -
# counting a position that does not exist costs one skipped trade, while
# missing one that does exist can double a position. Uncertain orders are
# therefore treated as live risk until reconcile.py resolves them against the
# broker, at which point they become 'not_filled' or a confirmed position.
DEAD_STATUSES = ("analysis_only", "canceled", "cancelled", "rejected", "expired",
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
