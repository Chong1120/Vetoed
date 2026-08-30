"""Unattended operation: restart safety, idempotency, locking, hard locks.

These test the failure modes an unattended agent actually produces - a cold
start with a stale journal, a timed-out submission, a killed process, a stale
lock, a journal that disagrees with the broker - rather than the happy path.
On GitHub Actions every tick is a cold start, so these are the normal path
there, not the edge case. Anything that could turn a restart into a duplicate
position belongs in this file.
"""

import os
import tempfile
import time
from datetime import date

import pytest

from agent import journal, loop, reconcile, runlock
from agent.reconcile import BrokerState


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(path)
    yield path


@pytest.fixture()
def lockfile():
    path = os.path.join(tempfile.mkdtemp(), "cycle.lock")
    yield path
    if os.path.exists(path):
        os.unlink(path)


def _row(**over):
    row = {"underlying": "AAPL", "kind": "put_credit",
           "short_symbol": "AAPL260911P00310000",
           "long_symbol": "AAPL260911P00305000",
           "contracts": 12, "credit": 0.94, "max_loss_total": 4872.0,
           "status": "filled", "alpaca_order_id": "oid-1",
           "client_order_id": "coid-1", "realised_pnl": None}
    row.update(over)
    return row


def _pos(symbol):
    return {"symbol": symbol, "unrealized_pl": "0"}


# --------------------------------------------------------------------------- #
# paper-trading hard lock
# --------------------------------------------------------------------------- #

def test_refuses_to_start_when_paper_flag_is_not_true(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    monkeypatch.setattr(loop, "ROOT", tempfile.mkdtemp())   # no .env to reload
    with pytest.raises(SystemExit) as exc:
        loop.assert_paper_trading()
    assert "REFUSING TO START" in str(exc.value)


def test_refuses_to_start_when_paper_flag_is_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    monkeypatch.setattr(loop, "ROOT", tempfile.mkdtemp())
    with pytest.raises(SystemExit):
        loop.assert_paper_trading()


def test_accepts_the_paper_flag(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setattr(loop, "ROOT", tempfile.mkdtemp())
    loop.assert_paper_trading()          # must not raise


# --------------------------------------------------------------------------- #
# scheduling configuration
# --------------------------------------------------------------------------- #

def test_default_poll_interval_is_thirty_minutes(monkeypatch):
    monkeypatch.delenv("POLL_INTERVAL_MINUTES", raising=False)
    assert loop.poll_interval_minutes() == 30


def test_poll_interval_is_configurable(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_MINUTES", "5")
    assert loop.poll_interval_minutes() == 5


@pytest.mark.parametrize("bad", ["0", "-5", "999", "abc", "5.5", ""])
def test_bad_poll_interval_falls_back_to_the_default(monkeypatch, bad):
    """A typo in a unit file must not silently produce a 1-second loop."""
    monkeypatch.setenv("POLL_INTERVAL_MINUTES", bad)
    assert loop.poll_interval_minutes() == 30


def test_force_is_refused_together_with_schedule(monkeypatch):
    """--force ignores market hours. Scheduled, that means weekend quotes."""
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setattr(loop, "ROOT", tempfile.mkdtemp())
    monkeypatch.setattr("sys.argv", ["loop", "--schedule", "--force", "--live"])
    with pytest.raises(SystemExit) as exc:
        loop.main()
    assert "--force cannot be combined with --schedule" in str(exc.value)


# --------------------------------------------------------------------------- #
# single-flight lock
# --------------------------------------------------------------------------- #

def test_second_cycle_cannot_start_while_the_first_holds_the_lock(lockfile):
    with runlock.single_flight(lockfile):
        with pytest.raises(runlock.LockBusy):
            with runlock.single_flight(lockfile):
                pytest.fail("two cycles ran at once")


def test_lock_is_released_after_a_normal_cycle(lockfile):
    with runlock.single_flight(lockfile):
        pass
    with runlock.single_flight(lockfile):
        pass                                   # must not raise


def test_lock_is_released_even_when_the_cycle_raises(lockfile):
    with pytest.raises(ValueError):
        with runlock.single_flight(lockfile):
            raise ValueError("cycle blew up")
    assert not os.path.exists(lockfile)


def test_a_lock_left_by_a_dead_process_is_reclaimed(lockfile):
    """Otherwise one crash means the service can never start again."""
    with open(lockfile, "w", encoding="utf-8") as fh:
        fh.write("999999 %f\n" % time.time())   # pid that does not exist
    notes = []
    with runlock.single_flight(lockfile, on_stale=notes.append):
        pass
    assert notes and "reclaiming stale lock" in notes[0]


def test_a_lock_held_implausibly_long_is_reclaimed(lockfile):
    """A hung holder must not wedge the service forever."""
    with open(lockfile, "w", encoding="utf-8") as fh:
        fh.write("%d %f\n" % (os.getpid(),
                              time.time() - runlock.STALE_AFTER_SECONDS - 60))
    notes = []
    with runlock.single_flight(lockfile, on_stale=notes.append):
        pass
    assert notes


def test_is_held_reports_the_lock_state(lockfile):
    assert runlock.is_held(lockfile) is False
    with runlock.single_flight(lockfile):
        assert runlock.is_held(lockfile) is True
    assert runlock.is_held(lockfile) is False


# --------------------------------------------------------------------------- #
# deterministic client_order_id - the idempotency primitive
# --------------------------------------------------------------------------- #

def test_the_same_trade_intent_produces_the_same_client_order_id():
    """Alpaca rejects a duplicate client_order_id. That only helps if a retry
    generates the SAME id, which a timestamp-based one never does."""
    a = reconcile.deterministic_client_order_id("AAPL", "S1", "L1", 12,
                                                date(2026, 9, 1))
    b = reconcile.deterministic_client_order_id("AAPL", "S1", "L1", 12,
                                                date(2026, 9, 1))
    assert a == b


@pytest.mark.parametrize("kw", [
    {"underlying": "SPY"}, {"short_symbol": "S2"},
    {"long_symbol": "L2"}, {"contracts": 11},
])
def test_a_different_trade_produces_a_different_id(kw):
    base = dict(underlying="AAPL", short_symbol="S1", long_symbol="L1",
                contracts=12, day=date(2026, 9, 1))
    other = dict(base); other.update(kw)
    assert (reconcile.deterministic_client_order_id(**base)
            != reconcile.deterministic_client_order_id(**other))


def test_the_same_spread_may_be_reopened_on_a_later_day():
    assert (reconcile.deterministic_client_order_id("AAPL", "S", "L", 1,
                                                    date(2026, 9, 1))
            != reconcile.deterministic_client_order_id("AAPL", "S", "L", 1,
                                                       date(2026, 9, 2)))


# --------------------------------------------------------------------------- #
# pre-submit duplicate guard
# --------------------------------------------------------------------------- #

CANDIDATE = {"underlying": "AAPL", "short_symbol": "AAPL260911P00310000",
             "long_symbol": "AAPL260911P00305000"}


def test_no_duplicate_when_the_broker_is_empty():
    assert reconcile.already_working(BrokerState(), CANDIDATE, 12) == ""


def test_duplicate_detected_by_client_order_id():
    coid = reconcile.deterministic_client_order_id(
        "AAPL", CANDIDATE["short_symbol"], CANDIDATE["long_symbol"], 12)
    state = BrokerState(open_order_ids={coid})
    assert "already working" in reconcile.already_working(state, CANDIDATE, 12)


def test_duplicate_detected_by_an_existing_position():
    state = BrokerState(legs={CANDIDATE["short_symbol"]: _pos("x")})
    assert "already held" in reconcile.already_working(state, CANDIDATE, 12)


def test_duplicate_detected_by_a_working_order_on_the_same_leg():
    state = BrokerState(open_order_symbols={CANDIDATE["short_symbol"]})
    assert "working order" in reconcile.already_working(state, CANDIDATE, 12)


# --------------------------------------------------------------------------- #
# broker reconciliation - the broker is authoritative
# --------------------------------------------------------------------------- #

def test_a_spread_held_at_the_broker_is_reported_open():
    row = _row()
    state = BrokerState(legs={row["short_symbol"]: _pos(row["short_symbol"]),
                              row["long_symbol"]: _pos(row["long_symbol"])})
    rec = reconcile.reconcile(state, rows=[row])
    assert len(rec.open_spreads) == 1
    assert rec.orphan_legs == []


def test_a_spread_gone_from_the_broker_is_marked_closed(db):
    """Expired, assigned, or closed by hand. The journal must follow."""
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 1, path=db)
    did = journal.record_decision(run, {"underlying": "AAPL"}, None, None, path=db)
    journal.record_order(did, {"underlying": "AAPL", "kind": "put_credit",
                               "short_symbol": "S", "long_symbol": "L",
                               "credit": 0.94},
                         12, 0.89, 4872.0, {"id": "oid-9", "status": "filled"},
                         path=db)
    assert len(journal.open_spreads(path=db)) == 1
    rec = reconcile.reconcile(BrokerState(), path=db)   # broker holds nothing
    assert rec.open_spreads == []
    assert journal.open_spreads(path=db) == []
    assert any("marking closed" in c for c in rec.corrections)


def test_an_uncertain_order_the_broker_never_saw_is_marked_not_filled(db):
    """A timeout resolved against the broker: it genuinely never arrived."""
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 1, path=db)
    did = journal.record_decision(run, {"underlying": "AAPL"}, None, None, path=db)
    journal.record_order(did, {"underlying": "AAPL", "kind": "put_credit",
                               "short_symbol": "S", "long_symbol": "L",
                               "credit": 0.94},
                         12, 0.89, 4872.0,
                         {"id": "oid-u", "status": "uncertain"}, path=db)
    assert len(journal.open_spreads(path=db)) == 1      # counted while unknown
    rec = reconcile.reconcile(BrokerState(), path=db)
    assert rec.open_spreads == []
    assert journal.open_spreads(path=db) == []
    assert any("marking not-filled" in c for c in rec.corrections)


def test_an_uncertain_order_is_live_risk_until_the_broker_says_otherwise(db):
    """The asymmetry that motivates the whole design: counting a phantom
    position costs one skipped trade, missing a real one doubles it."""
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 1, path=db)
    did = journal.record_decision(run, {"underlying": "AAPL"}, None, None, path=db)
    journal.record_order(did, {"underlying": "AAPL", "kind": "put_credit",
                               "short_symbol": "S", "long_symbol": "L",
                               "credit": 0.94},
                         12, 0.89, 4872.0,
                         {"id": "oid-u2", "status": "uncertain"}, path=db)
    rows = journal.open_spreads(path=db)
    assert len(rows) == 1 and rows[0]["status"] == "uncertain"


def test_a_rejected_order_is_never_counted_as_risk(db):
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 1, path=db)
    did = journal.record_decision(run, {"underlying": "AAPL"}, None, None, path=db)
    journal.record_order(did, {"underlying": "AAPL", "kind": "put_credit",
                               "short_symbol": "S", "long_symbol": "L",
                               "credit": 0.94},
                         12, 0.89, 4872.0, {"status": "failed"}, path=db)
    assert journal.open_spreads(path=db) == []


def test_a_resting_order_is_still_counted_as_open():
    """Submitted, not yet filled. Opening another would double the position."""
    row = _row(status="new")
    state = BrokerState(open_order_ids={row["client_order_id"]})
    rec = reconcile.reconcile(state, rows=[row])
    assert len(rec.open_spreads) == 1
    assert any("still working" in c for c in rec.corrections)


def test_a_single_leg_at_the_broker_is_flagged_loudly():
    """One leg means an uncapped position. It must block new entries."""
    row = _row()
    state = BrokerState(legs={row["short_symbol"]: _pos(row["short_symbol"])})
    rec = reconcile.reconcile(state, rows=[row])
    assert len(rec.open_spreads) == 1
    assert any("ONE LEG ONLY" in c for c in rec.corrections)


def test_positions_the_journal_never_recorded_are_counted_anyway():
    """A crash between submit and record_order leaves exactly this state."""
    state = BrokerState(legs={"ORPHAN1": _pos("ORPHAN1"),
                              "ORPHAN2": _pos("ORPHAN2")})
    rec = reconcile.reconcile(state, rows=[])
    assert set(rec.orphan_legs) == {"ORPHAN1", "ORPHAN2"}
    acct = reconcile.account_state_from(rec, 100000.0, 200000.0, 0.0)
    assert acct.open_positions == 1        # two legs make one spread


def test_an_unreachable_broker_never_reports_a_clean_slate():
    """Falling back to the journal is acceptable; pretending nothing is open
    is not, because that is the state in which we would open more."""
    row = _row()
    state = BrokerState(reachable=False, error="ConnectionError: timed out")
    rec = reconcile.reconcile(state, rows=[row])
    assert len(rec.open_spreads) == 1
    assert any("BROKER UNREACHABLE" in c for c in rec.corrections)


def test_account_state_is_built_from_broker_confirmed_rows():
    rows = [_row(underlying="AAPL", max_loss_total=4872.0),
            _row(underlying="SPY", short_symbol="S2", long_symbol="L2",
                 max_loss_total=924.0)]
    legs = {}
    for r in rows:
        legs[r["short_symbol"]] = _pos(r["short_symbol"])
        legs[r["long_symbol"]] = _pos(r["long_symbol"])
    rec = reconcile.reconcile(BrokerState(legs=legs), rows=rows)
    acct = reconcile.account_state_from(rec, 100000.0, 200000.0, -250.0)
    assert acct.open_positions == 2
    assert acct.open_risk == pytest.approx(5796.0)
    assert acct.positions_by_underlying == {"AAPL": 1, "SPY": 1}
    assert acct.day_pnl == -250.0


# --------------------------------------------------------------------------- #
# health file
# --------------------------------------------------------------------------- #

def test_health_file_is_written_and_merged(monkeypatch, tmp_path):
    path = str(tmp_path / "health.json")
    monkeypatch.setattr(loop, "HEALTH_PATH", path)
    loop.write_health(cycle_state="running", equity=100000.0)
    loop.write_health(cycle_state="idle")
    import json
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["cycle_state"] == "idle"
    assert data["equity"] == 100000.0          # earlier field preserved
    assert data["pid"] == os.getpid()
    assert "updated_at" in data


def test_health_write_failure_never_raises(monkeypatch):
    monkeypatch.setattr(loop, "HEALTH_PATH", "/nonexistent\x00/health.json")
    loop.write_health(cycle_state="running")    # must not raise
