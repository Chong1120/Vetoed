"""Close every open spread, through the agent's own close path.

For shutting the book when the agent is stopped. Nothing here is a strategy:
it closes what is open, once, and records what the broker filled.

Safe by construction, in the same way the agent is:
  - refuses unless ALPACA_PAPER_TRADE=true
  - refuses unless Alpaca says the market is open, because an option order
    outside regular hours is rejected, and a rejected close looks exactly like
    a closed position if you only read the exit code
  - closes both legs as ONE atomic mleg order, so there is never a moment
    holding a naked short
  - one close per leg pair, however many journal rows describe it
  - reads the closing fill back and journals the realised P&L from it

    python scripts/close_all.py            # show what it would close
    python scripts/close_all.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from agent import journal, reconcile  # noqa: E402
from agent.data import Market  # noqa: E402
from agent.executor import AlpacaMCP, new_client_order_id  # noqa: E402

REASON = "manual close - agent stopped"


async def run(apply: bool) -> int:
    if (os.getenv("ALPACA_PAPER_TRADE") or "").lower() != "true":
        print("refusing: ALPACA_PAPER_TRADE is not true")
        return 2

    rows = journal.open_spreads()
    if not rows:
        print("nothing open in the journal")
        return 0

    market = Market()
    if not market.is_market_open():
        print("market is closed - Alpaca rejects option orders outside regular "
              "hours. Run this again once it opens.")
        for r in rows:
            print("   would close %-5s %-20s / %-20s x%s"
                  % (r.get("underlying"), r.get("short_symbol"),
                     r.get("long_symbol"), r.get("contracts")))
        return 1

    async with AlpacaMCP() as mcp:
        state = await reconcile.fetch_broker_state(mcp)
        if not state.reachable:
            print("broker unreachable (%s) - refusing to guess" % state.error)
            return 2

        done: set = set()
        sent = 0
        for r in rows:
            short, long = r.get("short_symbol"), r.get("long_symbol")
            pair = (short, long)
            if pair in done:
                continue
            sp, lp = state.legs.get(short), state.legs.get(long)
            if not sp or not lp:
                print("   skip %-20s - the broker does not hold both legs" % short)
                continue
            try:
                unreal = float(sp.get("unrealized_pl") or 0) + \
                         float(lp.get("unrealized_pl") or 0)
            except (TypeError, ValueError):
                unreal = None
            qty = int(r.get("contracts") or 0)
            print("   close %-5s %-20s / %-20s x%-3d  unrealised %+.2f"
                  % (r.get("underlying"), short, long, qty, unreal or 0))
            if not apply:
                done.add(pair)
                continue
            res = await mcp.close_credit_spread(
                short, long, qty, client_order_id=new_client_order_id("close"))
            if not res.ok:
                print("      FAILED: %s" % res.error)
                continue
            done.add(pair)
            sent += 1
            journal.close_order(r.get("alpaca_order_id") or "", unreal, REASON,
                                row_id=r.get("id"),
                                close_order_id=(res.order or {}).get("id"))
            print("      submitted %s" % (res.order or {}).get("id"))

        if not apply:
            print("dry run - pass --apply to close them")
            return 0

        # Replace the unrealised estimates with what the closes actually filled.
        for note in await reconcile.sync_fills(mcp):
            print("   fills: %s" % note)
        print("closed %d spread(s)" % sent)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually send the closes")
    return asyncio.run(run(ap.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
