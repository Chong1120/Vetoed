"""
screener.py - DETERMINISTIC candidate generation. No LLM in this file.

Turns raw option chains into a short list of concrete, tradable, defined-risk
vertical credit spreads. Given identical market data it produces identical
output, which makes it testable and the agent's behaviour auditable.

    screener.py  what is STRUCTURALLY VALID and tradable  (arithmetic)
    brain.py     what is ATTRACTIVE right now             (judgement)
    risk.py      what we are ALLOWED to take, and how big (hard gates)

WHAT THIS MODULE MEASURES, STATED CAREFULLY
-------------------------------------------
Everything below is computed under ONE model: a zero-drift lognormal for the
terminal price, with E[S_T] = S_0. See `spread_ev` for the exact statement.
Volatility is the only input that ever changes between the two valuations:

    ev_rn   the spread's expected payoff when the model is fed the SHORT LEG'S
            IMPLIED volatility. An approximation to how the market itself
            values this spread.
    ev_rw   the same spread, same credit, fed 20-day REALISED volatility. A
            counterfactual: what it would be worth if volatility to expiry
            matched what the underlying has recently done.

    vrp_edge = ev_rw - ev_rn

`vrp_edge` is this project's OPERATIONAL SIGNAL, in dollars per spread. It is
the P&L attributable purely to the implied-minus-realised volatility gap under
this model. Because both sides share a model, a credit, and a drift, the
difference isolates the volatility gap and is exactly zero when the two
volatilities agree - which is pinned by a test.

WHAT IT IS NOT. It is not the variance risk premium of Carr & Wu (2009), which
is defined on variance swap rates over a matched horizon. Ours is a
spread-level dollar figure from a single quote snapshot under a simplified
model. The academic literature motivates WHY such a gap should persist; it
does not certify this particular estimator. Treat `vrp_edge` as a screening
signal with an economic rationale, not as a measured risk premium.

WHY NOT RANK ON DELTA
---------------------
Delta is a risk-neutral sensitivity, N(d1) for a call. The risk-neutral
probability of finishing in the money is N(d2) = N(d1 - sigma.sqrt(T)). They
are close but not equal, and the gap has OPPOSITE SIGN for calls and puts, so
substituting one for the other biases the two sides of the book in opposite
directions.

Separately, and more fundamentally: under the risk-neutral measure a
fairly-priced trade has zero expected P&L by construction. So any EV computed
purely from risk-neutral inputs carries no edge information - it can only
reflect quoting noise and model error. That is why the signal is a DIFFERENCE
between two volatility parameterisations rather than a level.

Why a gap should exist at all - the economic prior, not proof of this system:
  - Bakshi & Kapadia (2003), RFS 16(2), 527-566: delta-hedged S&P 500 option
    portfolios earn less than zero on average, consistent with a negative
    market volatility risk premium.
  - Carr & Wu (2009), RFS 22(3), 1311-1341: variance risk premiums measured
    across 5 indices and 35 stocks.
  - CBOE PUT index (Jun 1986-Dec 2018): systematic ONE-MONTH AT-THE-MONEY
    CASH-SECURED put writing returned 9.54% at 9.95% volatility versus 9.80%
    at 14.93% for the S&P 500. Different instrument and horizon from what this
    module trades, so it is supporting context, not validation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Literal

from agent.data import MarketSnapshot, OptionRow, atm_iv

# --------------------------------------------------------------------------- #
# screening parameters
# --------------------------------------------------------------------------- #

UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL"]

DTE_MIN = 2          # 1DTE gamma is brutal; 2 is the floor
DTE_MAX = 14         # beyond this, too little decays inside the contest window

# Liquidity floors per underlying. ETFs carry far deeper books than single
# names, so a flat threshold would delete every AAPL candidate.
# NOTE: Alpaca's option snapshot exposes NO daily volume - only open interest
# is available, and fetching daily bars for ~2,400 contracts per cycle is not
# practical. OI alone is therefore the liquidity proxy here.
MIN_OI = {"SPY": 500, "QQQ": 500, "IWM": 250, "AAPL": 100}
MIN_OI_DEFAULT = 250

# The SHORT leg is what we sell - it must pay real premium.
# The LONG leg is insurance we buy - we WANT it cheap. Different thresholds.
MIN_SHORT_BID = 0.10
MIN_LONG_BID = 0.02

# A percentage-only spread test unfairly kills cheap options: 0.05/0.07 is
# "33% wide" but costs two cents. Pass on either test.
MAX_SPREAD_ABS = 0.05
MAX_SPREAD_PCT = 0.10

SHORT_DELTA_MIN = 0.10
SHORT_DELTA_MAX = 0.35   # Bakshi & Kapadia: premium is richer nearer the money

WIDTHS = [1.0, 2.0, 5.0]
WIDTH_PREFERENCE = {1.0: 0.80, 2.0: 1.00, 5.0: 1.00}  # $1 wide pays too little
                                                       # against double bid-ask

MIN_CREDIT_RATIO = 0.12
MAX_CREDIT_RATIO = 0.60

MIN_EV_RW = 1.00        # dollars per spread, priced at realised vol

# The premium is the entire thesis, so it is a GATE and the ranking key - not
# a diagnostic printed beside a ranking that ignores it. Below ~$2 the measured
# edge sits inside the noise of a 20-day realised-vol estimate (~16% relative
# standard error), so it is not evidence of anything.
MIN_VRP_EDGE = 2.00

MAX_CANDIDATES = 8


# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpreadCandidate:
    """One fully-specified, defined-risk vertical credit spread."""

    underlying: str
    kind: Literal["put_credit", "call_credit"]
    expiry: str
    dte: int
    short_symbol: str
    long_symbol: str
    short_strike: float
    long_strike: float
    width: float
    credit: float
    max_loss: float
    max_profit: float
    credit_ratio: float
    short_delta: float
    long_delta: float
    pop: float                # model P(short strike survives) at realised vol
    pop_rn: float             # same, at implied vol - for comparison only
    ev: float                 # ev_rw: expected payoff priced at realised vol
    ev_rn: float              # expected payoff priced at implied vol
    vrp_edge: float           # ev_rw - ev_rn: the volatility-gap signal, $
    short_iv: float | None
    realized_vol: float
    min_open_interest: int
    worst_spread_pct: float
    distance_pct: float
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# probability helpers
# --------------------------------------------------------------------------- #

def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d(spot: float, strike: float, vol: float, years: float) -> float:
    """The exponent argument shared by every formula below."""
    return ((math.log(strike / spot) + 0.5 * vol * vol * years)
            / (vol * math.sqrt(years)))


def _inputs_ok(spot: float, strike: float, vol: float, years: float) -> bool:
    return spot > 0 and strike > 0 and vol > 0 and years > 0


def prob_below(spot: float, strike: float, vol: float, years: float) -> float:
    """P(S_T < K) under the model described in `spread_ev`.

    NOT a real-world probability. It is the probability under a zero-drift
    lognormal whose volatility the caller supplies, which is a modelling
    choice, not an observation. See `spread_ev` for what that does and does
    not entitle us to claim.
    """
    if not _inputs_ok(spot, strike, vol, years):
        return float("nan")
    return norm_cdf(_d(spot, strike, vol, years))


def spread_ev(spot: float, k_short: float, k_long: float, credit: float,
              vol: float, years: float, right: str) -> tuple[float, float]:
    """EXACT expected payoff and probability of profit for a vertical credit
    spread, in dollars per spread.

    THE MODEL, STATED PRECISELY. Terminal price is lognormal with zero drift:

        ln S_T ~ Normal( ln S_0 - sigma^2 T / 2 ,  sigma^2 T )   =>  E[S_T] = S_0

    Zero drift means the forward equals spot: no interest, no dividends, and
    crucially NO equity risk premium. We are not claiming the stock has zero
    expected return - we are declining to take a directional view, which for a
    premium seller is the conservative choice on the put side.

    Because the drift is fixed, the ONLY thing that changes between the two
    valuations this module computes is `vol`. That is what makes their
    difference interpretable (see `vrp_edge` in the module docstring).

    EXACT, NOT APPROXIMATE. The payoff has three regions - full credit beyond
    the short strike, a linear ramp between the strikes, and full max loss
    beyond the long strike. The ramp is integrated in closed form using

        E[S_T . 1{S_T < K}] = S_0 . N(d(K) - sigma.sqrt(T))

    so no quadrature and no midpoint rule is involved. An earlier version
    evaluated the ramp at the midpoint of the strike band, which is only right
    when E[S_T | in the band] lands exactly on that midpoint. Under a lognormal
    it does not, and the resulting error reached tens of dollars on wide
    spreads - larger than MIN_VRP_EDGE, so it could flip the trade/no-trade
    decision. `tests/test_screener_math.py` pins this against a numerically
    integrated reference.

    Returns (ev, probability of profit). `pop` is P(short strike not breached
    at expiry) - a hold-to-expiry number, unrelated to the probability of the
    take-profit exit firing first.
    """
    if not _inputs_ok(spot, k_short, vol, years) or k_long <= 0:
        return float("nan"), float("nan")

    width = abs(k_short - k_long)
    max_profit = credit * 100.0
    max_loss = width * 100.0 - max_profit

    sd = vol * math.sqrt(years)
    d_s, d_l = _d(spot, k_short, vol, years), _d(spot, k_long, vol, years)

    if right == "put":
        # Band is k_long < S < k_short; payoff there is 100*(credit-(k_short-S))
        lo_d, hi_d = d_l, d_s
        p_band = norm_cdf(hi_d) - norm_cdf(lo_d)
        e_s_band = spot * (norm_cdf(hi_d - sd) - norm_cdf(lo_d - sd))
        ramp = 100.0 * ((credit - k_short) * p_band + e_s_band)
        ev = max_profit * (1.0 - norm_cdf(d_s)) + ramp - max_loss * norm_cdf(d_l)
        pop = 1.0 - norm_cdf(d_s)
    else:
        # Band is k_short < S < k_long; payoff there is 100*(credit-(S-k_short))
        lo_d, hi_d = d_s, d_l
        p_band = norm_cdf(hi_d) - norm_cdf(lo_d)
        e_s_band = spot * (norm_cdf(hi_d - sd) - norm_cdf(lo_d - sd))
        ramp = 100.0 * ((credit + k_short) * p_band - e_s_band)
        ev = max_profit * norm_cdf(d_s) + ramp - max_loss * (1.0 - norm_cdf(d_l))
        pop = norm_cdf(d_s)

    return ev, pop


# --------------------------------------------------------------------------- #
# rejection recording
# --------------------------------------------------------------------------- #

class Rejections:
    """Why each candidate was turned down, counted as the screener works.

    The decision log could only ever show one reason a trade did not happen -
    CONCENTRATION, the position limit - because that is the only veto raised
    after a candidate has been nominated. Everything the screener threw out
    beforehand was discarded silently, so the agent read as one that always
    wants to trade and is only ever held back by a cap. The opposite is true:
    on a normal cycle it measures a few hundred spreads and rejects almost all
    of them on quality, and that is the part worth seeing.

    Recording is entirely passive. Nothing here participates in the decision -
    the collector is optional, defaults to absent, and when absent the screener
    runs exactly as it did before.
    """

    __slots__ = ("tally", "near", "examples", "measured")

    # Ordered worst-to-best so the log reads as a funnel.
    LABELS = {
        "spread_too_wide": "bid-ask spread too wide to trade",
        "oi_too_thin": "open interest below the liquidity floor",
        "premium_too_small": "premium too small to be worth the risk",
        "delta_out_of_band": "short strike delta outside the target band",
        "wrong_side_of_spot": "strike on the wrong side of the price",
        "no_long_leg": "no protective long leg available at any width",
        "credit_ratio": "credit versus width outside the accepted band",
        "ev_too_low": "expected value below the $%.2f floor" % MIN_EV_RW,
        "no_implied_vol": "no implied volatility quoted - edge unmeasurable",
        "edge_too_low": "measured edge below the $%.2f minimum" % MIN_VRP_EDGE,
        # Raised by adapt.py after a loss, so the screener tightens itself.
        "guardrail_delta": "delta band narrowed by an adaptive guardrail",
        "guardrail_banned": "underlying suspended by an adaptive guardrail",
    }

    EXAMPLES_PER_REASON = 3

    def __init__(self):
        self.tally: dict[str, int] = {}
        self.near: list[dict] = []
        self.examples: dict[str, list[str]] = {}
        self.measured = 0

    def add(self, reason: str, detail: dict | None = None,
            example: str | None = None):
        self.tally[reason] = self.tally.get(reason, 0) + 1
        if detail:
            self.near.append(dict(detail, reason=reason))
        # "1,029 rejected on open interest" is a statistic. "SPY 645 PUT -
        # open interest 87, floor 500" is the agent showing its work. A few
        # per reason is enough to make the count concrete, and capping it
        # keeps a cycle's tally small enough to commit every ten minutes.
        if example:
            got = self.examples.setdefault(reason, [])
            if len(got) < self.EXAMPLES_PER_REASON and example not in got:
                got.append(example)

    def to_dict(self) -> dict:
        """Counts plus the handful of spreads that came closest to passing.

        A bare tally says the agent rejected 312 spreads and shows none of
        them, which is not much more convincing than showing nothing. The
        near-misses are the ones that were fully measured and still declined,
        so they carry the actual numbers - this edge, against this floor.
        """
        near = sorted((n for n in self.near if n.get("vrp_edge") is not None),
                      key=lambda n: -n["vrp_edge"])[:6]
        return {
            "measured": self.measured,
            "rejected": sum(self.tally.values()),
            "by_reason": [
                {"reason": k, "label": self.LABELS.get(k, k), "count": v,
                 "examples": self.examples.get(k, [])}
                for k, v in sorted(self.tally.items(), key=lambda kv: -kv[1])
            ],
            "near_misses": near,
        }


# --------------------------------------------------------------------------- #
# leg-level filters
# --------------------------------------------------------------------------- #

def _spread_ok(row: OptionRow) -> bool:
    """Tradeable if the spread is narrow in cash OR narrow relative to price.

    Rounded before comparing, because a five-cent spread is not reliably five
    cents in binary. 0.34 - 0.29 lands on 0.050000000000000044 and failed the
    cap, while 0.41 - 0.36 lands on 0.049999999999999990 and passed it - the
    same five cents, admitted or refused on nothing but which way the float
    happened to fall. Eight of 2,724 contracts in one live screen were being
    turned away by that. Rounding to six places is far finer than any real
    quote increment, so it changes nothing except the noise.
    """
    return (round(row.spread, 6) <= MAX_SPREAD_ABS
            or row.spread_pct <= MAX_SPREAD_PCT)


def _oi_floor(underlying: str) -> int:
    """Never nominate a spread the risk gate is certain to reject.

    These per-underlying floors exist to be STRICTER than the gate where a
    name is deep enough to demand it. AAPL's 100 was looser, so every AAPL
    strike with open interest between 100 and 249 was shortlisted, ranked,
    frequently chosen - and then vetoed on liquidity every single time. Two
    live cycles were lost to exactly that before it was caught.

    Taking the max of the two keeps the stricter ETF floors and makes the
    divergence unrepresentable: the screener can be tighter than the gate,
    never looser. Importing the gate's own constant means raising it later
    tightens the screener with it, instead of silently reopening this hole.
    """
    from agent.risk import MIN_OPEN_INTEREST
    return max(MIN_OI.get(underlying, MIN_OI_DEFAULT), MIN_OPEN_INTEREST)


def _name(row: OptionRow) -> str:
    """"SPY 645 PUT 4DTE" - what a reader can actually look up."""
    strike = ("%g" % row.strike)
    return "%s %s %s %dDTE" % (row.underlying, strike, row.right.upper(), row.dte)


def _spread_name(short: OptionRow, long_strike: float) -> str:
    """"SPY 771/776 CALL 2DTE" - both legs, the way a spread is quoted."""
    return "%s %g/%g %s %dDTE" % (short.underlying, short.strike, long_strike,
                                  short.right.upper(), short.dte)


def _leg_example(row: OptionRow, reason: str, min_bid: float) -> str:
    """The measurement that failed, next to the floor it failed against."""
    if reason == "premium_too_small":
        return "%s - bid $%.2f, floor $%.2f" % (_name(row), row.bid, min_bid)
    if reason == "oi_too_thin":
        return "%s - open interest %d, floor %d" % (
            _name(row), row.open_interest, _oi_floor(row.underlying))
    # Both halves of the gate, because it passes on EITHER. Shown to one
    # decimal: 10.4%% rounded to "10%%" against a "10%%" cap read as though it
    # should have been allowed through.
    return "%s - bid-ask $%.3f (%.1f%% of mid), needs under $%.3f or %.0f%%" % (
        _name(row), row.spread, row.spread_pct * 100.0,
        MAX_SPREAD_ABS, MAX_SPREAD_PCT * 100.0)


def _leg_reason(row: OptionRow, min_bid: float) -> str | None:
    """Why this leg is untradeable, or None if it is fine.

    Mirrors the boolean gates below exactly; it exists so a rejection can be
    named rather than merely counted. First failure wins, which keeps one
    unusable leg from inflating three separate tallies.
    """
    if row.bid < min_bid:
        return "premium_too_small"
    if row.open_interest < _oi_floor(row.underlying):
        return "oi_too_thin"
    if not _spread_ok(row):
        return "spread_too_wide"
    return None


def _short_leg_ok(row: OptionRow) -> bool:
    return (row.bid >= MIN_SHORT_BID
            and row.open_interest >= _oi_floor(row.underlying)
            and _spread_ok(row))


def _long_leg_ok(row: OptionRow) -> bool:
    return (row.bid >= MIN_LONG_BID
            and row.open_interest >= _oi_floor(row.underlying)
            and _spread_ok(row))


# --------------------------------------------------------------------------- #

def _note(rejects: "Rejections | None", reason: str, detail: dict | None = None,
          example: str | None = None):
    """Record a rejection if anyone is collecting them."""
    if rejects is not None:
        rejects.add(reason, detail, example)


def _build_spreads(snap: MarketSnapshot, right: str,
                   min_vrp: float = MIN_VRP_EDGE,
                   rejects: "Rejections | None" = None) -> list[SpreadCandidate]:
    """Pair every viable short leg with a protective long leg.

    `min_vrp` is the floor on measured volatility risk premium. Tests lower it
    to inspect the raw maths; production keeps the default.
    """
    spot, vol = snap.spot, snap.realized_vol
    rows = [r for r in snap.rows if r.right == right]
    shorts = []
    for r in rows:
        reason = _leg_reason(r, MIN_SHORT_BID)
        if reason is None:
            shorts.append(r)
        elif rejects is not None:
            rejects.add(reason, example=_leg_example(r, reason, MIN_SHORT_BID))
    longs = {(str(r.expiry), r.strike): r for r in rows if _long_leg_ok(r)}

    kind = "put_credit" if right == "put" else "call_credit"
    out: list[SpreadCandidate] = []

    for short in shorts:
        if short.delta is None:
            continue
        if not (SHORT_DELTA_MIN <= short.abs_delta <= SHORT_DELTA_MAX):
            _note(rejects, "delta_out_of_band", example="%s - delta %.2f, band %.2f-%.2f"
                  % (_name(short), short.abs_delta, SHORT_DELTA_MIN, SHORT_DELTA_MAX))
            continue
        if right == "put" and short.strike >= spot:
            _note(rejects, "wrong_side_of_spot")
            continue
        if right == "call" and short.strike <= spot:
            _note(rejects, "wrong_side_of_spot")
            continue

        for width in WIDTHS:
            long_strike = (short.strike - width if right == "put"
                           else short.strike + width)
            long_leg = longs.get((str(short.expiry), long_strike))
            if long_leg is None or long_leg.delta is None:
                _note(rejects, "no_long_leg")
                continue

            credit = short.mid - long_leg.mid
            if credit <= 0:
                continue
            ratio = credit / width
            if not (MIN_CREDIT_RATIO <= ratio <= MAX_CREDIT_RATIO):
                _note(rejects, "credit_ratio",
                      example="%s - credit %.0f%% of width, band %.0f-%.0f%%"
                      % (_spread_name(short, long_strike), ratio * 100.0,
                         MIN_CREDIT_RATIO * 100.0, MAX_CREDIT_RATIO * 100.0))
                continue

            max_profit = credit * 100.0
            max_loss = width * 100.0 - max_profit
            if max_loss <= 0:
                continue

            # Calendar-day year fraction. Implied vol is quoted on this
            # convention, so using it keeps the two valuations below on the
            # same clock. It is not a trading-day count and is not claimed to
            # be one.
            years = max(short.dte, 1) / 365.0

            # --- valuation A: priced at 20-day REALISED volatility -------- #
            # A counterfactual: what this spread would be worth if volatility
            # to expiry matched what the underlying has recently done. `vol`
            # is an estimator from 20 daily returns, not a forecast.
            ev_rw, pop_rw = spread_ev(spot, short.strike, long_strike,
                                      credit, vol, years, right)
            if math.isnan(ev_rw) or ev_rw < MIN_EV_RW:
                _note(rejects, "ev_too_low",
                      example="%s - expected value $%.2f, floor $%.2f"
                      % (_spread_name(short, long_strike),
                         0.0 if math.isnan(ev_rw) else ev_rw, MIN_EV_RW))
                continue

            # --- valuation B: priced at the market's IMPLIED volatility --- #
            # Deliberately NOT delta. Delta is N(d1); the probability of
            # finishing in the money is N(d2), and the two differ by roughly
            # sigma*sqrt(T). Pricing this side off delta while the other side
            # came from a lognormal made the difference report a premium even
            # when implied and realised vol were equal - a premium that could
            # not exist. Same model both sides; volatility is the only input
            # that changes.
            #
            # Uses the SHORT leg's implied vol for both legs, so vertical skew
            # between the strikes is not modelled. That is a stated
            # simplification, and it is why ev_rn does not sit at zero.
            iv = short.iv if (short.iv is not None and short.iv > 0) else None
            if iv is None:
                _note(rejects, "no_implied_vol")
                continue     # edge is unmeasurable, so the spread is not taken
            ev_rn, pop_rn = spread_ev(spot, short.strike, long_strike,
                                      credit, iv, years, right)
            if math.isnan(ev_rn):
                continue

            # The gate. We are here to be paid for carrying volatility risk.
            # If the market is not paying, a high ev_rw only means the realised
            # -vol estimate happened to come in low, which is estimation error
            # rather than compensation.
            vrp_edge = ev_rw - ev_rn
            if rejects is not None:
                rejects.measured += 1
            if vrp_edge < min_vrp:
                # Fully measured and still declined - the numbers are worth
                # keeping, because this is the gate that does the real work.
                _note(rejects, "edge_too_low", example=(
                    "%s - edge $%.2f, floor $%.2f"
                    % (_spread_name(short, long_strike), vrp_edge, min_vrp)), detail={
                    "underlying": snap.symbol, "kind": kind,
                    "short_strike": short.strike, "long_strike": long_strike,
                    "dte": short.dte, "vrp_edge": round(vrp_edge, 2),
                    "required": round(min_vrp, 2),
                    "pop": round(pop_rw, 4) if not math.isnan(pop_rw) else None,
                })
                continue

            worst_spread = max(short.spread_pct, long_leg.spread_pct)
            min_oi = min(short.open_interest, long_leg.open_interest)
            distance_pct = abs(short.strike - spot) / spot

            score = ((vrp_edge / max_loss)
                     * (1.0 - min(worst_spread, 0.9))
                     * WIDTH_PREFERENCE.get(width, 1.0))

            out.append(SpreadCandidate(
                underlying=snap.symbol,
                kind=kind,
                expiry=str(short.expiry),
                dte=short.dte,
                short_symbol=short.symbol,
                long_symbol=long_leg.symbol,
                short_strike=short.strike,
                long_strike=long_strike,
                width=width,
                credit=round(credit, 4),
                max_loss=round(max_loss, 2),
                max_profit=round(max_profit, 2),
                credit_ratio=round(ratio, 4),
                short_delta=round(short.delta, 4),
                long_delta=round(long_leg.delta, 4),
                pop=round(pop_rw, 4),
                pop_rn=round(pop_rn, 4),
                ev=round(ev_rw, 2),
                ev_rn=round(ev_rn, 2),
                vrp_edge=round(vrp_edge, 2),
                short_iv=round(short.iv, 4) if short.iv is not None else None,
                realized_vol=round(vol, 4) if vol == vol else 0.0,
                min_open_interest=min_oi,
                worst_spread_pct=round(worst_spread, 4),
                distance_pct=round(distance_pct, 4),
                score=round(score, 4),
            ))
    return out


# --------------------------------------------------------------------------- #

def screen_snapshot(snap: MarketSnapshot,
                    min_vrp: float = MIN_VRP_EDGE,
                    rejects: "Rejections | None" = None) -> list[SpreadCandidate]:
    """Valid credit spreads for one underlying, with a one-sided trend filter.

    Selling PUT spreads into a market already below its 20-day average is the
    easiest way to turn a high-probability trade into a max loss: the short
    strike is approached by the very trend you ignored. The reverse applies to
    call spreads in an uptrend. Neither side is blocked outright - only the
    side the trend is running against.
    """
    cands: list[SpreadCandidate] = []
    if snap.above_trend:
        cands += _build_spreads(snap, "put", min_vrp, rejects)
    if not snap.above_trend or not snap.sma20:
        cands += _build_spreads(snap, "call", min_vrp, rejects)
    if not cands and not snap.sma20:
        cands = (_build_spreads(snap, "put", min_vrp, rejects)
                 + _build_spreads(snap, "call", min_vrp, rejects))
    return cands


def dedupe_best(cands: list[SpreadCandidate]) -> list[SpreadCandidate]:
    """Best width per (underlying, expiry, kind, short strike)."""
    best: dict[tuple, SpreadCandidate] = {}
    for c in cands:
        key = (c.underlying, c.expiry, c.kind, c.short_strike)
        cur = best.get(key)
        if cur is None or c.score > cur.score:
            best[key] = c
    return list(best.values())


def balanced_top(cands: list[SpreadCandidate], limit: int) -> list[SpreadCandidate]:
    """Top-N spread across (underlying, direction) buckets.

    A pure top-N can return eight variations of one directional bet - a
    concentration risk dressed up as a shortlist, leaving the brain nothing
    to actually choose between.
    """
    buckets: dict[tuple, list[SpreadCandidate]] = {}
    for c in cands:
        buckets.setdefault((c.underlying, c.kind), []).append(c)
    for b in buckets.values():
        b.sort(key=lambda c: c.score, reverse=True)

    out: list[SpreadCandidate] = []
    keys = sorted(buckets, key=lambda k: buckets[k][0].score, reverse=True)
    i = 0
    while len(out) < limit and any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            out.append(buckets[k].pop(0))
        i += 1
    return sorted(out, key=lambda c: c.score, reverse=True)


def screen(market, universe: list[str] | None = None,
           limit: int = MAX_CANDIDATES,
           overrides: dict | None = None) -> tuple[list[SpreadCandidate], dict]:
    """Full deterministic screen. `overrides` come from adapt.py guardrails."""
    universe = universe or UNIVERSE
    ov = overrides or {}
    dte_min = int(ov.get("dte_min", DTE_MIN))
    dte_max = int(ov.get("dte_max", DTE_MAX))

    all_cands: list[SpreadCandidate] = []
    context: dict = {"underlyings": {}, "feed": None, "overrides": ov}
    rejects = Rejections()

    for symbol in universe:
        try:
            snap = market.snapshot(symbol, dte_min, dte_max)
        except Exception as exc:
            # Type name and message are kept apart on purpose. brain.py
            # forwards only the type to the model, because the message can
            # carry text an external service chose.
            context["underlyings"][symbol] = {
                "error": type(exc).__name__,
                "error_detail": str(exc)[:300],
            }
            continue
        context["feed"] = snap.feed
        iv = atm_iv(snap.rows, snap.spot)
        context["underlyings"][symbol] = {
            "spot": round(snap.spot, 2),
            "sma20": round(snap.sma20, 2),
            "above_trend": snap.above_trend,
            "atm_iv": round(iv, 4) if iv else None,
            "realized_vol_20d": round(snap.realized_vol, 4),
            # IV richer than realised = the premium seller is being paid.
            # Stands in for IV rank, which needs IV history Alpaca lacks.
            "iv_vs_rv": round(iv / snap.realized_vol, 3)
            if iv and snap.realized_vol and snap.realized_vol > 0 else None,
            "contracts_examined": len(snap.rows),
        }
        all_cands.extend(screen_snapshot(snap, rejects=rejects))

    # An adaptive guardrail may narrow the delta band; applied post-build so
    # the band lives in one place.
    if "delta_min" in ov or "delta_max" in ov:
        lo = float(ov.get("delta_min", SHORT_DELTA_MIN))
        hi = float(ov.get("delta_max", SHORT_DELTA_MAX))
        kept = [c for c in all_cands if lo <= abs(c.short_delta) <= hi]
        for _ in range(len(all_cands) - len(kept)):
            rejects.add("guardrail_delta")
        all_cands = kept
    for banned in ov.get("banned_underlyings", []):
        kept = [c for c in all_cands if c.underlying != banned]
        for _ in range(len(all_cands) - len(kept)):
            rejects.add("guardrail_banned")
        all_cands = kept

    shortlist = balanced_top(dedupe_best(all_cands), limit)
    context["candidates_before_dedupe"] = len(all_cands)
    context["candidates_returned"] = len(shortlist)
    context["eliminated"] = rejects.to_dict()
    return shortlist, context
