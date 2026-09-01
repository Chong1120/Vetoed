"""
loop.py - the orchestrator. One cycle = reconcile -> manage -> screen -> think
-> gate -> execute.

Order of operations matters and is deliberate:

    0. HARD LOCKS. Paper-trading is asserted before anything else, and a
       cross-process lock guarantees only one cycle is ever in flight.
    1. RECONCILE WITH THE BROKER. Alpaca decides what we hold, not our
       journal. See reconcile.py for why this is not optional once the process
       can restart.
    2. MANAGE OPEN POSITIONS. Taking profits and closing before expiry is what
       converts paper gains into REALISED P&L. Opening new trades while
       ignoring existing ones is how an agent ends up holding losers into
       expiry.
    3. Snapshot equity to the journal (feeds the dashboard's equity curve).
    4. Screen deterministically.
    5. Ask the brain to select at most one candidate.
    6. Run the risk gates. They can veto. They always size.
    7. Execute via MCP under a deterministic client_order_id, and journal
       everything.

DRY RUN is the default. Nothing reaches the broker unless --live is passed.

UNATTENDED OPERATION. In production this runs one cycle per invocation from
a GitHub Actions schedule (.github/workflows/agent.yml). `--schedule` runs the
same cycle on an internal timer instead, for a long-lived host.

Either way a cycle must be safe to start from cold, because an Actions runner
IS a cold start every time - fresh container, journal checked out from git.
Three things make that safe rather than merely possible: the broker
reconciliation in step 1, the deterministic client_order_id in step 7 (Alpaca
refuses a duplicate, so a retry after a timeout cannot fill twice), and the
single-flight guard in step 0 - runlock.py on a long-lived host, the
workflow's `concurrency` group on Actions, where a lock file cannot persist.

`--force` is refused in combination with `--schedule`, because a scheduler that
ignores market hours would trade weekend quotes forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import date, datetime, timezone

from agent import adapt, brain, journal, reconcile, risk, runlock
from agent.data import Market
from agent.executor import AlpacaMCP, new_client_order_id
from agent.screener import UNIVERSE, screen

# Exit rules - these are what realise P&L inside a short contest window.
# UNCHANGED by the unattended-operation work: an audit found no bug in them,
# and loosening a risk parameter to suit a deployment would be backwards.
TAKE_PROFIT_FRACTION = 0.50   # buy back at 50% of max profit
STOP_LOSS_MULTIPLE = 2.0      # close if losing 2x the credit received
CLOSE_AT_DTE = 1              # never carry into expiry day
DELTA_STOP_MULTIPLE = 2.0     # short leg delta doubles -> price is coming at
                              # us; exit now rather than wait for -2x credit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_PATH = os.path.join(ROOT, "journal", "health.json")

# 30 minutes is the default because a cycle's inputs barely move faster than
# that: the screener reads a 20-day realised-vol estimate and an option chain
# whose relevant Greeks drift slowly at 2-14 DTE. Polling faster does not
# surface better trades, it just re-examines the same ones and burns API quota
# and Claude calls. The interval is CONFIGURATION, not strategy - every gate,
# threshold and exit rule is identical at any interval - which is why it is
# safe to expose. Shorter intervals exist for demos, not for production.
DEFAULT_POLL_MINUTES = 30
MIN_POLL_MINUTES = 1
MAX_POLL_MINUTES = 240

_shutdown = False


# --------------------------------------------------------------------------- #
# logging - structured enough to grep in a CI log, plain enough to read
# --------------------------------------------------------------------------- #

def log(msg: str, level: str = "INFO") -> None:
    print("%s %-5s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          level, msg), flush=True)


def warn(msg: str) -> None:
    log(msg, "WARN")


def error(msg: str) -> None:
    log(msg, "ERROR")


# --------------------------------------------------------------------------- #
# hard locks
# --------------------------------------------------------------------------- #

def assert_paper_trading() -> None:
    """Fail closed. Called before the process does anything at all.

    Already enforced inside data.load_keys() and executor._child_env(), but
    those fire mid-cycle. On a server we want the unit to refuse to start,
    visibly, rather than fail on the first order attempt hours later.
    """
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
    flag = os.getenv("ALPACA_PAPER_TRADE", "").strip().lower()
    if flag != "true":
        raise SystemExit(
            "REFUSING TO START: ALPACA_PAPER_TRADE is %r, expected 'true'.\n"
            "Vetoed is paper-trading only. There is no live-capital mode and "
            "this check is not overridable." % (flag or "<unset>"))


def poll_interval_minutes() -> int:
    """POLL_INTERVAL_MINUTES, clamped and validated."""
    raw = os.getenv("POLL_INTERVAL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_POLL_MINUTES
    try:
        val = int(raw)
    except ValueError:
        warn("POLL_INTERVAL_MINUTES=%r is not an integer - using %d"
             % (raw, DEFAULT_POLL_MINUTES))
        return DEFAULT_POLL_MINUTES
    if not (MIN_POLL_MINUTES <= val <= MAX_POLL_MINUTES):
        warn("POLL_INTERVAL_MINUTES=%d out of range [%d, %d] - using %d"
             % (val, MIN_POLL_MINUTES, MAX_POLL_MINUTES, DEFAULT_POLL_MINUTES))
        return DEFAULT_POLL_MINUTES
    return val


def write_health(**fields) -> None:
    """A heartbeat the dashboard and an operator can both read.

    Deliberately a file, not a socket: it survives the process dying, which is
    exactly when you want to know what the last cycle did.
    """
    try:
        os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
        current = {}
        if os.path.exists(HEALTH_PATH):
            try:
                with open(HEALTH_PATH, encoding="utf-8") as fh:
                    current = json.load(fh)
            except Exception:
                current = {}
        current.update(fields)
        current["updated_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        current["pid"] = os.getpid()
        tmp = HEALTH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, default=str)
        os.replace(tmp, HEALTH_PATH)      # atomic; never a half-written file
    except Exception as exc:
        warn("could not write health file: %s" % exc)


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

async def manage_positions(mcp: AlpacaMCP, market, dry_run: bool,
                           legs: dict | None = None) -> list[str]:
    """Close spreads that hit profit target, stop loss, or approach expiry.

    `legs` may be supplied by the caller's reconciliation pass so the broker is
    not queried twice in one cycle. Exit rules themselves are unchanged.
    """
    actions: list[str] = []
    open_rows = journal.open_spreads()
    if not open_rows:
        return actions

    if legs is None:
        try:
            legs = reconcile._leg_map(await mcp.positions())
        except Exception as exc:
            error("could not fetch positions: %s" % exc)
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
            journal.close_order(row.get("alpaca_order_id") or "", unreal, reason)
            log("  close order submitted: %s" % res.order.get("id"))
        else:
            error("  close FAILED: %s" % res.error)
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
                    use_llm: bool = True, no_open: bool = False) -> int:
    """One full cycle.

    `no_open` runs everything except the entry: reconcile, manage exits,
    screen, judge, gate, journal - then stops short of submitting an opening
    order. It exists so exits can be checked far more often than entries are
    taken. A late entry costs nothing; a late exit is the real exposure, and
    pacing the two together forces one to inherit the other's cadence.

    Exits are NOT suppressed. A stop-loss or take-profit found in an analysis
    pass is acted on immediately - that is the entire point.
    """
    journal.init()
    started = time.time()
    market = Market()

    is_open = market.is_market_open()
    log("cycle start  market_open=%s  feed=%s  dry_run=%s"
        % (is_open, market.feed.value, dry_run))
    write_health(market_open=is_open, feed=market.feed.value, dry_run=dry_run,
                 cycle_state="running")
    if not is_open and not force:
        log("market closed - no cycle (use --force to run anyway)")
        write_health(cycle_state="idle_market_closed", last_error="")
        return 0

    async with AlpacaMCP() as mcp:
        # --- 1. reconcile against the broker ------------------------------- #
        state = await reconcile.fetch_broker_state(mcp)
        rec = reconcile.reconcile(state)
        # Keep the broker's own view of the account, so the published dashboard
        # reports what Alpaca holds rather than what we believe it holds. Only
        # when the broker was actually readable - an unreachable cycle must not
        # be allowed to erase the last known truth.
        if state.reachable:
            journal.record_broker_positions(state.legs)
        for note in rec.corrections:
            warn("reconcile: %s" % note)
        log("reconcile: %d open spread(s) confirmed, %d orphan leg(s)"
            % (len(rec.open_spreads), len(rec.orphan_legs)))

        # --- 2. manage what we already hold -------------------------------- #
        closed = await manage_positions(mcp, market, dry_run,
                                        legs=state.legs if state.reachable else None)

        # --- 3. account snapshot, built from BROKER-confirmed state -------- #
        acct = await mcp.account()
        equity = float(acct.get("equity") or 0)
        last_equity = float(acct.get("last_equity") or equity)
        journal.snapshot_equity(
            equity, last_equity, float(acct.get("cash") or 0),
            float(acct.get("buying_power") or 0), rec.open_count)

        acct_state = reconcile.account_state_from(
            rec, equity=equity,
            options_buying_power=float(acct.get("options_buying_power") or 0),
            day_pnl=equity - last_equity)
        open_rows = rec.open_spreads

        log("equity=$%.2f  day P&L=$%.2f  open=%d  open_risk=$%.0f"
            % (equity, acct_state.day_pnl, acct_state.open_positions,
               acct_state.open_risk))
        write_health(equity=equity, day_pnl=acct_state.day_pnl,
                     open_positions=acct_state.open_positions,
                     open_risk=acct_state.open_risk)

        # --- 4. deterministic screen, under adaptive guardrails ------------ #
        _, probe_ctx = screen(market, universe=UNIVERSE[:1])
        rails = adapt.build(probe_ctx)
        for note in rails.notes:
            log("guardrail: %s" % note)

        shortlist, ctx = screen(market, overrides=rails.to_overrides())
        ctx["guardrails"] = rails.to_dict()
        cands = [c.to_dict() for c in shortlist]
        log("screener returned %d candidates" % len(cands))

        # Drop candidates whose legs are ALREADY held. The idempotency guard
        # catches these at the very last moment, which is correct as a defence
        # but wasteful as a policy: the model kept choosing the identical AAPL
        # spread it was already in, the guard refused it, and the cycle ended
        # having done nothing. Adding to a position is not what "a second
        # position in this underlying" is meant to allow - different strikes
        # are. Removing them here lets the model choose something it can
        # actually take.
        if state.reachable and state.legs:
            held = set(state.legs)
            before = len(cands)
            cands = [c for c in cands
                     if not (c["short_symbol"] in held
                             and c["long_symbol"] in held)]
            if len(cands) != before:
                log("dropped %d candidate(s) already held at the broker"
                    % (before - len(cands)))

        # Drop underlyings that are already at their per-underlying limit.
        #
        # Same reasoning as the filter above, one level up. AAPL sat at 2 of a
        # permitted 2 and kept winning the ranking on edge, so five cycles in a
        # row chose AAPL, hit CONCENTRATION at the gate, and ended having done
        # nothing - while SPY, QQQ and IWM candidates sat in the same shortlist
        # untaken. The veto was right every time; offering the candidate at all
        # was not.
        #
        # Nothing is weakened: the gate still counts and still refuses. This
        # stops the model being handed a choice it cannot act on.
        at_limit = {u for u in {r.get("underlying") for r in open_rows}
                    if sum(1 for r in open_rows if r.get("underlying") == u)
                    >= risk.MAX_POSITIONS_PER_UNDERLYING}
        if at_limit:
            before = len(cands)
            cands = [c for c in cands if c["underlying"] not in at_limit]
            if len(cands) != before:
                log("dropped %d candidate(s) on %s - already at the %d-position "
                    "limit for that underlying"
                    % (before - len(cands), ", ".join(sorted(at_limit)),
                       risk.MAX_POSITIONS_PER_UNDERLYING))

        run_id = journal.start_run(
            is_open, ctx.get("feed"), equity, acct_state.day_pnl,
            acct_state.halted, len(cands),
            note="; ".join(closed + rails.notes + rec.corrections), context=ctx,
            shortlist=cands)

        if not cands:
            journal.record_decision(run_id, None, None, None,
                                    outcome="no candidates")
            write_health(cycle_state="idle", last_cycle_outcome="no candidates",
                         last_success=datetime.now(timezone.utc).isoformat(
                             timespec="seconds"))
            return 0

        # A broker we cannot see is a broker we cannot safely add risk against.
        if not state.reachable:
            journal.record_decision(run_id, None, None, None,
                                    outcome="broker unreachable")
            error("broker state unavailable - managing only, no new entries")
            write_health(cycle_state="degraded",
                         last_error="broker unreachable: %s" % state.error[:200])
            return 0

        # --- 5. judgement --------------------------------------------------- #
        decision = brain.decide(
            cands, ctx,
            {"equity": equity, "day_pnl": acct_state.day_pnl,
             "open_positions": acct_state.open_positions},
            open_rows, use_llm=use_llm)
        log("brain: %s (confidence %.2f)" % (decision.action, decision.confidence))
        if decision.error:
            warn("brain error (fell back to deterministic): %s" % decision.error)

        if not decision.wants_trade:
            journal.record_decision(run_id, None, decision.to_dict(), None,
                                    llm_raw=decision.raw,
                                    llm_error=decision.error,
                                    outcome="no trade")
            log("no trade: %s" % decision.rationale[:200])
            write_health(cycle_state="idle", last_cycle_outcome="no trade",
                         last_success=datetime.now(timezone.utc).isoformat(
                             timespec="seconds"))
            return 0

        candidate = cands[decision.candidate_id]

        # --- 6. hard gates -------------------------------------------------- #
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
            write_health(cycle_state="idle", last_cycle_outcome="vetoed",
                         last_success=datetime.now(timezone.utc).isoformat(
                             timespec="seconds"))
            return 0

        if decision.contracts and decision.contracts != rd.contracts:
            log("note: model suggested %d contracts, risk sized %d (risk wins)"
                % (decision.contracts, rd.contracts))

        # --- 7. idempotency guard, then execute ----------------------------- #
        # Last line of defence before the only write path in the agent. Checks
        # the broker, not the journal, because the journal is what a crash
        # loses.
        duplicate = reconcile.already_working(state, candidate, rd.contracts)
        if duplicate:
            warn("SKIPPING duplicate submission: %s" % duplicate)
            # Correct the decision already recorded above. Recording a second
            # one would double-count a single cycle's judgement, which is
            # exactly what put every decision on the dashboard twice.
            journal.set_decision_outcome(decision_id, "duplicate skipped")
            write_health(cycle_state="idle",
                         last_cycle_outcome="duplicate skipped")
            return 0

        limit = entry_limit_price(float(candidate["credit"]))
        coid = reconcile.deterministic_client_order_id(
            candidate["underlying"], candidate["short_symbol"],
            candidate["long_symbol"], rd.contracts)
        log("ORDER: sell %s / buy %s  x%d  net credit limit $%.2f  coid=%s"
            % (candidate["short_symbol"], candidate["long_symbol"],
               rd.contracts, limit, coid))

        if dry_run:
            log("DRY RUN - order NOT submitted")
            journal.record_order(decision_id, candidate, rd.contracts, limit,
                                 rd.max_loss_total,
                                 {"id": None, "status": "dry_run",
                                  "client_order_id": coid})
            write_health(cycle_state="idle", last_cycle_outcome="dry run order",
                         last_success=datetime.now(timezone.utc).isoformat(
                             timespec="seconds"))
            return 0

        if no_open:
            # Everything above still happened: positions reconciled, exits
            # managed and acted on, the shortlist scored, the judgement made
            # and journalled. Only the new position is withheld, so the record
            # shows what the agent would have done at this moment.
            log("ANALYSIS PASS - entry withheld until the next execution cycle")
            journal.record_order(decision_id, candidate, rd.contracts, limit,
                                 rd.max_loss_total,
                                 {"id": None, "status": "analysis_only",
                                  "client_order_id": coid})
            write_health(cycle_state="idle",
                         last_cycle_outcome="analysis pass - entry withheld",
                         last_success=datetime.now(timezone.utc).isoformat(
                             timespec="seconds"))
            return 0

        res = await mcp.submit_credit_spread(candidate, rd.contracts, limit,
                                             client_order_id=coid)
        if res.ok:
            log("submitted: order id %s status %s"
                % (res.order.get("id"), res.order.get("status")))
            payload = res.order
        elif res.uncertain:
            # The critical branch. We do NOT know whether Alpaca received
            # this. Journal it as uncertain so open_spreads() counts it as
            # live risk, and let the next cycle's reconciliation resolve it
            # against the broker. NEVER retry here - a blind retry is exactly
            # how a timeout becomes a double position.
            error("SUBMIT UNCERTAIN (not retried): %s" % res.error)
            payload = {"status": "uncertain", "error": res.error,
                       "client_order_id": coid}
        else:
            error("SUBMIT REJECTED: %s" % res.error)
            payload = {"status": "failed", "error": res.error,
                       "client_order_id": coid}

        journal.record_order(decision_id, candidate, rd.contracts, limit,
                             rd.max_loss_total, payload)
        write_health(cycle_state="idle",
                     last_cycle_outcome=("submitted" if res.ok else
                                         "uncertain" if res.uncertain else "rejected"),
                     last_success=datetime.now(timezone.utc).isoformat(
                         timespec="seconds"))
    log("cycle done in %.1fs" % (time.time() - started))
    return 0


# --------------------------------------------------------------------------- #

async def guarded_cycle(dry_run: bool, force: bool, use_llm: bool,
                        no_open: bool = False) -> int:
    """One cycle under the single-flight lock. Never raises."""
    try:
        with runlock.single_flight(on_stale=warn):
            return await run_cycle(dry_run, force, use_llm, no_open)
    except runlock.LockBusy as exc:
        warn("skipping tick: %s" % exc)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        error("CYCLE FAILED: %s: %s" % (type(exc).__name__, exc))
        write_health(cycle_state="error",
                     last_error="%s: %s" % (type(exc).__name__, exc))
        return 1


def _install_signal_handlers(sched=None) -> None:
    """A supervisor stops a process with SIGTERM - systemd, Docker, or a CI
    runner cancelling a job. Exit cleanly so the run lock is released."""
    def handler(signum, _frame):
        global _shutdown
        _shutdown = True
        log("received %s - shutting down after the current cycle"
            % signal.Signals(signum).name)
        write_health(cycle_state="stopping")
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass          # not the main thread, or unsupported on this platform


def main() -> int:
    ap = argparse.ArgumentParser(description="Vetoed - options agent")
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY SUBMIT ORDERS (default is dry run)")
    ap.add_argument("--force", action="store_true",
                    help="run one cycle even when the market is closed "
                         "(refused together with --schedule)")
    ap.add_argument("--schedule", action="store_true",
                    help="run continuously on a schedule")
    ap.add_argument("--no-open", action="store_true",
                    help="analyse and manage exits, but do not open a new "
                         "position (entries are paced separately from exits)")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip Claude entirely; use deterministic selection")
    args = ap.parse_args()

    # Fail closed, before anything else touches the network.
    assert_paper_trading()

    dry_run = not args.live
    if args.live:
        log("*** LIVE MODE - orders WILL be submitted to the PAPER account ***")

    if not args.schedule:
        _install_signal_handlers()
        return asyncio.run(guarded_cycle(dry_run, args.force,
                                         not args.no_llm, args.no_open))

    # --force exists to test a single cycle against a closed market. Combined
    # with --schedule it would mean "trade on stale weekend quotes, forever",
    # which is not a thing anyone wants running unattended.
    if args.force:
        raise SystemExit(
            "REFUSING TO START: --force cannot be combined with --schedule.\n"
            "--force bypasses the market-open check, which is a debugging aid "
            "for one cycle, not a mode of unattended operation.")

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    minutes = poll_interval_minutes()
    sched = BlockingScheduler(timezone="America/New_York")
    _install_signal_handlers(sched)

    def job():
        if _shutdown:
            return
        asyncio.run(guarded_cycle(dry_run, False,
                                  not args.no_llm, args.no_open))

    # The cron window is a coarse filter in US Eastern time - never the local
    # clock of whatever host this runs on. The AUTHORITATIVE market-open check
    # is Alpaca's own clock inside run_cycle(), which handles holidays and
    # early closes that a cron expression cannot.
    sched.add_job(
        job,
        CronTrigger(day_of_week="mon-fri", hour="9-16",
                    minute="*/%d" % minutes if minutes < 60 else "0"),
        id="cycle",
        max_instances=1,        # belt; runlock.py is the braces
        coalesce=True,          # a backlog after a pause runs once, not N times
        misfire_grace_time=120,
    )

    log("=" * 68)
    log("Vetoed scheduler started")
    log("  poll interval : %d minute(s)   [POLL_INTERVAL_MINUTES]" % minutes)
    log("  window        : Mon-Fri 09:00-16:59 America/New_York (coarse)")
    log("  authoritative : Alpaca clock, checked every cycle")
    log("  mode          : %s" % ("LIVE (paper account)" if args.live else "DRY RUN"))
    log("  brain         : %s" % ("deterministic only" if args.no_llm else "Claude + fallback"))
    log("  pid           : %d" % os.getpid())
    log("=" * 68)
    write_health(cycle_state="starting", poll_interval_minutes=minutes,
                 dry_run=dry_run, started_at=datetime.now(timezone.utc)
                 .isoformat(timespec="seconds"), last_error="")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    log("scheduler stopped")
    write_health(cycle_state="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
