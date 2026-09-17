"""Write Alpaca's fills into rows journalled before the agent recorded them.

Not part of the cycle. The cycle now reads every fill it sends (see
reconcile.sync_fills), but rows written before that only ever held the
screener's quote, and their closes carried no order id to look up. So this
matches them against Alpaca's own order history, once, and corrects four
things to what the account actually shows:

  credit / fill price  the net credit Alpaca filled at, not the quote
  realised P&L         credit in minus debit out, from the two fills
  exit reason          where reconcile could only say "no longer held", the
                       cycle note that recorded the real exit
  duplicates           a second row for an order another row already records
                       is marked 'duplicate', not deleted - a deleted row is
                       restored by the next merge from a session that holds it

Read-only at the broker: get_orders only. Nothing is sent.

    python scripts/backfill_fills.py            # show what would change
    python scripts/backfill_fills.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import journal, reconcile  # noqa: E402

NO_LONGER_HELD = "no longer held at the broker"


def _t(ts) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def broker_fills(orders: list) -> list[dict]:
    """Filled two-leg orders, each tagged as an open or a close."""
    out = []
    for o in orders:
        got = reconcile._filled(o)
        legs = o.get("legs") or []
        if not got or len(legs) != 2:
            continue
        intents = [str(l.get("position_intent") or "") for l in legs]
        if all(i.endswith("_to_open") for i in intents):
            side = "open"
        elif all(i.endswith("_to_close") for i in intents):
            side = "close"
        else:
            continue
        price, qty, at = got
        out.append({"id": o.get("id"), "client_order_id": o.get("client_order_id"),
                    "legs": frozenset(l.get("symbol") for l in legs),
                    "side": side, "price": price, "qty": qty, "at": _t(at)})
    out.sort(key=lambda f: f["at"])
    return out


def _reason_from_runs(runs: list, short_symbol: str, at: datetime) -> str | None:
    """The exit the cycle itself logged for this leg, near when the close filled."""
    for ts, note in runs:
        t = _t(ts)
        if at - timedelta(minutes=2) <= t <= at + timedelta(minutes=15):
            for part in (note or "").split(";"):
                part = part.strip()
                if part.startswith(short_symbol + ":"):
                    return part[len(short_symbol) + 1:].strip() or None
    return None


def plan(rows: list[dict], orders: list, runs: list) -> list[dict]:
    """Every correction needed to make the journal match Alpaca. Pure."""
    fills = broker_fills(orders)
    opens = [f for f in fills if f["side"] == "open"]
    closes = [f for f in fills if f["side"] == "close"]
    dead = set(journal.DEAD_STATUSES)
    live = [r for r in rows if str(r.get("status") or "").lower() not in dead]
    changes: list[dict] = []

    # Rows the agent placed claim their order first, so when an adopted row
    # describes the same order it is the adopted one marked duplicate.
    owner: dict = {}
    matched: list = []
    for r in sorted(live, key=lambda r: (not r.get("client_order_id"), r["id"])):
        legs = frozenset([r.get("short_symbol"), r.get("long_symbol")])
        op = None
        if r.get("alpaca_order_id"):
            op = next((f for f in opens if f["id"] == r["alpaca_order_id"]), None)
        if op is None and r.get("client_order_id"):
            op = next((f for f in opens
                       if f["client_order_id"] == r["client_order_id"]), None)
        if op is None and not r.get("client_order_id"):
            # Adopted: no id of its own, so the latest open of these exact legs
            # that had filled by the time it was adopted.
            before = [f for f in opens if f["legs"] == legs and f["at"] <= _t(r["ts"])]
            op = before[-1] if before else None
        if op is None:
            continue
        if op["id"] in owner:
            changes.append({"row": r["id"], "field": "status", "old": r.get("status"),
                            "new": "duplicate",
                            "why": "same Alpaca order as row #%s" % owner[op["id"]]})
            continue
        owner[op["id"]] = r["id"]
        matched.append((r, op))

    used_close: set = set()
    for r, op in sorted(matched, key=lambda m: m[0]["id"]):
        credit = -op["price"]
        if credit <= 0:
            continue
        if (r.get("fill_price") is None or not r.get("filled_qty")
                or abs(float(r.get("credit") or 0) - credit) > 1e-6):
            changes.append({"row": r["id"], "field": "open_fill",
                            "old": r.get("credit"), "new": credit, "qty": op["qty"]})
        if not r.get("closed_ts"):
            continue
        # The close is the first closing fill of these legs after this open,
        # and before the same spread was opened again.
        reopen = next((f["at"] for f in opens
                       if f["legs"] == op["legs"] and f["at"] > op["at"]), None)
        cl = next((f for f in closes if f["legs"] == op["legs"] and f["at"] > op["at"]
                   and (reopen is None or f["at"] < reopen)
                   and f["id"] not in used_close), None)
        if cl is None:
            continue
        used_close.add(cl["id"])
        pnl = round((credit - cl["price"]) * 100 * op["qty"], 2)
        if not (r.get("close_order_id") == cl["id"]
                and r.get("close_fill_price") is not None
                and abs(float(r["close_fill_price"]) - cl["price"]) < 1e-6):
            changes.append({"row": r["id"], "field": "close_fill",
                            "close_order_id": cl["id"], "debit": cl["price"],
                            "at": cl["at"].isoformat(),
                            "old": r.get("realised_pnl"), "new": pnl})
        if r.get("exit_reason") == NO_LONGER_HELD:
            reason = _reason_from_runs(runs, r.get("short_symbol"), cl["at"])
            if reason:
                changes.append({"row": r["id"], "field": "exit_reason",
                                "old": r.get("exit_reason"), "new": reason})
    return changes


def apply(changes: list[dict], path: str) -> None:
    order = {"status": 0, "open_fill": 1, "close_fill": 2, "exit_reason": 3}
    for ch in sorted(changes, key=lambda c: (order[c["field"]], c["row"])):
        if ch["field"] == "open_fill":
            journal.record_open_fill(ch["row"], ch["new"], ch["qty"], path=path)
            continue
        if ch["field"] == "close_fill":
            with journal.connect(path) as c:
                c.execute("UPDATE orders SET close_order_id=? WHERE id=?",
                          (ch["close_order_id"], ch["row"]))
            journal.record_close_fill(ch["row"], ch["debit"], ch["at"], path=path)
            continue
        with journal.connect(path) as c:
            c.execute("UPDATE orders SET %s=? WHERE id=?" % ch["field"],
                      (ch["new"], ch["row"]))


async def fetch_orders() -> list:
    from agent import executor
    out, after = [], "2026-01-01T00:00:00Z"
    async with executor.AlpacaMCP() as mcp:
        while True:
            page = reconcile._as_list(await mcp.call("get_orders", {
                "status": "closed", "limit": 500, "after": after,
                "direction": "asc", "nested": True}), "orders")
            out += page
            if len(page) < 500:
                return out
            after = page[-1]["submitted_at"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--db", default=journal.DB_PATH)
    args = ap.parse_args()

    journal.init(args.db)
    orders = asyncio.run(fetch_orders())
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM orders ORDER BY id")]
    runs = [(r["ts"], r["note"]) for r in
            con.execute("SELECT ts, note FROM runs WHERE note IS NOT NULL ORDER BY ts")]
    con.close()

    changes = plan(rows, orders, runs)
    print("Alpaca: %d filled two-leg orders" % len(broker_fills(orders)))
    for ch in changes:
        extra = ch.get("why") or ("close %s at %.4f" % (ch["close_order_id"][:8], ch["debit"])
                                  if ch["field"] == "close_fill" else "")
        print("  #%-4s %-12s %s -> %s  %s" % (ch["row"], ch["field"], ch["old"], ch["new"], extra))
    print("%d change(s)" % len(changes))
    if not args.apply:
        print("dry run - pass --apply to write")
        return 0
    apply(changes, args.db)
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
