"""The exit rules, after the first month's record was replayed against them.

68 spreads, 72.1% of them winners, and a $58 loss - because the stop cut
positions that recovered. Replaying all of them against the marks Alpaca
actually published (scripts/replay_marks.py, which reads them out of the
journal's own git history) gave:

    stop at 0.75x credit   -$627       stop at 1.5x    +$2,312
    stop at 1.0x           -$3,473     stop at 2.0x    +$8,448   <- it was here
    stop at 1.25x          -$5,481     no stop        +$11,582

The 20 trades that ever traded 1x credit or more against the position realised
-$21,745 and would have settled at +$3,037. The delta stop alone took 11
trades for -$17,575 without a single winner.

So these tests pin the shape of the fix: nothing closes on a drawdown that the
record says recovers, a genuine disaster still closes, and the take-profit is
untouched because no data here can measure a better one.
"""
import asyncio
import datetime as dt
import os
import sqlite3
import tempfile

import pytest

from agent import journal, loop, reconcile
from agent.executor import OrderResult

# Far enough out that the expiry rule never fires: these tests are about the
# price rules, and a symbol with a past expiry closes for that reason instead.
_FAR = (dt.date.today() + dt.timedelta(days=10)).strftime("%y%m%d")
SHORT, LONG = "IWM%sC00291000" % _FAR, "IWM%sC00293000" % _FAR
CREDIT = 0.46                 # $1,150 on 25 contracts
CREDIT_TOTAL = CREDIT * 100 * 25


class FakeMCP:
    def __init__(self, orders=None):
        self.closes = []
        self.orders = orders or {}

    async def close_credit_spread(self, s, l, n, limit_price=None, client_order_id=None):
        self.closes.append((s, l, n))
        return OrderResult(True, {"id": "close-%d" % (len(self.closes))})

    async def order_by_id(self, oid):
        return self.orders.get(oid, {"id": oid, "status": "new"})


def _row(short=SHORT, long=LONG, **over):
    r = {"id": 1, "alpaca_order_id": "A1", "short_symbol": short,
         "long_symbol": long, "contracts": 25, "credit": CREDIT,
         "entry_short_delta": 0.25}
    r.update(over)
    return r


def _run(monkeypatch, rows, unreal):
    """Drive manage_positions with the broker showing this unrealised P&L."""
    calls = []
    monkeypatch.setattr(loop.journal, "open_spreads", lambda **k: rows)
    monkeypatch.setattr(loop.journal, "close_order", lambda *a, **k: calls.append(k))
    mcp = FakeMCP()
    legs = {rows[0]["short_symbol"]: {"unrealized_pl": str(unreal)},
            rows[0]["long_symbol"]: {"unrealized_pl": "0"}}
    actions = asyncio.run(loop.manage_positions(mcp, dry_run=False, legs=legs))
    return mcp, actions


# --------------------------------------------------------------------------- #
# the rule that was costing the money
# --------------------------------------------------------------------------- #

def test_the_delta_stop_is_gone():
    """11 trades, -$17,575, not one winner. It closed on a doubled delta,
    which is the underlying arriving at the strike - noise, not a verdict."""
    assert not hasattr(loop, "DELTA_STOP_MULTIPLE")


@pytest.mark.parametrize("multiple", [1.0, 1.5, 2.0, 2.9])
def test_a_drawdown_the_record_says_recovers_is_left_alone(monkeypatch, multiple):
    """At 2x this used to close. Those are the trades that came back."""
    mcp, actions = _run(monkeypatch, [_row()], -CREDIT_TOTAL * multiple)
    assert mcp.closes == [], "closed at %gx credit against" % multiple
    assert actions == []


def test_the_disaster_backstop_still_fires(monkeypatch):
    """Wide enough never to have triggered in the sample, kept for the regime
    the sample does not contain."""
    assert loop.STOP_LOSS_MULTIPLE == 3.0
    mcp, actions = _run(monkeypatch, [_row()], -CREDIT_TOTAL * 3.1)
    assert len(mcp.closes) == 1
    assert "stop loss" in actions[0]


def test_take_profit_is_untouched(monkeypatch):
    """Deliberately not tuned: the agent always closed at 50%, so this record
    contains no evidence about any other level."""
    assert loop.TAKE_PROFIT_FRACTION == 0.50
    mcp, actions = _run(monkeypatch, [_row()], CREDIT_TOTAL * 0.5)
    assert len(mcp.closes) == 1
    assert "take profit" in actions[0]


def test_just_under_the_target_is_left_open(monkeypatch):
    mcp, _ = _run(monkeypatch, [_row()], CREDIT_TOTAL * 0.49)
    assert mcp.closes == []


def test_expiry_still_closes(monkeypatch):
    """The one exit that is not about price. Carrying into expiry day risks
    assignment on a short leg for a position worth pennies."""
    soon = dt.date.today() + dt.timedelta(days=1)
    s = "IWM%sC00291000" % soon.strftime("%y%m%d")
    l = "IWM%sC00293000" % soon.strftime("%y%m%d")
    mcp, actions = _run(monkeypatch, [_row(short=s, long=l)], -CREDIT_TOTAL * 0.2)
    assert len(mcp.closes) == 1
    assert "approaching expiry" in actions[0]


# --------------------------------------------------------------------------- #
# the credit floor, which was never binding
# --------------------------------------------------------------------------- #

@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(path)
    return path


def _insert(path, **f):
    with journal.connect(path) as c:
        return c.execute("INSERT INTO orders (%s) VALUES (%s)"
                         % (",".join(f), ",".join("?" * len(f))),
                         tuple(f.values())).lastrowid


def test_a_fill_under_its_own_limit_is_flagged(db):
    """45 of the first 67 fills did this and nothing said so."""
    _insert(db, ts="t", alpaca_order_id="O1", short_symbol=SHORT, long_symbol=LONG,
            contracts=25, credit=0.47, limit_price=0.47, status="filled")
    order = {"id": "O1", "status": "filled", "filled_qty": "25",
             "filled_avg_price": "-0.46", "filled_at": "2026-09-16T18:31:11Z"}
    notes = asyncio.run(reconcile.sync_fills(FakeMCP({"O1": order}), path=db))
    assert any("credit floor is not binding" in n for n in notes), notes


def test_a_fill_that_honours_its_limit_is_quiet(db):
    _insert(db, ts="t", alpaca_order_id="O2", short_symbol=SHORT, long_symbol=LONG,
            contracts=25, credit=0.47, limit_price=0.47, status="filled")
    order = {"id": "O2", "status": "filled", "filled_qty": "25",
             "filled_avg_price": "-0.48", "filled_at": "2026-09-16T18:31:11Z"}
    notes = asyncio.run(reconcile.sync_fills(FakeMCP({"O2": order}), path=db))
    assert not any("not binding" in n for n in notes), notes
