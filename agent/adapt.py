"""
adapt.py - two-tier adaptive guardrails.

HONEST FRAMING FIRST. This is not machine learning and it is not a
"self-improving AI". Over a five-day contest the agent will close perhaps
10-20 trades. Distinguishing a 60% win rate from a 70% one at any real
statistical confidence needs HUNDREDS of trades. Anyone claiming a model
learned something useful from a dozen samples is overclaiming.

So the adaptation is split by how much data actually backs it:

  TIER 1 - REGIME (market data, thousands of observations)
      Reads implied vs realised volatility and adjusts DTE preference.
      This is statistically meaningful because it is measured from price
      history, not from our own handful of trades.

  TIER 2 - CIRCUIT BREAKER (our own trades, tiny sample)
      Can only ever DISABLE something after repeated losses. It can never
      widen a limit, increase size, or enable a setup. A small sample can
      therefore make the agent more cautious but never more reckless.

Every adjustment is written to the journal with its reason, so the dashboard
shows exactly what changed and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from agent import journal, screener

# Tier 2 thresholds
MIN_CLOSED_TRADES = 5          # never react to fewer than this
CONSECUTIVE_LOSS_LIMIT = 3     # 3 straight losses on one underlying -> ban it
LOSS_RATE_LIMIT = 0.70         # >70% losers in a bucket -> narrow that bucket

# Tier 1 regime bands (implied vs realised volatility)
RICH_IV = 1.10                 # implied >10% above realised: premium is rich
CHEAP_IV = 0.95                # implied below realised: poor compensation


@dataclass
class Guardrails:
    """Overrides handed to screener.screen(). Empty means 'use defaults'."""

    dte_min: int | None = None
    dte_max: int | None = None
    delta_min: float | None = None
    delta_max: float | None = None
    banned_underlyings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_overrides(self) -> dict:
        out: dict = {}
        if self.dte_min is not None:
            out["dte_min"] = self.dte_min
        if self.dte_max is not None:
            out["dte_max"] = self.dte_max
        if self.delta_min is not None:
            out["delta_min"] = self.delta_min
        if self.delta_max is not None:
            out["delta_max"] = self.delta_max
        if self.banned_underlyings:
            out["banned_underlyings"] = list(self.banned_underlyings)
        return out

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# TIER 1 - regime, from market data
# --------------------------------------------------------------------------- #

def regime_adjust(context: dict, g: Guardrails) -> None:
    """Adjust DTE preference from the implied-vs-realised volatility ratio.

    Rich implied vol means the premium being offered is generous relative to
    how much the underlying has actually been moving - Bakshi & Kapadia (2003)
    found the premium is larger at higher volatility. When it is rich we are
    willing to hold slightly longer-dated spreads; when it is thin we stay
    short-dated so capital turns over faster.
    """
    ratios = [d.get("iv_vs_rv") for d in context.get("underlyings", {}).values()
              if isinstance(d, dict) and d.get("iv_vs_rv")]
    if not ratios:
        return
    avg = sum(ratios) / len(ratios)

    if avg >= RICH_IV:
        g.dte_min, g.dte_max = 3, 14
        g.notes.append(
            "REGIME: implied/realised %.2f >= %.2f - premium is rich, "
            "allowing DTE 3-14" % (avg, RICH_IV))
    elif avg <= CHEAP_IV:
        g.dte_min, g.dte_max = 2, 7
        g.notes.append(
            "REGIME: implied/realised %.2f <= %.2f - premium is thin, "
            "staying short-dated DTE 2-7" % (avg, CHEAP_IV))
    else:
        g.notes.append("REGIME: implied/realised %.2f is neutral - defaults"
                       % avg)


# --------------------------------------------------------------------------- #
# TIER 2 - circuit breaker, from our own closed trades
# --------------------------------------------------------------------------- #

def _closed_trades(path: str | None = None) -> list[dict]:
    rows = journal.all_orders(500, path=path) if path else journal.all_orders(500)
    return [r for r in rows
            if r.get("closed_ts") and r.get("realised_pnl") is not None]


def circuit_breaker(g: Guardrails, path: str | None = None) -> None:
    """Disable setups that have repeatedly lost. Can ONLY restrict."""
    closed = _closed_trades(path)
    if len(closed) < MIN_CLOSED_TRADES:
        g.notes.append(
            "CIRCUIT BREAKER: only %d closed trade(s), need %d - no change "
            "(a handful of trades is not evidence)"
            % (len(closed), MIN_CLOSED_TRADES))
        return

    # Consecutive losses per underlying, most recent first.
    by_symbol: dict[str, list[dict]] = {}
    for r in closed:
        by_symbol.setdefault(r.get("underlying") or "?", []).append(r)

    for sym, rows in by_symbol.items():
        streak = 0
        for r in rows:                      # all_orders is newest-first
            if float(r["realised_pnl"]) < 0:
                streak += 1
            else:
                break
        if streak >= CONSECUTIVE_LOSS_LIMIT:
            g.banned_underlyings.append(sym)
            g.notes.append(
                "CIRCUIT BREAKER: %s disabled - %d consecutive losing trades"
                % (sym, streak))

    # Delta bucket performance. Only ever narrows the band.
    hi_bucket = [r for r in closed if _short_delta(r) >= 0.25]
    if len(hi_bucket) >= MIN_CLOSED_TRADES:
        losers = sum(1 for r in hi_bucket if float(r["realised_pnl"]) < 0)
        rate = losers / len(hi_bucket)
        if rate > LOSS_RATE_LIMIT:
            g.delta_max = 0.25
            g.notes.append(
                "CIRCUIT BREAKER: delta >= 0.25 lost %d of %d (%.0f%%) - "
                "narrowing band to <= 0.25"
                % (losers, len(hi_bucket), rate * 100))


def _short_delta(row: dict) -> float:
    try:
        return abs(float(row.get("entry_short_delta") or 0.0))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #

def build(context: dict | None = None, path: str | None = None) -> Guardrails:
    """Compute this cycle's guardrails. Never raises."""
    g = Guardrails()
    try:
        if context:
            regime_adjust(context, g)
    except Exception as exc:
        g.notes.append("REGIME: skipped (%s)" % type(exc).__name__)
    try:
        circuit_breaker(g, path)
    except Exception as exc:
        g.notes.append("CIRCUIT BREAKER: skipped (%s)" % type(exc).__name__)

    # Hard invariant: guardrails may only ever RESTRICT relative to the
    # screener's own defaults. Anything looser is clamped back.
    if g.dte_min is not None:
        g.dte_min = max(g.dte_min, screener.DTE_MIN)
    if g.dte_max is not None:
        g.dte_max = min(g.dte_max, screener.DTE_MAX)
    if g.delta_min is not None:
        g.delta_min = max(g.delta_min, screener.SHORT_DELTA_MIN)
    if g.delta_max is not None:
        g.delta_max = min(g.delta_max, screener.SHORT_DELTA_MAX)
    return g
