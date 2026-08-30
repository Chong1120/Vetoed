"""
loop.py - the orchestrator. One cycle = screen -> think -> gate -> execute.

Order of operations matters and is deliberate:

    1. MANAGE OPEN POSITIONS FIRST. Taking profits and closing before expiry
       is what converts paper gains into REALISED P&L. Opening new trades
       while ignoring existing ones is how an agent ends up holding losers
       into expiry.
    2. Snapshot equity to the journal (feeds the dashboard's equity curve).
    3. Screen deterministically.
    4. Ask the brain to select at most one candidate.
    5. Run the risk gates. They can veto. They always size.
    6. Execute via MCP, journal everything.

DRY RUN is the default. Nothing reaches the broker unless --live is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime

from agent import adapt, brain, journal, risk
from agent.data import Market
from agent.executor import AlpacaMCP, new_client_order_id
from agent.screener import UNIVERSE, screen

# Exit rules - these are what realise P&L inside a short contest window.
TAKE_PROFIT_FRACTION = 0.50   # buy back at 50% of max profit
STOP_LOSS_MULTIPLE = 2.0      # close if losing 2x the credit received
CLOSE_AT_DTE = 1              # never carry into expiry day
DELTA_STOP_MULTIPLE = 2.0     # short leg delta doubles -> price is coming at
                              # us; exit now rather than wait for -2x credit


def log(msg: str) -> None:
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def entry_limit_price(credit: float) -> float:
    """Ask slightly less than modelled mid so we actually get filled.

    Quotes here come from the indicative feed, which is an estimate rather
    than true NBBO, so we concede a little rather than chase a price that may
    not exist.
    """
    return max(round(credit * 0.95, 2), 0.05)


# --------------------------------------------------------------------------- #
# position management - runs BEFORE any new entry
# --------------------------------------------------------------------------- #

def _leg_map(positions) -> dict:
    out = {}
    if isinstance(positions, list):
        for p in positions:
            if isinstance(p, dict) and p.get("symbol"):
                out[p["symbol"]] = p
    return out


async def manage_positions(mcp: AlpacaMCP, market, dry_run: bool) -> list[str]:
    """Close spreads that hit profit target, stop loss, or approach expiry."""
    actions: list[str] = []
    open_rows = journal.open_spreads()
    if not open_rows:
        return actions

    try:
        legs = _leg_map(await mcp.positions())
    except Exception as exc:
        log("could not fetch positions: %s" % exc)
        return actions

    for row in open_rows:
        short_sym, long_sym = row.get("short_symbol"), row.get("long_symbol")
        sp, lp = legs.get(short_sym), legs.get(long_sym)
        if not sp or not lp:
            continue  # not (yet) filled, or already gone

        try:
            unreal = float(sp.get("unrealized_pl") or 0) + \
                     float(lp.get("unrealized_pl") or 0)
        except (TypeError, ValueError):
            continue

        contracts = int(row.get("contracts") or 1)
        credit_total = float(row.get("credit") or 0) * 100 * contracts
        reason = ""

        if credit_total > 0 and unreal >= credit_total * TAKE_PROFIT_FRACTION:
            reason = "take profit (+$%.0f of $%.0f max)" % (unreal, credit_total)
        elif credit_total > 0 and unreal <= -credit_total * STOP_LOSS_MULTIPLE:
            reason = "stop loss (-$%.0f)" % abs(unreal)
        else:
            exp = _expiry_of(short_sym)
            if exp is not None and (exp - date.today()).days <= CLOSE_AT_DTE:
                reason = "approaching expiry (DTE %d)" % (exp - date.today()).days
            else:
                # Delta stop: the short leg's delta doubling means the
                # underlying is moving at us and the probability of loss has
                # roughly doubled since entry. Cutting here turns some
                # max-losses into partial losses.
                entry_d = row.get("entry_short_delta")
                if entry_d:
                    now_d = market.option_delta(short_sym)
                    if now_d and now_d >= float(entry_d) * DELTA_STOP_MULTIPLE:
                        reason = ("delta stop (entry %.2f -> now %.2f)"
                                  % (float(entry_d), now_d))

        if not reason:
            continue

        log("CLOSING %s/%s - %s" % (short_sym, long_sym, reason))
        actions.append("%s: %s" % (short_sym, reason))
        if dry_run:
            log("  DRY RUN - not sending close order")
            continue

        res = await mcp.close_credit_spread(
            short_sym, long_sym, contracts,
            client_order_id=new_client_order_id("close"))
        if res.ok:
            journal.close_order(row.get("alpaca_order_id") or "", unreal)
            log("  close order submitted: %s" % res.order.get("id"))
        else:
            log("  close FAILED: %s" % res.error)
    return actions


def _expiry_of(occ_symbol: str | None) -> date | None:
    """SPY260904P00765000 -> date(2026, 9, 4). OCC layout is fixed-width."""
    if not occ_symbol:
        return None
    for i, ch in enumerate(occ_symbol):
        if ch.isdigit():
            body = occ_symbol[i:]
            if len(body) >= 6:
                try:
                    return datetime.strptime(body[:6], "%y%m%d").date()
                except ValueError:
                    return None
            return None
    return None


# --------------------------------------------------------------------------- #

async def run_cycle(dry_run: bool = True, force: bool = False,
                    use_llm: bool = True) -> int:
    journal.init()
    market = Market()

    is_open = market.is_market_open()
    log("market open=%s  feed=%s  dry_run=%s" % (is_open, market.feed.value, dry_run))
    if not is_open and not force:
        log("market closed - no cycle. (use --force to run anyway)")
        return 0

    async with AlpacaMCP() as mcp:
        # --- 1. manage what we already hold -------------------------------- #
        closed = await manage_positions(mcp, market, dry_run)

        # --- 2. account snapshot ------------------------------------------- #
        acct = await mcp.account()
        equity = float(acct.get("equity") or 0)
        last_equity = float(acct.get("last_equity") or equity)
        open_rows = journal.open_spreads()
        journal.snapshot_equity(
            equity, last_equity, float(acct.get("cash") or 0),
            float(acct.get("buying_power") or 0), len(open_rows))

        acct_state = risk.AccountState(
            equity=equity,
            options_buying_power=float(acct.get("options_buying_power") or 0),
            day_pnl=equity - last_equity,
            open_positions=len(open_rows),
            open_risk=sum(float(r.get("max_loss_total") or 0) for r in open_rows),
            positions_by_underlying=_count_by(open_rows),
        )
        log("equity=$%.2f  day P&L=$%.2f  open=%d"
            % (equity, acct_state.day_pnl, acct_state.open_positions))

        # --- 3. deterministic screen, under adaptive guardrails ------------ #
        # Two passes: the first establishes the volatility regime, the second
        # re-screens under the guardrails that regime implies. The circuit
        # breaker (own-trade history) applies to both.
        _, probe_ctx = screen(market, universe=UNIVERSE[:1])
        rails = adapt.build(probe_ctx)
        for note in rails.notes:
            log("guardrail: %s" % note)

        shortlist, ctx = screen(market, overrides=rails.to_overrides())
        ctx["guardrails"] = rails.to_dict()
        cands = [c.to_dict() for c in shortlist]
        log("screener returned %d candidates" % len(cands))

        run_id = journal.start_run(
            is_open, ctx.get("feed"), equity, acct_state.day_pnl,
            acct_state.halted, len(cands),
            note="; ".join(closed + rails.notes), context=ctx)

        if not cands:
            journal.record_decision(run_id, None, None, None,
                                    outcome="no candidates")
            return 0

        # --- 4. judgement --------------------------------------------------- #
        decision = brain.decide(
            cands, ctx,
            {"equity": equity, "day_pnl": acct_state.day_pnl,
             "open_positions": acct_state.open_positions},
            open_rows, use_llm=use_llm)
        log("brain: %s (confidence %.2f)" % (decision.action, decision.confidence))
        if decision.error:
            log("brain error: %s" % decision.error)

        if not decision.wants_trade:
            journal.record_decision(run_id, None, decision.to_dict(), None,
                                    llm_raw=decision.raw,
                                    llm_error=decision.error,
                                    outcome="no trade")
            log("no trade: %s" % decision.rationale[:200])
            return 0

        candidate = cands[decision.candidate_id]

        # --- 5. hard gates -------------------------------------------------- #
        from agent.screener import SpreadCandidate
        rd = risk.evaluate(SpreadCandidate(**candidate), acct_state)
        log("risk: approved=%s contracts=%d" % (rd.approved, rd.contracts))
        for v in rd.vetoes:
            log("  VETO: %s" % v)

        decision_id = journal.record_decision(
            run_id, candidate, decision.to_dict(), rd.to_dict(),
            llm_raw=decision.raw, llm_error=decision.error,
            outcome="approved" if rd.approved else "vetoed")

        if not rd.approved:
            return 0

        if decision.contracts and decision.contracts != rd.contracts:
            log("note: model suggested %d contracts, risk sized %d (risk wins)"
                % (decision.contracts, rd.contracts))

        # --- 6. execute ----------------------------------------------------- #
        limit = entry_limit_price(float(candidate["credit"]))
        log("ORDER: sell %s / buy %s  x%d  net credit limit $%.2f"
            % (candidate["short_symbol"], candidate["long_symbol"],
               rd.contracts, limit))

        if dry_run:
            log("DRY RUN - order NOT submitted")
            journal.record_order(decision_id, candidate, rd.contracts, limit,
                                 rd.max_loss_total,
                                 {"id": None, "status": "dry_run"})
            return 0

        res = await mcp.submit_credit_spread(
            candidate, rd.contracts, limit,
            client_order_id=new_client_order_id())
        if res.ok:
            log("submitted: order id %s status %s"
                % (res.order.get("id"), res.order.get("status")))
        else:
            log("SUBMIT FAILED: %s" % res.error)
        journal.record_order(decision_id, candidate, rd.contracts, limit,
                             rd.max_loss_total,
                             res.order if res.ok else {"status": "failed",
                                                       "error": res.error})
    return 0


def _count_by(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        u = r.get("underlying") or "?"
        out[u] = out.get(u, 0) + 1
    return out


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha options agent")
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY SUBMIT ORDERS (default is dry run)")
    ap.add_argument("--force", action="store_true",
                    help="run even when the market is closed")
    ap.add_argument("--schedule", action="store_true",
                    help="run continuously on a schedule")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip Claude entirely; use deterministic selection")
    args = ap.parse_args()

    dry_run = not args.live
    if args.live:
        log("*** LIVE MODE - orders WILL be submitted to the paper account ***")

    if not args.schedule:
        return asyncio.run(run_cycle(dry_run, args.force, not args.no_llm))

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone="America/New_York")

    def job():
        try:
            asyncio.run(run_cycle(dry_run, args.force, not args.no_llm))
        except Exception as exc:
            log("CYCLE FAILED: %s: %s" % (type(exc).__name__, exc))

    # US market hours, weekdays. Entries in the first half of the session,
    # management passes through to the close.
    sched.add_job(job, CronTrigger(day_of_week="mon-fri", hour="10-15",
                                   minute="0,30"), id="cycle")
    log("scheduler started (America/New_York, weekdays 10:00-15:30 every 30m)")
    log("dry_run=%s   Ctrl-C to stop" % dry_run)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log("scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
