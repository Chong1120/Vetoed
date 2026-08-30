"""
screener.py - DETERMINISTIC candidate generation. No LLM in this file.

Turns raw option chains into a short list of concrete, tradable, defined-risk
vertical credit spreads. Given identical market data it produces identical
output, which makes it testable and the agent's behaviour auditable.

    screener.py  what is STRUCTURALLY VALID and tradable  (arithmetic)
    brain.py     what is ATTRACTIVE right now             (judgement)
    risk.py      what we are ALLOWED to take, and how big (hard gates)

WHY THE RANKING USES TWO PROBABILITY MEASURES
---------------------------------------------
Delta is the RISK-NEUTRAL probability of finishing in the money. Under
risk-neutral pricing every fairly-priced option trade has an expected value of
exactly zero - that is a no-arbitrage identity, not an opinion. So ranking
candidates by a delta-derived EV measures nothing except quote noise.

Option selling is profitable because risk-neutral probabilities systematically
OVERSTATE the real-world chance of large moves. That gap is the volatility
risk premium:

  - Bakshi & Kapadia (2003), Review of Financial Studies 16(2), 527-566:
    delta-hedged S&P 500 option portfolios underperform zero, and the
    underperformance is greater at higher volatility.
  - Carr & Wu (2009), Review of Financial Studies 22(3), 1311-1341:
    variance risk premiums quantified across 5 indices and 35 stocks.
  - CBOE PUT index (data from June 1986): 9.9% annualised volatility vs
    14.9% for the S&P 500, with a higher Sharpe ratio over 32.5 years.

So this module uses BOTH measures deliberately:

    credit received  <- the market quote  (risk-neutral, carries the premium)
    ev_rn            <- that credit priced at the market's IMPLIED volatility
    ev_rw            <- that credit priced at 20-day REALISED volatility

Both EVs run through ONE probability model and differ only in the volatility
fed into it. That is the whole point: `vrp_edge = ev_rw - ev_rn` then isolates
exactly the implied-minus-realised gap - the premium being harvested - and
correctly reads ~0 when implied and realised agree, i.e. when there is no
premium to harvest.

`vrp_edge` is therefore both the GATE and the RANKING KEY. Ranking on `ev_rw`
alone would rank on how low the 20-day realised-vol estimate happened to come
in - an estimation error - instead of on what the market is actually paying
us to carry the risk.
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

MIN_EV_RW = 1.00        # dollars per spread, real-world measure

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
    pop: float                # real-world probability of profit
    pop_rn: float             # risk-neutral (implied-vol), for comparison
    ev: float                 # real-world EV - what we rank on
    ev_rn: float              # same spread priced at implied vol
    vrp_edge: float           # ev - ev_rn: the premium being harvested
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


def prob_below(spot: float, strike: float, vol: float, years: float) -> float:
    """Real-world P(S_T < K) under driftless lognormal returns.

    The CALLER chooses the volatility. Pass realised vol for the real-world
    measure, implied vol for the risk-neutral one. Running both through this
    same function is what makes their difference mean something.
    """
    if spot <= 0 or strike <= 0 or vol <= 0 or years <= 0:
        return float("nan")
    d = (math.log(strike / spot) + 0.5 * vol * vol * years) / (vol * math.sqrt(years))
    return norm_cdf(d)


def _three_point_ev(p_short_itm: float, p_long_itm: float,
                    max_profit: float, max_loss: float) -> tuple[float, float]:
    """(EV, probability of profit) for a vertical credit spread.

        beyond neither strike -> keep the full credit   (+max_profit)
        between the strikes   -> payoff runs LINEARLY from +max_profit at the
                                 short strike to -max_loss at the long strike
        beyond both strikes   -> full max loss          (-max_loss)

    The middle band is the easy one to get wrong. The payoff there is NOT
    "about half the max loss" - at the short strike the spread still expires
    for the entire credit. Averaging the linear payoff across the band gives
    (max_profit - max_loss) / 2, which is what is used below.
    """
    p_win = 1.0 - p_short_itm
    p_partial = max(p_short_itm - p_long_itm, 0.0)
    p_maxloss = p_long_itm
    ev = (p_win * max_profit
          + p_partial * (max_profit - max_loss) * 0.5
          - p_maxloss * max_loss)
    return ev, p_win


# --------------------------------------------------------------------------- #
# leg-level filters
# --------------------------------------------------------------------------- #

def _spread_ok(row: OptionRow) -> bool:
    return row.spread <= MAX_SPREAD_ABS or row.spread_pct <= MAX_SPREAD_PCT


def _oi_floor(underlying: str) -> int:
    return MIN_OI.get(underlying, MIN_OI_DEFAULT)


def _short_leg_ok(row: OptionRow) -> bool:
    return (row.bid >= MIN_SHORT_BID
            and row.open_interest >= _oi_floor(row.underlying)
            and _spread_ok(row))


def _long_leg_ok(row: OptionRow) -> bool:
    return (row.bid >= MIN_LONG_BID
            and row.open_interest >= _oi_floor(row.underlying)
            and _spread_ok(row))


# --------------------------------------------------------------------------- #

def _build_spreads(snap: MarketSnapshot, right: str,
                   min_vrp: float = MIN_VRP_EDGE) -> list[SpreadCandidate]:
    """Pair every viable short leg with a protective long leg.

    `min_vrp` is the floor on measured volatility risk premium. Tests lower it
    to inspect the raw maths; production keeps the default.
    """
    spot, vol = snap.spot, snap.realized_vol
    shorts = [r for r in snap.rows if r.right == right and _short_leg_ok(r)]
    longs = {(str(r.expiry), r.strike): r
             for r in snap.rows if r.right == right and _long_leg_ok(r)}

    kind = "put_credit" if right == "put" else "call_credit"
    out: list[SpreadCandidate] = []

    for short in shorts:
        if short.delta is None:
            continue
        if not (SHORT_DELTA_MIN <= short.abs_delta <= SHORT_DELTA_MAX):
            continue
        if right == "put" and short.strike >= spot:
            continue
        if right == "call" and short.strike <= spot:
            continue

        for width in WIDTHS:
            long_strike = (short.strike - width if right == "put"
                           else short.strike + width)
            long_leg = longs.get((str(short.expiry), long_strike))
            if long_leg is None or long_leg.delta is None:
                continue

            credit = short.mid - long_leg.mid
            if credit <= 0:
                continue
            ratio = credit / width
            if not (MIN_CREDIT_RATIO <= ratio <= MAX_CREDIT_RATIO):
                continue

            max_profit = credit * 100.0
            max_loss = width * 100.0 - max_profit
            if max_loss <= 0:
                continue

            years = max(short.dte, 1) / 365.0

            def _probs(v: float) -> tuple[float, float]:
                """(P short breached, P long breached) at volatility `v`."""
                ps = prob_below(spot, short.strike, v, years)
                pl = prob_below(spot, long_strike, v, years)
                return (ps, pl) if right == "put" else (1.0 - ps, 1.0 - pl)

            # --- real-world view: 20-day realised volatility -------------- #
            p_short, p_long = _probs(vol)
            if math.isnan(p_short) or math.isnan(p_long):
                continue
            ev_rw, pop_rw = _three_point_ev(p_short, p_long,
                                            max_profit, max_loss)
            if ev_rw < MIN_EV_RW:
                continue

            # --- risk-neutral view: the market's own implied volatility --- #
            # NOT delta. Delta is N(d1); the probability of finishing in the
            # money is N(d2). They differ by ~sigma*sqrt(T), which at 14 DTE is
            # worth several dollars of EV - more than MIN_EV_RW itself. Pricing
            # ev_rn off delta while ev_rw came from a lognormal made vrp_edge
            # report a premium even when implied == realised, i.e. when there
            # was demonstrably no premium. Same model both sides, vol is the
            # only thing that changes.
            iv = short.iv if (short.iv is not None and short.iv > 0) else None
            if iv is None:
                continue     # no IV -> the edge is unmeasurable -> not tradable
            p_short_rn, p_long_rn = _probs(iv)
            if math.isnan(p_short_rn) or math.isnan(p_long_rn):
                continue
            ev_rn, pop_rn = _three_point_ev(p_short_rn, p_long_rn,
                                            max_profit, max_loss)

            # The gate. We are here to harvest a premium; if the market is not
            # paying one, a high real-world EV only means the realised-vol
            # estimate came in low - an estimation error we would be ranking
            # on, rather than an edge we are being paid for.
            vrp_edge = ev_rw - ev_rn
            if vrp_edge < min_vrp:
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
                    min_vrp: float = MIN_VRP_EDGE) -> list[SpreadCandidate]:
    """Valid credit spreads for one underlying, with a one-sided trend filter.

    Selling PUT spreads into a market already below its 20-day average is the
    easiest way to turn a high-probability trade into a max loss: the short
    strike is approached by the very trend you ignored. The reverse applies to
    call spreads in an uptrend. Neither side is blocked outright - only the
    side the trend is running against.
    """
    cands: list[SpreadCandidate] = []
    if snap.above_trend:
        cands += _build_spreads(snap, "put", min_vrp)
    if not snap.above_trend or not snap.sma20:
        cands += _build_spreads(snap, "call", min_vrp)
    if not cands and not snap.sma20:
        cands = (_build_spreads(snap, "put", min_vrp)
                 + _build_spreads(snap, "call", min_vrp))
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

    for symbol in universe:
        try:
            snap = market.snapshot(symbol, dte_min, dte_max)
        except Exception as exc:
            context["underlyings"][symbol] = {"error": "%s: %s"
                                              % (type(exc).__name__, exc)}
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
        all_cands.extend(screen_snapshot(snap))

    # An adaptive guardrail may narrow the delta band; applied post-build so
    # the band lives in one place.
    if "delta_min" in ov or "delta_max" in ov:
        lo = float(ov.get("delta_min", SHORT_DELTA_MIN))
        hi = float(ov.get("delta_max", SHORT_DELTA_MAX))
        all_cands = [c for c in all_cands if lo <= abs(c.short_delta) <= hi]
    for banned in ov.get("banned_underlyings", []):
        all_cands = [c for c in all_cands if c.underlying != banned]

    shortlist = balanced_top(dedupe_best(all_cands), limit)
    context["candidates_before_dedupe"] = len(all_cands)
    context["candidates_returned"] = len(shortlist)
    return shortlist, context
