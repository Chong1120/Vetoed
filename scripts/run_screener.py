"""Dev tool: run the deterministic screener and print the shortlist.

    .venv\\Scripts\\python.exe -m scripts.run_screener

Read-only. Places no orders. Safe to run at any time, including when the
market is closed (quotes will simply be the last known ones).
"""

import sys
import time

from agent.data import Market
from agent.screener import screen


def main() -> int:
    t0 = time.time()
    market = Market()
    print("options feed   : %s" % market.feed.value)
    print("market open    : %s" % market.is_market_open())
    print("screening ...")

    shortlist, ctx = screen(market)
    elapsed = time.time() - t0

    print()
    for sym, d in ctx["underlyings"].items():
        if "error" in d:
            print("%-5s ERROR %s" % (sym, d["error"][:70]))
            continue
        print("%-5s spot=%-9.2f sma20=%-9.2f trend=%-6s atm_iv=%-8s rv20=%-8s "
              "iv/rv=%-6s rows=%d" % (
                  sym, d["spot"], d["sma20"],
                  "UP" if d["above_trend"] else "DOWN",
                  d["atm_iv"], d["realized_vol_20d"],
                  d["iv_vs_rv"], d["contracts_examined"]))

    print()
    print("candidates built : %d" % ctx["candidates_before_dedupe"])
    print("shortlist        : %d" % ctx["candidates_returned"])
    print("elapsed          : %.1fs" % elapsed)
    print()

    if not shortlist:
        print("NO CANDIDATES. Loosen filters or check market data.")
        return 1

    hdr = ("%-5s %-11s %-10s %3s %7s %7s %5s %6s %7s %5s %5s %7s %7s %6s" % (
        "SYM", "KIND", "EXPIRY", "DTE", "SHORT", "LONG", "W",
        "CREDIT", "MAXLOSS", "POP", "DELTA", "EV_RW", "EV_RN", "VRP"))
    print(hdr)
    print("-" * len(hdr))
    for c in shortlist:
        print("%-5s %-11s %-10s %3d %7.1f %7.1f %5.1f %6.2f %7.0f %5.2f %5.2f %7.2f %7.2f %6.2f" % (
            c.underlying, c.kind, c.expiry, c.dte, c.short_strike,
            c.long_strike, c.width, c.credit, c.max_loss,
            c.pop, c.short_delta, c.ev, c.ev_rn, c.vrp_edge))

    print()
    top = shortlist[0]
    print("TOP CANDIDATE, spelled out:")
    print("  SELL %s  (strike %.1f)" % (top.short_symbol, top.short_strike))
    print("  BUY  %s  (strike %.1f)  <- caps the loss" % (
        top.long_symbol, top.long_strike))
    print("  credit ~$%.2f/share = $%.0f per spread" % (top.credit, top.max_profit))
    print("  max loss $%.0f per spread, hard-capped" % top.max_loss)
    print("  short strike is %.2f%% OTM, delta %.2f, OI >= %d" % (
        top.distance_pct * 100, top.short_delta, top.min_open_interest))
    print("  model POP ~%.0f%% held to expiry, at %.1f%% realised vol; EV $%.2f"
          % (top.pop * 100, top.realized_vol * 100, top.ev))
    print("  same spread, same credit, at %.1f%% implied vol: EV $%.2f"
          % ((top.short_iv or 0) * 100, top.ev_rn))
    print("  volatility-gap signal (ev_rw - ev_rn): $%.2f per spread"
          % top.vrp_edge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
