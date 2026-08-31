"""Reconcile the journal against the broker, once, for damage already done.

Not part of the cycle. This exists because a parsing bug in reconcile.py read
the MCP server's {"result": [...]} envelope as an empty position list, so a
filled spread was marked closed while it was still open at Alpaca, and the
following cycles re-attempted it and were refused by the duplicate guard.

The code is fixed. These rows are not: they were written wrong, and no future
cycle repairs them, because a closed row is never reconsidered. The broker is
authoritative, so the journal is corrected to match it.

    python scripts/repair_journal.py --dry-run
    python scripts/repair_journal.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import executor, journal, reconcile


async def broker_legs() -> dict:
    async with executor.AlpacaMCP() as mcp:
        state = await reconcile.fetch_broker_state(mcp)
    if not state.reachable:
        raise SystemExit("broker unreachable (%s) - refusing to guess" % state.error)
    return state.legs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    legs = asyncio.run(broker_legs())
    print("broker holds %d option leg(s):" % len(legs))
    for sym in sorted(legs):
        print("   %s  qty %s" % (sym, legs[sym].get("qty")))

    import sqlite3
    con = sqlite3.connect(journal.DB_PATH)
    con.row_factory = sqlite3.Row

    rows = list(con.execute("SELECT * FROM orders ORDER BY id"))
    fixes: list[tuple[str, int, str]] = []

    # A broker position belongs to exactly ONE journal row: the one that
    # carries an alpaca_order_id. Several rows here name the same strikes,
    # because the same spread was re-attempted every cycle while the journal
    # wrongly showed nothing open - but only the first reached Alpaca. The
    # rest were refused by the duplicate guard and never existed as orders.
    # Matching on symbols alone would resurrect all of them as open positions.
    for r in rows:
        legs_held = r["short_symbol"] in legs and r["long_symbol"] in legs
        reached_broker = bool(r["alpaca_order_id"])
        closed = r["closed_ts"] is not None
        status = (r["status"] or "").lower()

        if reached_broker and legs_held:
            if closed:
                fixes.append(("REOPEN", r["id"],
                              "held at broker but journal says closed"))
            elif status not in ("filled", "pending_new", "new", "accepted"):
                fixes.append(("MARK-FILLED", r["id"],
                              "held at broker, journal status %r" % status))
        elif not reached_broker and status == "uncertain" and not closed:
            fixes.append(("NOT-FILLED", r["id"],
                          "no broker order id - never reached Alpaca"))

    if not fixes:
        print("\nnothing to correct - journal already matches the broker")
        return 0

    print("\n%d correction(s):" % len(fixes))
    for kind, oid, why in fixes:
        print("   %-12s order #%-3s %s" % (kind, oid, why))

    if not args.apply:
        print("\ndry run - pass --apply to write")
        return 0

    for kind, oid, _ in fixes:
        if kind == "REOPEN":
            con.execute("UPDATE orders SET closed_ts=NULL, realised_pnl=NULL, "
                        "status='filled' WHERE id=?", (oid,))
        elif kind == "MARK-FILLED":
            con.execute("UPDATE orders SET status='filled' WHERE id=?", (oid,))
        elif kind == "NOT-FILLED":
            con.execute("UPDATE orders SET status='not_filled' WHERE id=?", (oid,))
    con.commit()
    print("\napplied. the journal now matches the broker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
