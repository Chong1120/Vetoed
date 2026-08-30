"""Guardrail tests. The invariant that matters: they can only RESTRICT."""

import os
import tempfile

import pytest

from agent import adapt, journal, screener
from agent.adapt import Guardrails, circuit_breaker, regime_adjust


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    journal.init(path)
    yield path


def add_closed(db, underlying, pnl, delta=0.30):
    run = journal.start_run(True, "indicative", 100000.0, 0.0, False, 5, path=db)
    did = journal.record_decision(run, {"underlying": underlying}, None, None,
                                  path=db)
    oid = "o-%s-%d" % (underlying, abs(hash((underlying, pnl, delta))) % 10**6)
    journal.record_order(
        did, {"underlying": underlying, "kind": "put_credit",
              "short_symbol": "S", "long_symbol": "L", "credit": 1.0,
              "short_delta": -delta, "dte": 5},
        1, 0.95, 380.0, {"id": oid, "status": "filled"}, path=db)
    journal.close_order(oid, pnl, path=db)


# --------------------------------------------------------------------------- #
# tier 1 - regime
# --------------------------------------------------------------------------- #

def ctx(ratio):
    return {"underlyings": {"SPY": {"iv_vs_rv": ratio}}}


def test_rich_premium_allows_longer_dated():
    g = Guardrails()
    regime_adjust(ctx(1.30), g)
    assert g.dte_max == 14
    assert any("rich" in n for n in g.notes)


def test_thin_premium_stays_short_dated():
    g = Guardrails()
    regime_adjust(ctx(0.85), g)
    assert g.dte_max == 7
    assert any("thin" in n for n in g.notes)


def test_neutral_regime_changes_nothing():
    g = Guardrails()
    regime_adjust(ctx(1.02), g)
    assert g.dte_min is None and g.dte_max is None


def test_missing_ratio_is_safe():
    g = Guardrails()
    regime_adjust({"underlyings": {"SPY": {}}}, g)
    assert g.dte_min is None


# --------------------------------------------------------------------------- #
# tier 2 - circuit breaker
# --------------------------------------------------------------------------- #

def test_too_few_trades_changes_nothing(db):
    add_closed(db, "SPY", -100.0)
    g = Guardrails()
    circuit_breaker(g, db)
    assert g.banned_underlyings == []
    assert g.delta_max is None
    assert any("not evidence" in n for n in g.notes)


def test_three_consecutive_losses_bans_underlying(db):
    for _ in range(3):
        add_closed(db, "QQQ", -120.0)
    for _ in range(2):
        add_closed(db, "SPY", 80.0)
    g = Guardrails()
    circuit_breaker(g, db)
    assert "QQQ" in g.banned_underlyings
    assert "SPY" not in g.banned_underlyings


def test_a_win_resets_the_streak(db):
    add_closed(db, "QQQ", -120.0)
    add_closed(db, "QQQ", -120.0)
    add_closed(db, "QQQ", 90.0)     # most recent is a win
    add_closed(db, "SPY", 10.0)
    add_closed(db, "SPY", 10.0)
    g = Guardrails()
    circuit_breaker(g, db)
    assert "QQQ" not in g.banned_underlyings


def test_losing_delta_bucket_narrows_band(db):
    for _ in range(5):
        add_closed(db, "SPY", -100.0, delta=0.32)
    g = Guardrails()
    circuit_breaker(g, db)
    assert g.delta_max == 0.25


def test_winning_delta_bucket_does_not_widen(db):
    for _ in range(6):
        add_closed(db, "SPY", 100.0, delta=0.32)
    g = Guardrails()
    circuit_breaker(g, db)
    assert g.delta_max is None      # never widens on success


# --------------------------------------------------------------------------- #
# THE invariant
# --------------------------------------------------------------------------- #

def test_guardrails_can_never_loosen_beyond_defaults(db):
    """Even a hostile Guardrails object gets clamped back to the defaults."""
    g = adapt.build(ctx(1.30), db)
    if g.dte_min is not None:
        assert g.dte_min >= screener.DTE_MIN
    if g.dte_max is not None:
        assert g.dte_max <= screener.DTE_MAX
    if g.delta_max is not None:
        assert g.delta_max <= screener.SHORT_DELTA_MAX
    if g.delta_min is not None:
        assert g.delta_min >= screener.SHORT_DELTA_MIN


def test_build_never_raises_on_bad_context(db):
    for bad in (None, {}, {"underlyings": None}, {"underlyings": {"X": None}}):
        g = adapt.build(bad, db)
        assert isinstance(g, Guardrails)


def test_overrides_only_contain_set_fields():
    g = Guardrails()
    assert g.to_overrides() == {}
    g.dte_max = 7
    assert g.to_overrides() == {"dte_max": 7}
