"""Probability and expected-value maths.

The dual-valuation design is the project's central claim, so these tests are
written to falsify it rather than to confirm it. The most important one is
`test_spread_ev_matches_numerical_integration`: it checks the closed form
against a brute-force integral of the actual payoff, which is the only way to
know the analytic shortcut is right rather than merely plausible.
"""

import math
from datetime import date, datetime

import pytest

from agent.data import MarketSnapshot, OptionRow
from agent.screener import (MIN_VRP_EDGE, _build_spreads, _d, norm_cdf,
                            prob_below, spread_ev)


# --------------------------------------------------------------------------- #
# reference implementations - deliberately dumb, so they can disagree
# --------------------------------------------------------------------------- #

def payoff(spot_at_expiry: float, k_short: float, k_long: float,
           credit: float, right: str) -> float:
    """The contract, written out literally. No probability involved."""
    width = abs(k_short - k_long)
    max_profit = credit * 100.0
    max_loss = width * 100.0 - max_profit
    s = spot_at_expiry
    if right == "put":
        if s >= k_short:
            return max_profit
        if s <= k_long:
            return -max_loss
        return 100.0 * (credit - (k_short - s))
    if s <= k_short:
        return max_profit
    if s >= k_long:
        return -max_loss
    return 100.0 * (credit - (s - k_short))


def ev_by_integration(spot, k_short, k_long, credit, vol, years, right,
                      steps=200000) -> float:
    """E[payoff] by trapezoidal integration over the lognormal density.

    Slow and obvious on purpose. If the closed form in screener.py ever drifts
    from the payoff it claims to price, this disagrees.
    """
    sd = vol * math.sqrt(years)
    mean = math.log(spot) - 0.5 * vol * vol * years
    lo, hi = mean - 8 * sd, mean + 8 * sd
    h = (hi - lo) / steps
    total = 0.0
    for i in range(steps + 1):
        x = lo + i * h
        pdf = math.exp(-0.5 * ((x - mean) / sd) ** 2) / (sd * math.sqrt(2 * math.pi))
        weight = 0.5 if i in (0, steps) else 1.0
        total += weight * payoff(math.exp(x), k_short, k_long, credit, right) * pdf * h
    return total


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
# the lognormal
# --------------------------------------------------------------------------- #

def test_further_otm_is_less_likely():
    near = prob_below(100.0, 97.0, 0.20, 30 / 365)
    far = prob_below(100.0, 90.0, 0.20, 30 / 365)
    assert far < near < 0.5


def test_higher_vol_raises_breach_probability():
    assert prob_below(100.0, 95.0, 0.40, 30 / 365) > \
           prob_below(100.0, 95.0, 0.10, 30 / 365)


def test_more_time_raises_breach_probability():
    assert prob_below(100.0, 95.0, 0.20, 60 / 365) > \
           prob_below(100.0, 95.0, 0.20, 2 / 365)


def test_degenerate_inputs_return_nan():
    for args in ((0, 100, 0.2, 0.1), (100, 0, 0.2, 0.1),
                 (100, 95, 0, 0.1), (100, 95, 0.2, 0)):
        assert math.isnan(prob_below(*args))


def test_model_is_zero_drift_so_expected_terminal_price_is_spot():
    """The stated assumption: E[S_T] = S_0. No drift, no equity risk premium.

    If this ever fails, every probability in the system has quietly acquired a
    directional view that the documentation does not admit to.
    """
    spot, vol, years = 100.0, 0.25, 0.5
    sd = vol * math.sqrt(years)
    mean = math.log(spot) - 0.5 * vol * vol * years
    # E[S_T] = exp(mean + sd^2/2)
    assert math.exp(mean + 0.5 * sd * sd) == pytest.approx(spot, rel=1e-12)


# --------------------------------------------------------------------------- #
# THE load-bearing test: is the closed form actually exact?
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("right,k_short,k_long,spot", [
    ("put", 310.0, 305.0, 319.92),
    ("put", 758.0, 757.0, 769.28),
    ("put", 300.0, 295.0, 305.00),
    ("call", 300.0, 301.0, 295.73),
    ("call", 723.0, 728.0, 716.91),
    ("call", 110.0, 112.0, 100.00),
])
@pytest.mark.parametrize("vol", [0.10, 0.22, 0.45])
@pytest.mark.parametrize("dte", [2, 7, 14])
def test_spread_ev_matches_numerical_integration(right, k_short, k_long,
                                                 spot, vol, dte):
    """The closed form must equal a brute-force integral of the payoff.

    This is what entitles the documentation to call the expected value exact
    rather than approximate. A midpoint rule over the strike band - which is
    what this code used to do - fails this by tens of dollars on wide spreads.
    """
    credit = 0.25 * abs(k_short - k_long)
    years = dte / 365.0
    ev, _ = spread_ev(spot, k_short, k_long, credit, vol, years, right)
    ref = ev_by_integration(spot, k_short, k_long, credit, vol, years, right,
                            steps=60000)
    assert ev == pytest.approx(ref, abs=0.02)


def test_midpoint_approximation_would_have_failed_that_test():
    """Documents why the exact form was worth the extra lines.

    The old implementation valued the ramp between the strikes at its midpoint
    payoff, (max_profit - max_loss) / 2. That is only correct when
    E[S_T | in the band] lands on the arithmetic midpoint, which a lognormal
    does not do. Here that error is far larger than MIN_VRP_EDGE, so it could
    change a trade decision rather than just a displayed number.
    """
    spot, k_short, k_long, credit, vol, years = 319.92, 310.0, 305.0, 0.90, 0.1891, 12 / 365
    exact, _ = spread_ev(spot, k_short, k_long, credit, vol, years, "put")

    max_profit = credit * 100.0
    max_loss = 5.0 * 100.0 - max_profit
    p_short = prob_below(spot, k_short, vol, years)
    p_long = prob_below(spot, k_long, vol, years)
    midpoint = (max_profit * (1 - p_short)
                + (p_short - p_long) * (max_profit - max_loss) * 0.5
                - p_long * max_loss)

    assert abs(exact - midpoint) > MIN_VRP_EDGE


# --------------------------------------------------------------------------- #
# payoff bounds and limiting behaviour
# --------------------------------------------------------------------------- #

def test_payoff_regions_are_correct():
    """Above, between, and below the strikes, for both orientations."""
    # put credit 310/305 at $0.90: max profit $90, max loss $410
    assert payoff(330.0, 310.0, 305.0, 0.90, "put") == pytest.approx(90.0)
    assert payoff(310.0, 310.0, 305.0, 0.90, "put") == pytest.approx(90.0)
    assert payoff(307.5, 310.0, 305.0, 0.90, "put") == pytest.approx(-160.0)
    assert payoff(305.0, 310.0, 305.0, 0.90, "put") == pytest.approx(-410.0)
    assert payoff(290.0, 310.0, 305.0, 0.90, "put") == pytest.approx(-410.0)
    # call credit 300/305 at $0.90 mirrors it
    assert payoff(280.0, 300.0, 305.0, 0.90, "call") == pytest.approx(90.0)
    assert payoff(302.5, 300.0, 305.0, 0.90, "call") == pytest.approx(-160.0)
    assert payoff(320.0, 300.0, 305.0, 0.90, "call") == pytest.approx(-410.0)


def test_expiry_breakeven_is_short_strike_offset_by_the_credit():
    """Put spread breaks even at K_short - credit, call spread at K_short + credit.

    abs=1e-9 rather than the default: 310.0 - 0.90 is not exactly 309.1 in
    binary floating point, and the residue is ~2e-12, which is larger than
    pytest's default absolute tolerance against zero.
    """
    assert payoff(310.0 - 0.90, 310.0, 305.0, 0.90, "put") == pytest.approx(0.0, abs=1e-9)
    assert payoff(300.0 + 0.90, 300.0, 305.0, 0.90, "call") == pytest.approx(0.0, abs=1e-9)


def test_max_loss_is_width_times_100_minus_credit_times_100():
    credit, width = 0.90, 5.0
    assert payoff(0.0, 310.0, 305.0, credit, "put") == \
        pytest.approx(-(width * 100.0 - credit * 100.0))


def test_ev_is_bounded_by_the_payoff_extremes():
    """An expected value cannot exceed the best or worst possible outcome."""
    for right, ks, kl, spot in (("put", 310.0, 305.0, 319.92),
                                ("call", 300.0, 305.0, 295.0)):
        for vol in (0.05, 0.20, 0.80):
            ev, _ = spread_ev(spot, ks, kl, 1.25, vol, 10 / 365, right)
            assert -(5.0 * 100.0 - 125.0) - 1e-6 <= ev <= 125.0 + 1e-6


def test_vanishing_vol_converges_to_the_full_credit():
    """With no volatility the OTM spread expires worthless and we keep it all."""
    ev, pop = spread_ev(319.92, 310.0, 305.0, 0.90, 1e-6, 12 / 365, "put")
    assert ev == pytest.approx(90.0, abs=0.01)
    assert pop == pytest.approx(1.0, abs=1e-6)


def test_ev_falls_monotonically_as_volatility_rises():
    """For a fixed credit, more volatility can only make a short spread worse."""
    evs = [spread_ev(319.92, 310.0, 305.0, 0.90, v, 12 / 365, "put")[0]
           for v in (0.05, 0.15, 0.25, 0.40, 0.80)]
    assert evs == sorted(evs, reverse=True)


def test_extreme_vol_converges_to_the_max_loss_for_a_put_spread():
    """The limit, and a note on how slowly it arrives.

    d_short = (ln(K/S) + sigma^2.T/2) / (sigma.sqrt(T)) -> sigma.sqrt(T)/2,
    so N(d_short) -> 1 and the EV tends to -max_loss. It gets there slowly:
    at 1200% vol the right tail still carries 14% of the mass and the EV is
    only -339. The convergence needs a genuinely absurd volatility, which is
    why this test uses one.
    """
    ev, pop = spread_ev(319.92, 310.0, 305.0, 0.90, 100.0, 12 / 365, "put")
    assert ev == pytest.approx(-410.0, abs=0.5)
    assert pop < 1e-6


def test_pop_is_the_probability_the_short_strike_survives():
    spot, ks, kl, vol, years = 319.92, 310.0, 305.0, 0.1891, 12 / 365
    _, pop = spread_ev(spot, ks, kl, 0.90, vol, years, "put")
    assert pop == pytest.approx(1.0 - prob_below(spot, ks, vol, years))


def test_three_region_probabilities_sum_to_one():
    spot, ks, kl, vol, years = 319.92, 310.0, 305.0, 0.1891, 12 / 365
    p_win = 1.0 - prob_below(spot, ks, vol, years)
    p_band = prob_below(spot, ks, vol, years) - prob_below(spot, kl, vol, years)
    p_maxloss = prob_below(spot, kl, vol, years)
    assert p_win + p_band + p_maxloss == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# delta is not a probability
# --------------------------------------------------------------------------- #

def _bs_delta_and_itm(spot, strike, vol, years, right):
    """(|delta|, risk-neutral P(finish ITM)) under the same zero-drift model."""
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / sd
    d2 = d1 - sd
    if right == "call":
        return norm_cdf(d1), norm_cdf(d2)
    return norm_cdf(-d1), norm_cdf(-d2)


def test_call_delta_overstates_the_itm_probability():
    """delta = N(d1), P(ITM) = N(d2), and d1 > d2, so delta is the larger."""
    delta, p_itm = _bs_delta_and_itm(295.73, 300.0, 0.25, 12 / 365, "call")
    assert delta > p_itm
    assert delta - p_itm > 0.005


def test_put_delta_understates_the_itm_probability():
    """|put delta| = N(-d1) and P(ITM) = N(-d2); -d1 < -d2, so delta is smaller.

    The sign of the error flips between calls and puts. That is exactly why
    substituting delta for a probability is not a harmless simplification -
    it biases call spreads and put spreads in opposite directions.
    """
    delta, p_itm = _bs_delta_and_itm(319.92, 310.0, 0.2383, 12 / 365, "put")
    assert delta < p_itm
    assert p_itm - delta > 0.005


def test_the_delta_probability_gap_scales_with_sigma_root_t():
    """N(d1) - N(d2) grows with sigma*sqrt(T), so it is worst at long DTE."""
    gaps = []
    for dte in (2, 7, 14, 30):
        d, p = _bs_delta_and_itm(295.73, 300.0, 0.25, dte / 365, "call")
        gaps.append(d - p)
    assert gaps == sorted(gaps)


def test_screener_probability_is_n_d2_not_delta():
    """prob_below must be the N(d2)-shaped quantity, not the delta-shaped one."""
    spot, strike, vol, years = 319.92, 310.0, 0.2383, 12 / 365
    delta, p_itm = _bs_delta_and_itm(spot, strike, vol, years, "put")
    assert prob_below(spot, strike, vol, years) == pytest.approx(p_itm, abs=1e-12)
    assert prob_below(spot, strike, vol, years) != pytest.approx(delta, abs=1e-4)


# --------------------------------------------------------------------------- #
# the volatility-difference signal
# --------------------------------------------------------------------------- #

def _chain(iv):
    """A minimal IWM-shaped chain holding one valid 300/301 call spread."""
    common = dict(underlying="IWM", right="call", expiry=date(2026, 9, 11),
                  dte=12, open_interest=4836, gamma=0.0, theta=0.0, vega=0.0,
                  iv=iv)
    return [
        OptionRow(symbol="IWM260911C00300000", strike=300.0,
                  bid=0.49, ask=0.51, delta=0.3184, **common),
        OptionRow(symbol="IWM260911C00301000", strike=301.0,
                  bid=0.155, ask=0.175, delta=0.2718, **common),
    ]


def _snapshot(iv, realised):
    return MarketSnapshot(symbol="IWM", spot=295.74, realized_vol=realised,
                          sma20=293.0, rows=_chain(iv), feed="indicative",
                          asof=datetime(2026, 8, 30, 6, 2, 43))


def test_equal_implied_and_realised_gives_exactly_zero_signal():
    """The invariant the whole design rests on.

    Both valuations run the same function on the same credit and differ only
    in volatility, so equal volatilities must cancel to the last bit. An
    earlier build priced one side off delta and reported a premium of $2.75 on
    a trade where implied and realised agreed to within 2%.
    """
    [c] = _build_spreads(_snapshot(iv=0.147, realised=0.147), "call",
                         min_vrp=-1e9)
    assert c.vrp_edge == pytest.approx(0.0, abs=1e-9)
    assert c.ev == pytest.approx(c.ev_rn, abs=1e-9)


def test_signal_is_positive_only_when_implied_exceeds_realised():
    edges = [_build_spreads(_snapshot(iv=iv, realised=0.147), "call",
                            min_vrp=-1e9)[0].vrp_edge
             for iv in (0.147, 0.18, 0.25)]
    assert edges[0] == pytest.approx(0.0, abs=1e-9)
    assert edges == sorted(edges)
    assert edges[-1] > 1.0


def test_cheap_implied_vol_produces_a_negative_signal():
    [c] = _build_spreads(_snapshot(iv=0.10, realised=0.147), "call",
                         min_vrp=-1e9)
    assert c.vrp_edge < 0


def test_both_valuations_use_the_identical_credit():
    """Only volatility may differ between the two. If the credit differed, the
    difference would mix a pricing change into a volatility signal."""
    [c] = _build_spreads(_snapshot(iv=0.25, realised=0.147), "call",
                         min_vrp=-1e9)
    ev_rw, _ = spread_ev(295.74, 300.0, 301.0, c.credit, 0.147, 12 / 365, "call")
    ev_rn, _ = spread_ev(295.74, 300.0, 301.0, c.credit, 0.25, 12 / 365, "call")
    assert c.ev == pytest.approx(ev_rw, abs=0.01)
    assert c.ev_rn == pytest.approx(ev_rn, abs=0.01)


# --------------------------------------------------------------------------- #
# the gate and the ranking key
# --------------------------------------------------------------------------- #

def test_gate_rejects_a_spread_with_no_measurable_premium():
    assert _build_spreads(_snapshot(iv=0.147, realised=0.147), "call") == []


def test_gate_admits_a_spread_with_a_real_premium():
    [c] = _build_spreads(_snapshot(iv=0.25, realised=0.147), "call")
    assert c.vrp_edge >= MIN_VRP_EDGE


def test_score_rises_with_the_premium_when_nothing_else_changes():
    lo = _build_spreads(_snapshot(iv=0.20, realised=0.147), "call")[0]
    hi = _build_spreads(_snapshot(iv=0.28, realised=0.147), "call")[0]
    assert hi.vrp_edge > lo.vrp_edge
    assert hi.score > lo.score


def test_missing_implied_vol_makes_the_spread_untradable():
    """No IV means the signal cannot be computed, so there is nothing to rank."""
    assert _build_spreads(_snapshot(iv=None, realised=0.147), "call") == []


def test_candidate_max_loss_reconciles_with_width_and_credit():
    [c] = _build_spreads(_snapshot(iv=0.25, realised=0.147), "call")
    assert c.max_loss == pytest.approx(c.width * 100.0 - c.credit * 100.0, abs=0.01)
    assert c.max_profit == pytest.approx(c.credit * 100.0, abs=0.01)


def test_score_is_dimensionless_premium_per_dollar_risked():
    [c] = _build_spreads(_snapshot(iv=0.25, realised=0.147), "call")
    expected = ((c.vrp_edge / c.max_loss)
                * (1.0 - min(c.worst_spread_pct, 0.9)) * 0.80)  # width 1.0
    assert c.score == pytest.approx(expected, abs=1e-4)
