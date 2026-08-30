"""Probability and EV maths. The dual-measure design is the core claim."""

import math

import pytest

from datetime import date, datetime

from agent.data import MarketSnapshot, OptionRow
from agent.screener import (_build_spreads, _three_point_ev, norm_cdf,
                            prob_below)


# --------------------------------------------------------------------------- #
# normal CDF
# --------------------------------------------------------------------------- #

def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.645) == pytest.approx(0.95, abs=1e-3)
    assert norm_cdf(-1.645) == pytest.approx(0.05, abs=1e-3)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_norm_cdf_is_monotonic():
    vals = [norm_cdf(x) for x in (-3, -1, 0, 1, 3)]
    assert vals == sorted(vals)


# --------------------------------------------------------------------------- #
# real-world probability
# --------------------------------------------------------------------------- #

def test_atm_probability_is_near_half():
    """At the money with tiny drift, P(below) should sit just above 0.5."""
    p = prob_below(spot=100.0, strike=100.0, vol=0.20, years=30 / 365)
    assert 0.50 <= p <= 0.53


def test_further_otm_is_less_likely():
    """A strike further below spot must be less likely to be breached."""
    near = prob_below(100.0, 97.0, 0.20, 30 / 365)
    far = prob_below(100.0, 90.0, 0.20, 30 / 365)
    assert far < near < 0.5


def test_higher_vol_raises_breach_probability():
    calm = prob_below(100.0, 95.0, 0.10, 30 / 365)
    wild = prob_below(100.0, 95.0, 0.40, 30 / 365)
    assert wild > calm


def test_more_time_raises_breach_probability():
    short = prob_below(100.0, 95.0, 0.20, 2 / 365)
    long = prob_below(100.0, 95.0, 0.20, 60 / 365)
    assert long > short


def test_degenerate_inputs_return_nan():
    for args in ((0, 100, 0.2, 0.1), (100, 0, 0.2, 0.1),
                 (100, 95, 0, 0.1), (100, 95, 0.2, 0)):
        assert math.isnan(prob_below(*args))


# --------------------------------------------------------------------------- #
# three-point EV
# --------------------------------------------------------------------------- #

def test_ev_positive_when_breach_unlikely():
    ev, pop = _three_point_ev(0.10, 0.05, max_profit=120.0, max_loss=380.0)
    assert pop == pytest.approx(0.90)
    assert ev > 0


def test_ev_negative_when_breach_likely():
    ev, pop = _three_point_ev(0.60, 0.50, max_profit=120.0, max_loss=380.0)
    assert pop == pytest.approx(0.40)
    assert ev < 0


def test_ev_decreases_as_breach_probability_rises():
    evs = [_three_point_ev(p, p * 0.6, 120.0, 380.0)[0]
           for p in (0.10, 0.20, 0.30, 0.40)]
    assert evs == sorted(evs, reverse=True)


def test_probabilities_are_coherent():
    """win + partial + maxloss must sum to 1."""
    p_short, p_long = 0.30, 0.18
    _, p_win = _three_point_ev(p_short, p_long, 120.0, 380.0)
    p_partial = p_short - p_long
    assert p_win + p_partial + p_long == pytest.approx(1.0)


def test_long_delta_above_short_does_not_produce_negative_probability():
    """Malformed input must not silently create impossible probabilities."""
    ev, pop = _three_point_ev(0.20, 0.35, 120.0, 380.0)
    assert not math.isnan(ev)
    assert 0.0 <= pop <= 1.0


# --------------------------------------------------------------------------- #
# the point of the whole design
# --------------------------------------------------------------------------- #

def test_real_world_beats_risk_neutral_when_iv_exceeds_realised():
    """The volatility risk premium, expressed as code.

    Implied vol 20% (what we are paid on) versus realised 12% (what actually
    happens). The real-world EV must exceed the risk-neutral EV - that gap is
    the premium documented by Bakshi & Kapadia (2003) and Carr & Wu (2009).
    """
    spot, short_k, long_k = 100.0, 95.0, 90.0
    years = 7 / 365

    p_short_rn = prob_below(spot, short_k, 0.20, years)   # implied
    p_long_rn = prob_below(spot, long_k, 0.20, years)
    p_short_rw = prob_below(spot, short_k, 0.12, years)   # realised
    p_long_rw = prob_below(spot, long_k, 0.12, years)

    ev_rn, _ = _three_point_ev(p_short_rn, p_long_rn, 120.0, 380.0)
    ev_rw, _ = _three_point_ev(p_short_rw, p_long_rw, 120.0, 380.0)

    assert p_short_rw < p_short_rn      # realised implies a safer world
    assert ev_rw > ev_rn                # so the trade is worth more


# --------------------------------------------------------------------------- #
# regression: vrp_edge must measure the PREMIUM, not the model mismatch
# --------------------------------------------------------------------------- #

def _chain(iv: float):
    """A minimal IWM-shaped chain holding one valid 300/301 call spread."""
    exp = date(2026, 9, 11)
    common = dict(underlying="IWM", right="call", expiry=exp, dte=12,
                  open_interest=4836, gamma=0.0, theta=0.0, vega=0.0, iv=iv)
    return [
        OptionRow(symbol="IWM260911C00300000", strike=300.0,
                  bid=0.49, ask=0.51, delta=0.3184, **common),
        OptionRow(symbol="IWM260911C00301000", strike=301.0,
                  bid=0.155, ask=0.175, delta=0.2718, **common),
    ]


def _snapshot(iv: float, realised: float) -> MarketSnapshot:
    return MarketSnapshot(symbol="IWM", spot=295.74, realized_vol=realised,
                          sma20=293.0, rows=_chain(iv), feed="indicative",
                          asof=datetime(2026, 8, 30, 6, 2, 43))


def test_no_premium_is_reported_when_implied_equals_realised():
    """The bug this rewrite exists to kill.

    Journal run 3 traded IWM with implied 14.84% against realised 14.58% - a
    ratio of 1.018, meaning essentially NO volatility risk premium was on
    offer. It still reported vrp_edge = 2.75, because ev_rn came from delta
    (N(d1)) while ev_rw came from a lognormal (N(d2)-shaped). Those disagree
    even at identical volatility, so the "premium" was 86% model mismatch.

    Same vol on both sides must mean no edge. If this test ever fails, the two
    measures have drifted onto different models again.
    """
    [c] = _build_spreads(_snapshot(iv=0.147, realised=0.147), "call")
    assert c.vrp_edge == pytest.approx(0.0, abs=0.01)
    assert c.ev == pytest.approx(c.ev_rn, abs=0.01)


def test_premium_appears_only_when_implied_exceeds_realised():
    """Rich implied vol must produce a positive, monotonically larger edge."""
    edges = [_build_spreads(_snapshot(iv=iv, realised=0.147), "call")[0].vrp_edge
             for iv in (0.147, 0.18, 0.25)]
    assert edges[0] == pytest.approx(0.0, abs=0.01)
    assert edges == sorted(edges)
    assert edges[-1] > 1.0


def test_cheap_implied_vol_produces_a_negative_edge():
    """Implied BELOW realised means we would be selling vol too cheap."""
    [c] = _build_spreads(_snapshot(iv=0.10, realised=0.147), "call")
    assert c.vrp_edge < 0


def test_delta_is_not_the_itm_probability():
    """Why ev_rn cannot be computed from delta.

    Delta is N(d1); P(finishing ITM) is N(d2). The gap is ~sigma*sqrt(T) and at
    12 DTE it is worth more EV than MIN_EV_RW, so it cannot be waved away.
    """
    [c] = _build_spreads(_snapshot(iv=0.147, realised=0.147), "call")
    p_itm_model = 1.0 - c.pop_rn
    assert abs(p_itm_model - abs(c.short_delta)) > 0.01


def test_partial_region_averages_the_linear_payoff():
    """Between the strikes the payoff runs +max_profit -> -max_loss.

    The midpoint is (max_profit - max_loss)/2, NOT -max_loss/2: at the short
    strike the spread still expires for the whole credit.
    """
    mp, ml = 120.0, 380.0
    ev, _ = _three_point_ev(1.0, 0.0, mp, ml)   # all probability in the band
    assert ev == pytest.approx((mp - ml) / 2)
