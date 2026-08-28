"""
screener.py - DETERMINISTIC candidate generation. No LLM in this file.

Turns a raw option chain into a short list of concrete, tradable, defined-risk
vertical credit spreads. Given identical market data this produces identical
output, which makes it testable and makes the agent's behaviour auditable.

The division of labour matters:
  screener.py  decides what is STRUCTURALLY VALID and tradable  (arithmetic)
  brain.py     decides which of those is ATTRACTIVE right now   (judgement)
  risk.py      decides whether we are ALLOWED to take it        (hard gates)

A vertical credit spread, concretely (put side, "bull put spread"):
    SELL a put at the higher strike   <- collects premium
    BUY  a put at a lower strike      <- caps the loss, never naked
    net credit received up front; keep it if price stays above the short strike
    max loss = (width x 100) - credit ... and not one cent more
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

from agent.data import MarketSnapshot, OptionRow, atm_iv

# --------------------------------------------------------------------------- #
# screening parameters - all deterministic, all tunable in one place
# --------------------------------------------------------------------------- #

UNIVERSE = ["SPY", "QQQ"]

DTE_MIN = 1          # avoid same-day 0DTE for the first live sessions
DTE_MAX = 7

MIN_OPEN_INTEREST = 250     # both legs must be genuinely traded
MAX_SPREAD_PCT = 0.20       # bid-ask <= 20% of mid, per leg
MIN_LEG_BID = 0.02          # a leg with no bid cannot be exited

SHORT_DELTA_MIN = 0.10      # short leg sits out of the money...
SHORT_DELTA_MAX = 0.35      # ...but still collects a worthwhile premium

WIDTHS = [1.0, 2.0, 5.0]    # candidate distances between the two strikes
MIN_CREDIT_RATIO = 0.12     # credit must be >= 12% of width, else risk/reward
                            # is not worth the gamma exposure
MAX_CREDIT_RATIO = 0.60     # >60% of width usually means a stale/absurd quote

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
    credit: float             # per share, mid-based estimate
    max_loss: float           # dollars per 1 spread, after credit
    max_profit: float         # dollars per 1 spread
    credit_ratio: float       # credit / width
    short_delta: float
    long_delta: float
    pop: float                # probability of profit, from delta
    ev: float                 # expected value in dollars, per 1 spread
    short_iv: float | None
    min_open_interest: int
    worst_spread_pct: float
    distance_pct: float       # how far OTM the short strike sits, vs spot
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# leg-level liquidity filter
# --------------------------------------------------------------------------- #

def _leg_is_tradable(row: OptionRow) -> bool:
    if row.bid < MIN_LEG_BID:
        return False
    if row.open_interest < MIN_OPEN_INTEREST:
        return False
    if row.spread_pct > MAX_SPREAD_PCT:
        return False
    return True


def _build_spreads(rows: list[OptionRow], spot: float, right: str,
                   underlying: str) -> list[SpreadCandidate]:
    """Pair every viable short leg with a protective long leg."""
    legs = [r for r in rows if r.right == right and _leg_is_tradable(r)]
    by_strike: dict[tuple[str, float], OptionRow] = {
        (str(r.expiry), r.strike): r for r in legs
    }

    kind = "put_credit" if right == "put" else "call_credit"
    out: list[SpreadCandidate] = []

    for short in legs:
        if short.delta is None:
            continue
        if not (SHORT_DELTA_MIN <= short.abs_delta <= SHORT_DELTA_MAX):
            continue
        # The short leg must be OUT of the money on the correct side.
        if right == "put" and short.strike >= spot:
            continue
        if right == "call" and short.strike <= spot:
            continue

        for width in WIDTHS:
            # protective leg sits FURTHER out of the money than the short
            long_strike = (short.strike - width if right == "put"
                           else short.strike + width)
            long_leg = by_strike.get((str(short.expiry), long_strike))
            if long_leg is None:
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
                continue  # nonsensical pricing, discard

            if long_leg.delta is None:
                continue

            distance_pct = abs(short.strike - spot) / spot
            worst_spread = max(short.spread_pct, long_leg.spread_pct)
            min_oi = min(short.open_interest, long_leg.open_interest)

            # EXPECTED VALUE, not raw reward/risk.
            #
            # Ranking on max_profit/max_loss alone is a trap: it always prefers
            # the narrowest spread closest to the money, which is also the one
            # most likely to lose. Delta is the standard proxy for the risk-
            # neutral probability of finishing in the money, so use it.
            #
            #   above the short strike   -> keep the full credit
            #   between the two strikes  -> partial loss, ~half of max on average
            #   beyond the long strike   -> full max loss
            p_short_itm = short.abs_delta
            p_long_itm = long_leg.abs_delta
            p_win = 1.0 - p_short_itm
            p_partial = max(p_short_itm - p_long_itm, 0.0)
            p_maxloss = p_long_itm

            ev = (p_win * max_profit
                  - p_partial * max_loss * 0.5
                  - p_maxloss * max_loss)
            if ev <= 0:
                continue

            # EV per dollar risked, discounted by the round-trip bid-ask cost.
            score = (ev / max_loss) * (1.0 - worst_spread)

            out.append(SpreadCandidate(
                underlying=underlying,
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
                pop=round(p_win, 4),
                ev=round(ev, 2),
                short_iv=round(short.iv, 4) if short.iv is not None else None,
                min_open_interest=min_oi,
                worst_spread_pct=round(worst_spread, 4),
                distance_pct=round(distance_pct, 4),
                score=round(score, 4),
            ))
    return out


# --------------------------------------------------------------------------- #

def screen_snapshot(snap: MarketSnapshot) -> list[SpreadCandidate]:
    """All structurally valid credit spreads for one underlying."""
    cands = _build_spreads(snap.rows, snap.spot, "put", snap.symbol)
    cands += _build_spreads(snap.rows, snap.spot, "call", snap.symbol)
    return cands


def dedupe_best(cands: list[SpreadCandidate]) -> list[SpreadCandidate]:
    """Keep only the best width per (underlying, expiry, short strike).

    Without this the shortlist fills with the same trade at 1/2/5 wide, which
    wastes the model's attention on near-duplicates.
    """
    best: dict[tuple, SpreadCandidate] = {}
    for c in cands:
        key = (c.underlying, c.expiry, c.kind, c.short_strike)
        cur = best.get(key)
        if cur is None or c.score > cur.score:
            best[key] = c
    return list(best.values())


def balanced_top(cands: list[SpreadCandidate], limit: int) -> list[SpreadCandidate]:
    """Top-N, but spread across (underlying, direction) buckets.

    A pure top-N by score can return eight variations of the same directional
    bet. That is a concentration risk dressed up as a shortlist, and it leaves
    the brain nothing to actually choose between. Round-robin across buckets
    guarantees the model sees both put-credit (bullish/neutral) and call-credit
    (bearish/neutral) structures whenever both qualify.
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
           limit: int = MAX_CANDIDATES) -> tuple[list[SpreadCandidate], dict]:
    """Full deterministic screen across the universe.

    Returns (shortlist, context) where context carries the market facts the
    brain needs in order to reason - spot, realised vol, ATM implied vol.
    """
    universe = universe or UNIVERSE
    all_cands: list[SpreadCandidate] = []
    context: dict = {"underlyings": {}, "feed": None}

    for symbol in universe:
        snap = market.snapshot(symbol, DTE_MIN, DTE_MAX)
        context["feed"] = snap.feed
        iv = atm_iv(snap.rows, snap.spot)
        context["underlyings"][symbol] = {
            "spot": round(snap.spot, 2),
            "atm_iv": round(iv, 4) if iv else None,
            "realized_vol_20d": round(snap.realized_vol, 4),
            # IV richer than realised = premium selling is being paid well.
            # Stands in for IV rank, which needs IV history Alpaca lacks.
            "iv_vs_rv": round(iv / snap.realized_vol, 3)
            if iv and snap.realized_vol and snap.realized_vol > 0 else None,
            "contracts_examined": len(snap.rows),
        }
        all_cands.extend(screen_snapshot(snap))

    shortlist = balanced_top(dedupe_best(all_cands), limit)
    context["candidates_before_dedupe"] = len(all_cands)
    context["candidates_returned"] = len(shortlist)
    return shortlist, context
