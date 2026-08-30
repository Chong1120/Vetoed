"""
risk.py - DETERMINISTIC HARD GATES. This module can VETO the LLM.

Design rule: the brain proposes, risk disposes. The LLM never sizes a
position and never has the last word. Every order that reaches Alpaca has
passed every gate in this file, and the gates are pure functions of
(candidate, account state) so they are unit-testable and auditable.

If any gate fails the trade is rejected outright. There is no "override",
no confidence threshold that buys an exception, and no path by which a
persuasive rationale can widen a limit.

The gates, in order:
   1. SESSION HALT      - daily loss stop tripped -> nothing trades today
   2. STRUCTURE         - both legs present, defined risk, never naked
   3. DTE BOUNDS        - no 0DTE gamma cliff, no far-dated capital lockup
   4. LIQUIDITY         - re-checked here, not trusted from the screener
   5. VOLATILITY BOUNDS - refuse absurd IV (bad data or a real event)
   6. CONCENTRATION     - max concurrent positions, max per underlying
   7. SIZING            - risk.py computes contracts, never the LLM
   8. BUYING POWER      - after sizing, must still fit with headroom
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from agent.screener import SpreadCandidate

# --------------------------------------------------------------------------- #
# LIMITS - the entire risk policy, in one auditable block
# --------------------------------------------------------------------------- #

MAX_RISK_PCT_PER_POSITION = 0.05    # 5% of equity at risk in any one spread
MAX_TOTAL_RISK_PCT = 0.25           # 25% of equity at risk across all open
MAX_CONCURRENT_POSITIONS = 5
MAX_POSITIONS_PER_UNDERLYING = 2

DAILY_LOSS_STOP_PCT = 0.03          # -3% on the day -> halt for the session

MIN_DTE = 2                         # 1DTE gamma is brutal; 2 is the floor
MAX_DTE = 14

MIN_OPEN_INTEREST = 250
MAX_SPREAD_PCT = 0.25               # slightly looser than screener, as quotes
                                    # move between screening and execution
MIN_CREDIT = 0.10                   # below this, fees and slippage dominate

MIN_IV = 0.03                       # < 3% implied vol is almost certainly bad data
MAX_IV = 1.50                       # > 150% means an event we do not want to sell

MAX_CONTRACTS_PER_ORDER = 25        # blast radius cap, independent of sizing
BUYING_POWER_HEADROOM = 0.50        # never commit more than 50% of options BP


# --------------------------------------------------------------------------- #

@dataclass
class AccountState:
    """Everything the gates need to know about the account, right now."""

    equity: float
    options_buying_power: float
    day_pnl: float                      # realised + unrealised, today
    open_positions: int
    open_risk: float                    # sum of max_loss across open spreads
    positions_by_underlying: dict[str, int] = field(default_factory=dict)
    halted: bool = False                # set by a previous halt this session


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    max_loss_total: float
    reasons: list[str]                  # why it passed
    vetoes: list[str]                   # why it failed - non-empty => rejected

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# individual gates
# --------------------------------------------------------------------------- #

def _gate_session(acct: AccountState, v: list[str]) -> None:
    if acct.halted:
        v.append("SESSION HALTED: trading stopped for the day")
        return
    if acct.equity > 0:
        loss_pct = acct.day_pnl / acct.equity
        if loss_pct <= -DAILY_LOSS_STOP_PCT:
            v.append("DAILY LOSS STOP: day P&L %.2f%% <= -%.2f%%"
                     % (loss_pct * 100, DAILY_LOSS_STOP_PCT * 100))


def _gate_structure(c: SpreadCandidate, v: list[str]) -> None:
    """The single most important gate: prove the position is defined-risk."""
    if not c.short_symbol or not c.long_symbol:
        v.append("STRUCTURE: missing a leg - would be NAKED SHORT")
        return
    if c.short_symbol == c.long_symbol:
        v.append("STRUCTURE: both legs identical - not a spread")
        return
    if c.width <= 0:
        v.append("STRUCTURE: non-positive width")
        return

    # The long leg must sit FURTHER out of the money than the short leg,
    # on the correct side. Otherwise it does not cap the loss.
    if c.kind == "put_credit" and not c.long_strike < c.short_strike:
        v.append("STRUCTURE: put spread long strike %.2f not below short %.2f "
                 "- loss is NOT capped" % (c.long_strike, c.short_strike))
    if c.kind == "call_credit" and not c.long_strike > c.short_strike:
        v.append("STRUCTURE: call spread long strike %.2f not above short %.2f "
                 "- loss is NOT capped" % (c.long_strike, c.short_strike))

    if abs(abs(c.short_strike - c.long_strike) - c.width) > 1e-6:
        v.append("STRUCTURE: width %.2f disagrees with strikes %.2f/%.2f"
                 % (c.width, c.short_strike, c.long_strike))

    if c.max_loss <= 0:
        v.append("STRUCTURE: max_loss %.2f is not positive" % c.max_loss)

    expected = c.width * 100.0 - c.credit * 100.0
    if abs(c.max_loss - expected) > 1.0:
        v.append("STRUCTURE: max_loss %.2f != width*100 - credit*100 (%.2f)"
                 % (c.max_loss, expected))


def _gate_dte(c: SpreadCandidate, v: list[str]) -> None:
    if c.dte < MIN_DTE:
        v.append("DTE %d below minimum %d (0DTE gamma risk)" % (c.dte, MIN_DTE))
    if c.dte > MAX_DTE:
        v.append("DTE %d above maximum %d" % (c.dte, MAX_DTE))


def _gate_liquidity(c: SpreadCandidate, v: list[str]) -> None:
    if c.min_open_interest < MIN_OPEN_INTEREST:
        v.append("LIQUIDITY: open interest %d < %d"
                 % (c.min_open_interest, MIN_OPEN_INTEREST))
    if c.worst_spread_pct > MAX_SPREAD_PCT:
        v.append("LIQUIDITY: bid-ask %.1f%% > %.1f%% of mid"
                 % (c.worst_spread_pct * 100, MAX_SPREAD_PCT * 100))
    if c.credit < MIN_CREDIT:
        v.append("LIQUIDITY: credit %.3f below minimum %.3f"
                 % (c.credit, MIN_CREDIT))


def _gate_volatility(c: SpreadCandidate, v: list[str]) -> None:
    if c.short_iv is None:
        v.append("VOLATILITY: no implied vol available for the short leg")
        return
    if c.short_iv < MIN_IV:
        v.append("VOLATILITY: IV %.3f below %.3f - suspect data"
                 % (c.short_iv, MIN_IV))
    if c.short_iv > MAX_IV:
        v.append("VOLATILITY: IV %.3f above %.3f - event risk"
                 % (c.short_iv, MAX_IV))


def _gate_concentration(c: SpreadCandidate, acct: AccountState,
                        v: list[str]) -> None:
    if acct.open_positions >= MAX_CONCURRENT_POSITIONS:
        v.append("CONCENTRATION: %d open positions >= limit %d"
                 % (acct.open_positions, MAX_CONCURRENT_POSITIONS))
    n = acct.positions_by_underlying.get(c.underlying, 0)
    if n >= MAX_POSITIONS_PER_UNDERLYING:
        v.append("CONCENTRATION: %d open on %s >= limit %d"
                 % (n, c.underlying, MAX_POSITIONS_PER_UNDERLYING))


# --------------------------------------------------------------------------- #
# sizing - the LLM never does this
# --------------------------------------------------------------------------- #

def size_position(c: SpreadCandidate, acct: AccountState) -> tuple[int, list[str]]:
    """How many spreads may we take? Smallest of every binding constraint."""
    notes: list[str] = []
    if c.max_loss <= 0:
        return 0, ["sizing: non-positive max_loss"]

    per_position_budget = acct.equity * MAX_RISK_PCT_PER_POSITION
    by_position = math.floor(per_position_budget / c.max_loss)
    notes.append("per-position budget $%.0f / $%.0f max loss -> %d"
                 % (per_position_budget, c.max_loss, by_position))

    remaining_total = acct.equity * MAX_TOTAL_RISK_PCT - acct.open_risk
    by_total = math.floor(max(remaining_total, 0.0) / c.max_loss)
    notes.append("portfolio headroom $%.0f -> %d" % (remaining_total, by_total))

    usable_bp = acct.options_buying_power * BUYING_POWER_HEADROOM
    by_bp = math.floor(usable_bp / c.max_loss) if c.max_loss > 0 else 0
    notes.append("usable buying power $%.0f -> %d" % (usable_bp, by_bp))

    contracts = min(by_position, by_total, by_bp, MAX_CONTRACTS_PER_ORDER)
    contracts = max(contracts, 0)
    notes.append("final size = min(...) capped at %d -> %d"
                 % (MAX_CONTRACTS_PER_ORDER, contracts))
    return contracts, notes


# --------------------------------------------------------------------------- #

def evaluate(c: SpreadCandidate, acct: AccountState) -> RiskDecision:
    """Run every gate. Any veto rejects the trade."""
    vetoes: list[str] = []
    reasons: list[str] = []

    _gate_session(acct, vetoes)
    _gate_structure(c, vetoes)
    _gate_dte(c, vetoes)
    _gate_liquidity(c, vetoes)
    _gate_volatility(c, vetoes)
    _gate_concentration(c, acct, vetoes)

    if vetoes:
        return RiskDecision(False, 0, 0.0, reasons, vetoes)

    contracts, notes = size_position(c, acct)
    reasons.extend(notes)
    if contracts < 1:
        vetoes.append("SIZING: constraints permit 0 contracts")
        return RiskDecision(False, 0, 0.0, reasons, vetoes)

    total_risk = contracts * c.max_loss

    # Final buying-power assertion AFTER sizing.
    required_bp = total_risk
    if required_bp > acct.options_buying_power * BUYING_POWER_HEADROOM:
        vetoes.append("BUYING POWER: need $%.0f, headroom allows $%.0f"
                      % (required_bp,
                         acct.options_buying_power * BUYING_POWER_HEADROOM))
        return RiskDecision(False, 0, 0.0, reasons, vetoes)

    if total_risk > acct.equity * MAX_RISK_PCT_PER_POSITION + 1e-6:
        vetoes.append("RISK: total $%.0f exceeds per-position cap $%.0f"
                      % (total_risk, acct.equity * MAX_RISK_PCT_PER_POSITION))
        return RiskDecision(False, 0, 0.0, reasons, vetoes)

    reasons.append("approved: %d contract(s), max loss $%.0f (%.2f%% of equity)"
                   % (contracts, total_risk, 100 * total_risk / acct.equity))
    return RiskDecision(True, contracts, total_risk, reasons, vetoes)


def account_state_from_alpaca(trading, open_spreads: list[dict] | None = None,
                              halted: bool = False) -> AccountState:
    """Build AccountState from a live Alpaca account + our own journal."""
    acct = trading.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity or equity)
    open_spreads = open_spreads or []

    by_underlying: dict[str, int] = {}
    open_risk = 0.0
    for s in open_spreads:
        u = s.get("underlying", "?")
        by_underlying[u] = by_underlying.get(u, 0) + 1
        open_risk += float(s.get("max_loss_total", 0.0))

    return AccountState(
        equity=equity,
        options_buying_power=float(acct.options_buying_power or 0.0),
        day_pnl=equity - last_equity,
        open_positions=len(open_spreads),
        open_risk=open_risk,
        positions_by_underlying=by_underlying,
        halted=halted,
    )
