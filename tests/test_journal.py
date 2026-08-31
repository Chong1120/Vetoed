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


# --------------------------------------------------------------------------
# An analysis pass must not invent risk.
#
# The 5-minute passes reach a real judgement and withhold only the entry, so
# they journal an order row with status analysis_only. If that counted as an
# open spread, every pass would add phantom risk: the concentration limit
# would fill up with positions that do not exist, and real trades would be
# vetoed by them. It is the mirror of why 'uncertain' deliberately DOES count.

def test_an_analysis_pass_is_not_live_risk(db):
    _order(db, "analysis_only", None)
    assert journal.open_spreads(path=db) == [], (
        "an analysis pass withheld the entry - counting it as an open spread "
        "would fill the concentration limit with positions that do not exist")


def test_analysis_only_is_listed_as_dead():
    assert "analysis_only" in journal.DEAD_STATUSES


def test_uncertain_is_still_live_risk(db):
    # The asymmetry that makes analysis_only safe to exclude is exactly what
    # makes 'uncertain' unsafe to exclude. Pin both together.
    _order(db, "uncertain", None)
    assert len(journal.open_spreads(path=db)) == 1
