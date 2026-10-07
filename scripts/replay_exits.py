"""Replay the trades the agent really made, under different exit rules.

The month closed flat on a 72% win rate because the exits capped the wins at
half the credit and let the losses run to roughly a third of the width. That
is an arithmetic claim about averages. This settles it on the actual
positions: every closed spread is re-run hour by hour against Alpaca's own
option bars, and each candidate rule is scored on the same 68 trades.

WHAT IS REAL HERE
  - entry credit, contracts and dates come from the journal: what was filled
  - the price path is Alpaca's hourly option bars for both legs
  - expiry settles against the underlying's close that day

WHAT IS APPROXIMATE, AND WHY IT IS STILL FAIR
  - an hourly close is a traded print, not the quote the agent would have hit,
    so every variant is slightly optimistic about its exit price - including
    the one that reproduces the live rules. The comparison between rules is
    like-for-like; the absolute numbers are a ceiling.
  - the live agent looked every 10 minutes, this looks hourly, so a fast
    round trip through a threshold can be missed.
  - no slippage is charged on the simulated close. The live fills gave away
    8.9% of premium, so a rule that closes more often is flattered here.

    python scripts/replay_exits.py
    python scripts/replay_exits.py --json out.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = "https://data.alpaca.markets"
DEAD = ('analysis_only', 'canceled', 'cancelled', 'rejected', 'expired',
        'dry_run', 'failed', 'not_filled', 'duplicate')


def creds() -> dict:
    env = {}
    for line in open(os.path.join(ROOT, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}


def get(url: str, headers: dict):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=60))


def option_bars(symbols: list[str], start: str, end: str, headers: dict) -> dict:
    """Hourly bars for many option symbols, paginated."""
    out: dict = {}
    for i in range(0, len(symbols), 20):
        batch, token = symbols[i:i + 20], None
        while True:
            q = {"symbols": ",".join(batch), "timeframe": "1Hour",
                 "start": start, "end": end, "limit": 10000}
            if token:
                q["page_token"] = token
            d = get(DATA + "/v1beta1/options/bars?" + urllib.parse.urlencode(q), headers)
            for sym, bars in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = d.get("next_page_token")
            if not token:
                break
    for sym in out:
        out[sym].sort(key=lambda b: b["t"])
    return out


def underlying_close(sym: str, day: dt.date, headers: dict):
    for feed in ("iex", "sip"):
        q = {"timeframe": "1Day", "start": str(day),
             "end": str(day + dt.timedelta(days=1)), "feed": feed, "limit": 5}
        try:
            b = get(DATA + "/v2/stocks/%s/bars?" % sym + urllib.parse.urlencode(q),
                    headers).get("bars") or []
        except Exception:
            continue
        if b:
            return b[0]["c"]
    return None


def expiry_of(short_symbol: str, underlying: str) -> dt.date:
    body = short_symbol[len(underlying):]
    return dt.datetime.strptime(body[:6], "%y%m%d").date()


def path_for(trade: dict, bars: dict) -> list[tuple[str, float]]:
    """(timestamp, cost to close the spread) through the life of the trade.

    Closing a credit spread costs short price minus long price. Both legs are
    aligned on the hours where BOTH printed - a bar for one leg alone says
    nothing about what the pair was worth.
    """
    s = {b["t"]: b["c"] for b in bars.get(trade["short_symbol"], [])}
    l = {b["t"]: b["c"] for b in bars.get(trade["long_symbol"], [])}
    return [(t, s[t] - l[t]) for t in sorted(set(s) & set(l)) if t >= trade["ts"]]


def simulate(trade: dict, path: list, settle: float | None,
             take: float | None, stop: float | None) -> tuple[float, str]:
    """P&L for one trade under one rule.

    take: close at this fraction of the credit kept (0.5 = the live rule).
          None means carry to expiry.
    stop: close when the loss reaches this multiple of the credit.
          None means no stop - the spread's own long leg is the cap.
    """
    credit, qty = trade["credit"], trade["qty"]
    gross = credit * 100 * qty
    for _, cost in path:
        pnl = (credit - cost) * 100 * qty
        if take is not None and pnl >= gross * take:
            return gross * take, "take profit"
        if stop is not None and pnl <= -gross * stop:
            return -gross * stop, "stop loss"
    if settle is None:
        return trade["actual"], "no data - actual kept"
    return (credit - settle) * 100 * qty, "expiry"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the full result here")
    args = ap.parse_args()
    H = creds()

    con = sqlite3.connect(os.path.join(ROOT, "journal", "trades.db"))
    con.row_factory = sqlite3.Row
    today = dt.date.today()
    trades = []
    for r in con.execute("select * from orders where closed_ts is not null order by ts"):
        d = dict(r)
        if d["status"] in DEAD or d["realised_pnl"] is None or not d["credit"]:
            continue
        exp = expiry_of(d["short_symbol"], d["underlying"])
        if exp >= today:
            continue                      # cannot be settled yet
        trades.append({
            "id": d["id"], "underlying": d["underlying"],
            "short_symbol": d["short_symbol"], "long_symbol": d["long_symbol"],
            "credit": float(d["credit"]),
            "qty": float(d["filled_qty"] or d["contracts"]),
            "width": abs(int(d["short_symbol"][-8:]) - int(d["long_symbol"][-8:])) / 1000.0,
            "is_call": d["short_symbol"][len(d["underlying"]) + 6] == "C",
            "strike": int(d["short_symbol"][-8:]) / 1000.0,
            "ts": d["ts"].replace("+00:00", "Z"), "expiry": exp,
            "actual": float(d["realised_pnl"]),
            "actual_reason": d["exit_reason"] or "",
        })
    print("replaying %d closed trades that have reached expiry" % len(trades))

    syms = sorted({t["short_symbol"] for t in trades} | {t["long_symbol"] for t in trades})
    start = min(t["ts"] for t in trades)[:10]
    end = str(max(t["expiry"] for t in trades) + dt.timedelta(days=1))
    print("fetching hourly bars for %d option symbols, %s to %s" % (len(syms), start, end))
    bars = option_bars(syms, start, end, H)
    print("  got bars for %d symbols" % len(bars))

    closes: dict = {}
    for t in trades:
        key = (t["underlying"], t["expiry"])
        if key not in closes:
            closes[key] = underlying_close(t["underlying"], t["expiry"], H)
        spot = closes[key]
        t["path"] = path_for(t, bars)
        if spot is None:
            t["settle"] = None
        else:
            intr = max(0.0, min(t["width"], spot - t["strike"])) if t["is_call"] \
                else max(0.0, min(t["width"], t["strike"] - spot))
            t["settle"] = intr
    missing = [t for t in trades if not t["path"]]
    if missing:
        print("  no usable price path for %d trade(s): %s"
              % (len(missing), ", ".join(str(t["id"]) for t in missing)))

    def score(take, stop):
        pnl = [simulate(t, t["path"], t["settle"], take, stop)[0] for t in trades]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        return {
            "take": take, "stop": stop, "total": round(sum(pnl)),
            "win_rate": round(100 * len(wins) / len(pnl), 1),
            "avg_win": round(sum(wins) / len(wins)) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
            "worst": round(min(pnl)),
        }

    actual_total = round(sum(t["actual"] for t in trades))
    print()
    print("what these %d trades actually returned: %+d" % (len(trades), actual_total))
    print()
    grid = []
    for take in (0.25, 0.4, 0.5, 0.6, 0.7, 0.8, None):
        for stop in (1.0, 1.5, 2.0, 3.0, None):
            grid.append(score(take, stop))
    grid.sort(key=lambda g: -g["total"])
    fmt = lambda v: "hold" if v is None else ("%g" % v)
    print("%-7s %-6s %9s %8s %9s %9s %9s" %
          ("take", "stop", "total", "win%", "avg win", "avg loss", "worst"))
    for g in grid:
        print("%-7s %-6s %9d %7.1f%% %9d %9d %9d" %
              (fmt(g["take"]), fmt(g["stop"]), g["total"], g["win_rate"],
               g["avg_win"], g["avg_loss"], g["worst"]))
    if args.json:
        json.dump({"actual": actual_total, "n": len(trades), "grid": grid},
                  open(args.json, "w"), indent=1)
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
