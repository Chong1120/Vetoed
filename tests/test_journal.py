"""Journal tests. open_spreads() feeds the risk gates, so it must not lie."""

import os
import tempfile

import pytest

from agent import journal


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(path)
    yield path


def _order(db, status, oid):
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 5, path=db)
    did = journal.record_decision(run, {"underlying": "SPY"}, None, None, path=db)
    return journal.record_order(
        did, {"underlying": "SPY", "kind": "put_credit",
              "short_symbol": "S", "long_symbol": "L", "credit": 1.0},
        1, 0.95, 380.0, {"id": oid, "status": status}, path=db)


def test_dry_run_orders_are_not_open_risk(db):
    """A simulated order must never count toward real exposure."""
    _order(db, "dry_run", None)
    assert journal.open_spreads(path=db) == []


def test_failed_orders_are_not_open_risk(db):
    _order(db, "failed", None)
    assert journal.open_spreads(path=db) == []


def test_cancelled_and_rejected_excluded(db):
    for s in ("canceled", "cancelled", "rejected", "expired"):
        _order(db, s, "o-" + s)
    assert journal.open_spreads(path=db) == []


def test_live_order_counts_as_open(db):
    _order(db, "accepted", "real-1")
    rows = journal.open_spreads(path=db)
    assert len(rows) == 1
    assert rows[0]["alpaca_order_id"] == "real-1"


def test_closed_order_drops_out(db):
    _order(db, "filled", "real-2")
    assert len(journal.open_spreads(path=db)) == 1
    journal.close_order("real-2", 55.0, path=db)
    assert journal.open_spreads(path=db) == []


def test_mixed_only_returns_live(db):
    _order(db, "dry_run", None)
    _order(db, "filled", "real-3")
    _order(db, "canceled", "x")
    rows = journal.open_spreads(path=db)
    assert len(rows) == 1
    assert rows[0]["alpaca_order_id"] == "real-3"
