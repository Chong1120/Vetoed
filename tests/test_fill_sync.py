"""The journal must say what Alpaca says - prices, P&L, and one row per trade.

On 16 Sep an IWM 291/293 call spread opened at 18:31 and was closed by the
agent's own take-profit at 19:22 for +$600. The dashboard showed it as two
closed trades, both "realised: not recorded". Three faults stacked:

  1. A backup session started from a journal 70 minutes stale, never saw the
     order, and adopted the broker's legs as a second row.
  2. The take-profit closed the adopted row, and close_order matched on the
     broker order id - which an adopted row does not have - so nothing was
     written. Reconcile then marked both rows "no longer held", P&L unknown.
  3. Separately, every trade stored the screener's QUOTED credit, not the
     fill: $0.495 quoted, $0.46 filled. "Collected", "kept" and "on risk" were
     all computed from a premium that was never received, and so were the
     take-profit and stop thresholds.

The broker orders below are the real ones for that spread, as the MCP server
returned them. Every test here fails if its fix is removed.
"""
import importlib.util
import os
import sqlite3
import tempfile

import pytest

from agent import journal, loop, reconcile
from agent.executor import OrderResult

SHORT, LONG = "IWM260925C00291000", "IWM260925C00293000"
OPEN_ID = "a31127a4-5331-47a9-bde0-0c344134d8ee"
CLOSE_ID = "46657c45-close"

REAL_OPEN = {
    "id": OPEN_ID, "client_order_id": "vetoed-20260916-2349cf0fd4a8",
    "order_class": "mleg", "status": "filled", "qty": "25", "filled_qty": "25",
    "filled_avg_price": "-0.46", "limit_price": "0.47",
    "submitted_at": "2026-09-16T18:31:11.202196Z",
    "filled_at": "2026-09-16T18:31:11.916923Z",
    "legs": [{"symbol": SHORT, "side": "sell", "position_intent": "sell_to_open",
              "filled_avg_price": "1.19"},
             {"symbol": LONG, "side": "buy", "position_intent": "buy_to_open",
              "filled_avg_price": "0.73"}]}

REAL_CLOSE = {
    "id": CLOSE_ID, "client_order_id": "close-1789586576682",
    "order_class": "mleg", "status": "filled", "qty": "25", "filled_qty": "25",
    "filled_avg_price": "0.22", "limit_price": None,
    "submitted_at": "2026-09-16T19:22:56.717151Z",
    "filled_at": "2026-09-16T19:22:56.733253Z",
    "legs": [{"symbol": SHORT, "side": "buy", "position_intent": "buy_to_close",
              "filled_avg_price": "0.45"},
             {"symbol": LONG, "side": "sell", "position_intent": "sell_to_close",
              "filled_avg_price": "0.23"}]}

RUN_NOTE = ("2026-09-16T19:23:03+00:00",
            "IWM260925C00291000: take profit (+$600 of $1150 max); REGIME: "
            "implied/realised 2.00 >= 1.10 - premium is rich, allowing DTE 3-14")

_SPEC = importlib.util.spec_from_file_location(
    "backfill_fills",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "scripts", "backfill_fills.py"))
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(path)
    return path


def _insert(path, **f):
    cols = ",".join(f)
    with journal.connect(path) as c:
        cur = c.execute("INSERT INTO orders (%s) VALUES (%s)"
                        % (cols, ",".join("?" * len(f))), tuple(f.values()))
        return cur.lastrowid


def _row(path, rid):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM orders WHERE id=?", (rid,)).fetchone())
    finally:
        con.close()


def _the_16_sep_rows(path):
    """Rows #75 and #76 exactly as the journal held them."""
    placed = _insert(path, ts="2026-09-16T18:31:11+00:00", alpaca_order_id=OPEN_ID,
                     client_order_id="vetoed-20260916-2349cf0fd4a8",
                     underlying="IWM", kind="call_credit", short_symbol=SHORT,
                     long_symbol=LONG, contracts=25, limit_price=0.47, credit=0.495,
                     max_loss_total=3762.5, status="filled", filled_qty=0.0,
                     closed_ts="2026-09-16T19:33:16+00:00",
                     exit_reason="no longer held at the broker")
    adopted = _insert(path, ts="2026-09-16T18:52:10+00:00", underlying="IWM",
                      kind="call_credit", short_symbol=SHORT, long_symbol=LONG,
                      contracts=25, limit_price=0.46, credit=0.46,
                      max_loss_total=3850.0, status="filled", filled_qty=25.0,
                      fill_price=0.46, closed_ts="2026-09-16T19:33:16+00:00",
                      exit_reason="no longer held at the broker")
    return placed, adopted


# --------------------------------------------------------------------------- #
# journal: the broker's prices, and the P&L they imply
# --------------------------------------------------------------------------- #

def test_open_fill_replaces_the_quote_and_max_loss_follows(db):
    rid = _insert(db, ts="t", short_symbol=SHORT, long_symbol=LONG, contracts=25,
                  credit=0.495, max_loss_total=3762.5, status="filled")
    journal.record_open_fill(rid, 0.46, 25, path=db)
    r = _row(db, rid)
    assert r["credit"] == 0.46 and r["fill_price"] == 0.46 and r["filled_qty"] == 25
    # $2 wide, $0.46 received, 25 contracts: (2 - 0.46) * 100 * 25
    assert r["max_loss_total"] == 3850.0


def test_close_fill_gives_exactly_what_the_account_made(db):
    rid = _insert(db, ts="t", short_symbol=SHORT, long_symbol=LONG, contracts=25,
                  credit=0.46, fill_price=0.46, filled_qty=25, status="filled",
                  closed_ts="2026-09-16T19:33:16+00:00", realised_pnl=None)
    journal.record_close_fill(rid, 0.22, "2026-09-16T19:22:56.733253Z", path=db)
    r = _row(db, rid)
    assert r["realised_pnl"] == 600.0
    assert r["close_fill_price"] == 0.22
    assert r["closed_ts"] == "2026-09-16T19:22:56+00:00"


def test_close_fill_refuses_to_subtract_a_real_debit_from_a_quote(db):
    rid = _insert(db, ts="t", short_symbol=SHORT, long_symbol=LONG, contracts=25,
                  credit=0.495, status="filled", closed_ts="x", realised_pnl=None)
    journal.record_close_fill(rid, 0.22, "2026-09-16T19:22:56Z", path=db)
    assert _row(db, rid)["realised_pnl"] is None


def test_close_order_keeps_the_closing_order_id_on_both_match_paths(db):
    by_broker = _insert(db, ts="a", alpaca_order_id="B1", status="filled",
                        short_symbol=SHORT, long_symbol=LONG)
    by_row = _insert(db, ts="b", status="adopted", short_symbol=SHORT, long_symbol=LONG)
    journal.close_order("B1", 10.0, "take profit", close_order_id="C1", path=db)
    journal.close_order("", 20.0, "take profit", row_id=by_row,
                        close_order_id="C2", path=db)
    assert _row(db, by_broker)["close_order_id"] == "C1"
    assert _row(db, by_row)["close_order_id"] == "C2"


def test_a_duplicate_is_not_open_risk_and_needs_no_sync(db):
    assert "duplicate" in journal.DEAD_STATUSES
    _insert(db, ts="t", alpaca_order_id="X", status="duplicate",
            short_symbol=SHORT, long_symbol=LONG, close_order_id="Y")
    assert journal.open_spreads(path=db) == []
    todo = journal.rows_needing_fill_sync(path=db)
    assert todo == {"opens": [], "closes": []}


# --------------------------------------------------------------------------- #
# the cycle: an adopted row's close is recorded, and one spread closes once
# --------------------------------------------------------------------------- #

class FakeMCP:
    def __init__(self, orders=None, fail=False):
        self.closes, self.asked = [], []
        self.orders, self.fail = orders or {}, fail

    async def close_credit_spread(self, s, l, n, limit_price=None, client_order_id=None):
        self.closes.append((s, l, n))
        return OrderResult(True, {"id": "close-order-%d" % len(self.closes)})

    async def order_by_id(self, oid):
        self.asked.append(oid)
        if self.fail:
            raise RuntimeError("broker hiccup")
        return self.orders.get(oid, {"id": oid, "status": "new"})


class _Market:
    def option_delta(self, sym):
        return None


LEGS_UP_600 = {SHORT: {"unrealized_pl": "825"}, LONG: {"unrealized_pl": "-225"}}


def _patch_journal(monkeypatch, rows):
    calls = []
    monkeypatch.setattr(loop.journal, "open_spreads", lambda **k: rows)
    monkeypatch.setattr(loop.journal, "close_order",
                        lambda *a, **k: calls.append((a, k)))
    return calls


def test_take_profit_on_an_adopted_row_is_recorded(monkeypatch):
    """The 19:22 exit exactly: adopted row, no broker id, +$600 on $1150."""
    adopted = {"id": 76, "alpaca_order_id": None, "short_symbol": SHORT,
               "long_symbol": LONG, "contracts": 25, "credit": 0.46}
    calls = _patch_journal(monkeypatch, [adopted])
    mcp = FakeMCP()
    import asyncio
    asyncio.run(loop.manage_positions(mcp, _Market(), dry_run=False, legs=LEGS_UP_600))
    assert len(mcp.closes) == 1
    assert len(calls) == 1, "the close must be written"
    (args, kwargs) = calls[0]
    assert kwargs.get("row_id") == 76, "an adopted row can only be found by row id"
    assert kwargs.get("close_order_id") == "close-order-1"


def test_two_rows_for_one_spread_send_one_close(monkeypatch):
    """Both rows read the same broker P&L. Only one close may be sent."""
    rows = [{"id": 76, "alpaca_order_id": None, "short_symbol": SHORT,
             "long_symbol": LONG, "contracts": 25, "credit": 0.46},
            {"id": 75, "alpaca_order_id": OPEN_ID, "short_symbol": SHORT,
             "long_symbol": LONG, "contracts": 25, "credit": 0.46}]
    _patch_journal(monkeypatch, rows)
    mcp = FakeMCP()
    import asyncio
    asyncio.run(loop.manage_positions(mcp, _Market(), dry_run=False, legs=LEGS_UP_600))
    assert mcp.closes == [(SHORT, LONG, 25)], "a second close would buy back a short that is gone"


# --------------------------------------------------------------------------- #
# reconcile.sync_fills: the per-cycle correction
# --------------------------------------------------------------------------- #

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_sync_writes_the_open_then_the_close_in_one_pass(db):
    rid = _insert(db, ts="2026-09-16T18:31:11+00:00", alpaca_order_id=OPEN_ID,
                  short_symbol=SHORT, long_symbol=LONG, contracts=25, credit=0.495,
                  max_loss_total=3762.5, status="filled",
                  closed_ts="2026-09-16T19:23:00+00:00", realised_pnl=599.0,
                  close_order_id=CLOSE_ID)
    mcp = FakeMCP({OPEN_ID: REAL_OPEN, CLOSE_ID: REAL_CLOSE})
    notes = _run(reconcile.sync_fills(mcp, path=db))
    r = _row(db, rid)
    assert r["credit"] == 0.46 and r["max_loss_total"] == 3850.0
    assert r["realised_pnl"] == 600.0, "P&L from fills, not the estimate at trigger"
    assert r["closed_ts"] == "2026-09-16T19:22:56+00:00"
    assert len(notes) == 2
    assert journal.rows_needing_fill_sync(path=db) == {"opens": [], "closes": []}


def test_an_unfilled_order_is_asked_about_again_next_cycle(db):
    rid = _insert(db, ts="t", alpaca_order_id="PENDING", short_symbol=SHORT,
                  long_symbol=LONG, contracts=25, credit=0.495, status="accepted")
    _run(reconcile.sync_fills(FakeMCP(), path=db))
    assert _row(db, rid)["credit"] == 0.495
    assert [r["id"] for r in journal.rows_needing_fill_sync(path=db)["opens"]] == [rid]


def test_a_broker_error_is_noted_not_raised(db):
    _insert(db, ts="t", alpaca_order_id=OPEN_ID, short_symbol=SHORT,
            long_symbol=LONG, contracts=25, credit=0.495, status="filled")
    notes = _run(reconcile.sync_fills(FakeMCP(fail=True), path=db))
    assert notes and "could not read" in notes[0]


def test_sync_is_bounded_per_cycle(db):
    for i in range(5):
        _insert(db, ts="t%d" % i, alpaca_order_id="O%d" % i, short_symbol=SHORT,
                long_symbol=LONG, contracts=1, credit=0.5, status="filled")
    mcp = FakeMCP()
    _run(reconcile.sync_fills(mcp, path=db, limit=2))
    assert len(mcp.asked) == 2


def test_a_debit_fill_is_never_written_as_a_credit(db):
    rid = _insert(db, ts="t", alpaca_order_id="D", short_symbol=SHORT,
                  long_symbol=LONG, contracts=1, credit=0.5, status="filled")
    debit = dict(REAL_OPEN, id="D", filled_avg_price="0.30")
    notes = _run(reconcile.sync_fills(FakeMCP({"D": debit}), path=db))
    assert _row(db, rid)["credit"] == 0.5
    assert "not a credit" in notes[0]


# --------------------------------------------------------------------------- #
# the backfill: 16 Sep, reproduced and repaired
# --------------------------------------------------------------------------- #

def test_backfill_repairs_the_16_sep_iwm_rows(db):
    placed, adopted = _the_16_sep_rows(db)
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM orders ORDER BY id")]
    con.close()

    changes = backfill.plan(rows, [REAL_OPEN, REAL_CLOSE], [RUN_NOTE])
    by = {(c["row"], c["field"]): c for c in changes}
    assert by[(adopted, "status")]["new"] == "duplicate"
    assert by[(placed, "open_fill")]["new"] == 0.46
    assert by[(placed, "close_fill")]["new"] == 600.0
    assert by[(placed, "exit_reason")]["new"] == "take profit (+$600 of $1150 max)"
    assert not any(c["row"] == adopted and c["field"] != "status" for c in changes), \
        "the duplicate must not also be given the trade's P&L"

    backfill.apply(changes, db)
    p = _row(db, placed)
    assert (p["credit"], p["realised_pnl"], p["max_loss_total"]) == (0.46, 600.0, 3850.0)
    assert p["closed_ts"] == "2026-09-16T19:22:56+00:00"
    assert p["exit_reason"] == "take profit (+$600 of $1150 max)"
    assert _row(db, adopted)["status"] == "duplicate"


def test_backfill_is_idempotent(db):
    _the_16_sep_rows(db)
    load = lambda: [dict(r) for r in _conn(db).execute("SELECT * FROM orders ORDER BY id")]
    backfill.apply(backfill.plan(load(), [REAL_OPEN, REAL_CLOSE], [RUN_NOTE]), db)
    assert backfill.plan(load(), [REAL_OPEN, REAL_CLOSE], [RUN_NOTE]) == []


def test_backfill_leaves_a_close_it_cannot_find_alone(db):
    placed, _ = _the_16_sep_rows(db)
    rows = [dict(r) for r in _conn(db).execute("SELECT * FROM orders ORDER BY id")]
    changes = backfill.plan(rows, [REAL_OPEN], [RUN_NOTE])
    assert not any(c["field"] in ("close_fill", "exit_reason") for c in changes), \
        "no closing order at the broker means the P&L stays unknown, not guessed"


def _conn(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------- #
# the repair must survive the merge from a session still running old code
# --------------------------------------------------------------------------- #

def test_corrected_values_survive_a_merge_from_a_stale_session(tmp_path):
    """The session running when this ships holds the OLD rows. Its next push
    merges them into main - and must not put the quote or the phantom back."""
    spec = importlib.util.spec_from_file_location(
        "merge_journal",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "merge_journal.py"))
    merge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge)

    main, stale = str(tmp_path / "main.db"), str(tmp_path / "stale.db")
    for p in (main, stale):
        journal.init(p)
        _the_16_sep_rows(p)
    rows = [dict(r) for r in _conn(main).execute("SELECT * FROM orders ORDER BY id")]
    backfill.apply(backfill.plan(rows, [REAL_OPEN, REAL_CLOSE], [RUN_NOTE]), main)

    merge.merge(stale, main, verbose=False)

    got = [dict(r) for r in _conn(main).execute("SELECT * FROM orders ORDER BY id")]
    assert len(got) == 2, "the merge must not add a row"
    placed = next(r for r in got if r["client_order_id"])
    adopted = next(r for r in got if not r["client_order_id"])
    assert (placed["credit"], placed["realised_pnl"]) == (0.46, 600.0)
    assert adopted["status"] == "duplicate"
