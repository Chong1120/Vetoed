"""Gate tests. The naked-short and loss-cap gates are the ones that matter."""

import pytest

from agent.risk import (
    AccountState,
    MAX_RISK_PCT_PER_POSITION,
    evaluate,
    size_position,
)
from agent.screener import SpreadCandidate


def make_candidate(**over) -> SpreadCandidate:
    """A well-formed 5-wide SPY put credit spread: sell 765, buy 760."""
    base = dict(
        underlying="SPY", kind="put_credit", expiry="2026-09-04", dte=5,
        short_symbol="SPY260904P00765000", long_symbol="SPY260904P00760000",
        short_strike=765.0, long_strike=760.0, width=5.0,
        credit=1.20, max_loss=380.0, max_profit=120.0, credit_ratio=0.24,
        short_delta=-0.25, long_delta=-0.15, pop=0.75, ev=25.0,
        short_iv=0.13, min_open_interest=1500, worst_spread_pct=0.02,
        distance_pct=0.0075, score=0.06,
    )
    base.update(over)
    return SpreadCandidate(**base)


def make_account(**over) -> AccountState:
    base = dict(equity=100_000.0, options_buying_power=100_000.0, day_pnl=0.0,
                open_positions=0, open_risk=0.0, positions_by_underlying={},
                halted=False)
    base.update(over)
    return AccountState(**base)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #

def test_valid_spread_is_approved_and_sized():
    d = evaluate(make_candidate(), make_account())
    assert d.approved, d.vetoes
    assert d.contracts >= 1
    # 2% of 100k = $2000 budget / $380 max loss = 5 contracts
    assert d.contracts == 5
    assert d.max_loss_total == pytest.approx(1900.0)


def test_position_risk_never_exceeds_the_per_position_cap():
    acct = make_account()
    d = evaluate(make_candidate(), acct)
    assert d.max_loss_total <= acct.equity * MAX_RISK_PCT_PER_POSITION


# --------------------------------------------------------------------------- #
# NAKED SHORT / undefined risk - the gates that must never fail open
# --------------------------------------------------------------------------- #

def test_missing_long_leg_is_vetoed_as_naked():
    d = evaluate(make_candidate(long_symbol=""), make_account())
    assert not d.approved
    assert any("NAKED" in v for v in d.vetoes)


def test_put_spread_with_long_strike_above_short_is_vetoed():
    """Long leg on the wrong side does not cap the loss."""
    d = evaluate(make_candidate(long_strike=770.0), make_account())
    assert not d.approved
    assert any("not capped" in v.lower() for v in d.vetoes)


def test_call_spread_with_long_strike_below_short_is_vetoed():
    d = evaluate(make_candidate(kind="call_credit", short_strike=775.0,
                                long_strike=770.0), make_account())
    assert not d.approved
    assert any("not capped" in v.lower() for v in d.vetoes)


def test_identical_legs_vetoed():
    c = make_candidate(long_symbol="SPY260904P00765000")
    d = evaluate(c, make_account())
    assert not d.approved


def test_max_loss_inconsistent_with_strikes_is_vetoed():
    """Guards against a candidate whose arithmetic was tampered with."""
    d = evaluate(make_candidate(max_loss=50.0), make_account())
    assert not d.approved
    assert any("max_loss" in v for v in d.vetoes)


# --------------------------------------------------------------------------- #
# session + bounds
# --------------------------------------------------------------------------- #

def test_daily_loss_stop_halts_trading():
    d = evaluate(make_candidate(), make_account(day_pnl=-3_500.0))
    assert not d.approved
    assert any("DAILY LOSS STOP" in v for v in d.vetoes)


def test_explicit_halt_flag_blocks_everything():
    d = evaluate(make_candidate(), make_account(halted=True))
    assert not d.approved
    assert any("HALTED" in v for v in d.vetoes)


def test_zero_dte_is_vetoed():
    d = evaluate(make_candidate(dte=0), make_account())
    assert not d.approved
    assert any("DTE" in v for v in d.vetoes)


def test_far_dated_is_vetoed():
    d = evaluate(make_candidate(dte=45), make_account())
    assert not d.approved


def test_illiquid_is_vetoed():
    d = evaluate(make_candidate(min_open_interest=10), make_account())
    assert not d.approved
    assert any("LIQUIDITY" in v for v in d.vetoes)


def test_absurd_iv_is_vetoed():
    assert not evaluate(make_candidate(short_iv=0.001), make_account()).approved
    assert not evaluate(make_candidate(short_iv=3.0), make_account()).approved


def test_missing_iv_is_vetoed():
    d = evaluate(make_candidate(short_iv=None), make_account())
    assert not d.approved


# --------------------------------------------------------------------------- #
# concentration + capital
# --------------------------------------------------------------------------- #

def test_max_concurrent_positions():
    d = evaluate(make_candidate(), make_account(open_positions=5))
    assert not d.approved
    assert any("CONCENTRATION" in v for v in d.vetoes)


def test_max_per_underlying():
    d = evaluate(make_candidate(),
                 make_account(open_positions=2,
                              positions_by_underlying={"SPY": 2}))
    assert not d.approved
    assert any("CONCENTRATION" in v for v in d.vetoes)


def test_portfolio_risk_headroom_limits_size():
    """Already near the 10% portfolio cap -> size shrinks toward zero."""
    acct = make_account(open_risk=9_800.0, open_positions=2,
                        positions_by_underlying={"QQQ": 2})
    contracts, _ = size_position(make_candidate(), acct)
    assert contracts == 0


def test_low_buying_power_shrinks_size():
    acct = make_account(options_buying_power=1_000.0)
    contracts, _ = size_position(make_candidate(), acct)
    assert contracts == 1  # 50% headroom of $1000 = $500 / $380 -> 1


def test_zero_buying_power_is_vetoed():
    d = evaluate(make_candidate(), make_account(options_buying_power=0.0))
    assert not d.approved


# --------------------------------------------------------------------------- #
# the LLM cannot widen a limit
# --------------------------------------------------------------------------- #

def test_llm_cannot_influence_size():
    """Sizing is a pure function of candidate + account. Nothing else."""
    c = make_candidate()
    a = make_account()
    first, _ = size_position(c, a)
    second, _ = size_position(c, a)
    assert first == second == 5
