"""Re-run the real trades against the marks the agent actually saw.

scripts/replay_exits.py had to price the spread from last-trade prints,
because Alpaca sells no historical option quotes on this plan - and those
prints are stale enough to show a position the agent closed for +$1,148 as a
stop-out at -$4,312. This does not need them.

Every cycle the agent wrote Alpaca's own mark for every leg it held into
broker_positions, and every cycle it committed the journal to git. So the
whole month of marks is in the repository's history, at the ten-minute cadence
the agent acted on, and the sum of a spread's two legs is exactly the
`unreal` that manage_positions tested against its thresholds.

WHAT THIS CAN AND CANNOT ANSWER
  - while a position was open, the marks are the real ones. A stop level can
    be checked against them directly.
  - after the agent closed a position, there are no more marks. A rule that
    would have held longer is settled at expiry instead, which is exact but
    blind to anything that happened in between.
  - a take-profit ABOVE 50% cannot be evaluated at all: the agent always
    closed at 50%, so there is no record of what the position did next. That
    is a limit of the data, not of the method.

    python scripts/replay_marks.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def mark_history(verbose: bool = True) -> dict:
    """{symbol: {ts: unrealised}} from every journal commit in the history."""
    shas = subprocess.run(["git", "log", "--format=%H", "--", "journal/trades.db"],
                          cwd=ROOT, capture_output=True, text=True).stdout.split()
    marks: dict = {}
    tmp = os.path.join(tempfile.mkdtemp(), "h.db")
    for i, sha in enumerate(shas):
        blob = subprocess.run(["git", "cat-file", "-p", "%s:journal/trades.db" % sha],
                              cwd=ROOT, capture_output=True)
        if blob.returncode:
            continue
        with open(tmp, "wb") as fh:
            fh.write(blob.stdout)
        try:
            con = sqlite3.connect(tmp)
            for ts, sym, un in con.execute(
                    "select ts, symbol, unrealised from broker_positions"):
                if un is not None:
                    marks.setdefault(sym, {})[ts] = float(un)
            con.close()
        except sqlite3.Error:
            continue
        if verbose and i % 150 == 0:
            print("  %d/%d commits" % (i, len(shas)), flush=True)
    return marks


def underlying_close(sym: str, day: dt.date, H: dict):
    for feed in ("iex", "sip"):
        q = {"timeframe": "1Day", "start": str(day),
             "end": str(day + dt.timedelta(days=1)), "feed": feed, "limit": 5}
        try:
            b = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://data.alpaca.markets/v2/stocks/%s/bars?" % sym
                + urllib.parse.urlencode(q), headers=H), timeout=30)).get("bars") or []
        except Exception:
            continue
        if b:
            return b[0]["c"]
    return None


def load_trades(H: dict) -> list:
    con = sqlite3.connect(os.path.join(ROOT, "journal", "trades.db"))
    con.row_factory = sqlite3.Row
    today = dt.date.today()
    out, closes = [], {}
    for r in con.execute("select * from orders where closed_ts is not null order by ts"):
        d = dict(r)
        if d["status"] in DEAD or d["realised_pnl"] is None or not d["credit"]:
            continue
        u = d["underlying"]
        exp = dt.datetime.strptime(d["short_symbol"][len(u):][:6], "%y%m%d").date()
        qty = float(d["filled_qty"] or d["contracts"])
        width = abs(int(d["short_symbol"][-8:]) - int(d["long_symbol"][-8:])) / 1000.0
        strike = int(d["short_symbol"][-8:]) / 1000.0
        settle = None
        if exp < today:
            if (u, exp) not in closes:
                closes[(u, exp)] = underlying_close(u, exp, H)
            spot = closes[(u, exp)]
            if spot is not None:
                intr = (max(0.0, min(width, spot - strike))
                        if d["short_symbol"][len(u) + 6] == "C"
                        else max(0.0, min(width, strike - spot)))
                settle = (float(d["credit"]) - intr) * 100 * qty
        out.append({
            "id": d["id"], "u": u, "short": d["short_symbol"], "long": d["long_symbol"],
            "credit_total": float(d["credit"]) * 100 * qty,
            "max_loss": float(d["max_loss_total"] or 0),
            "ts": d["ts"], "closed_ts": d["closed_ts"], "expiry": exp,
            "actual": float(d["realised_pnl"]), "reason": d["exit_reason"] or "",
            "settle": settle,
        })
    return out


def attach_paths(trades: list, marks: dict) -> None:
    for t in trades:
        s, l = marks.get(t["short"], {}), marks.get(t["long"], {})
        pts = []
        for ts in sorted(set(s) & set(l)):
            if t["ts"] <= ts <= t["closed_ts"]:
                pts.append((ts, s[ts] + l[ts]))
        t["path"] = pts


def simulate(t: dict, take: float | None, stop: float | None):
    """P&L under one rule. Returns (pnl, how, observed) - observed is False when
    the answer rests on expiry settlement rather than on marks."""
    g = t["credit_total"]
    for ts, unreal in t["path"]:
        if stop is not None and unreal <= -g * stop:
            return -g * stop, "stop", True
        if take is not None and unreal >= g * take:
            return g * take, "take", True
    if t["settle"] is None:
        return t["actual"], "no settlement - actual kept", False
    return t["settle"], "expiry", False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    H = creds()
    print("reading the mark history out of the journal's git commits...")
    marks = mark_history()
    print("  marks for %d option symbols" % len(marks))
    trades = load_trades(H)
    attach_paths(trades, marks)
    usable = [t for t in trades if t["path"]]
    print("  %d closed trades, %d with a mark path (%d points median)"
          % (len(trades), len(usable),
             sorted(len(t["path"]) for t in usable)[len(usable) // 2] if usable else 0))

    print()
    print("=== DOES IT REPRODUCE WHAT REALLY HAPPENED? ===")
    print("Simulating the live rules (take 0.5, stop 2.0) on trades the agent")
    print("closed at its take-profit - those are the ones whose exit is fully observed.")
    tp = [t for t in usable if "take profit" in t["reason"]]
    err = []
    for t in tp:
        pnl, how, _ = simulate(t, 0.5, 2.0)
        err.append(pnl - t["actual"])
    if tp:
        print("  %d take-profit trades, simulated total %+d vs actual %+d (mean error %+.0f)"
              % (len(tp), sum(simulate(t, 0.5, 2.0)[0] for t in tp),
                 sum(t["actual"] for t in tp), sum(err) / len(err)))

    print()
    print("=== HOW DEEP DID THE LOSERS ACTUALLY GO? ===")
    print("Worst mark each trade reached while open, as a multiple of its credit.")
    for t in usable:
        t["mae"] = min((u for _, u in t["path"]), default=0) / t["credit_total"]
    bands = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)]
    for lo, hi in bands:
        grp = [t for t in usable if lo <= -t["mae"] < hi]
        if not grp:
            continue
        settled = [t for t in grp if t["settle"] is not None]
        print("  touched %.1f-%.1fx credit against: %2d trades | actual %+7d | if held to expiry %+7d"
              % (lo, hi, len(grp), sum(t["actual"] for t in grp),
                 sum(t["settle"] for t in settled) if settled else 0))

    print()
    print("=== STOP LEVELS, ON THE REAL MARKS ===")
    print("Take-profit stays at 0.5 (anything higher is unobservable - the agent")
    print("always closed there, so there is no record of what came next).")
    print("%-8s %9s %9s %8s %9s %9s" % ("stop", "total", "vs actual", "win%", "avg win", "worst"))
    actual_total = sum(t["actual"] for t in usable)
    rows = []
    for stop in (0.75, 1.0, 1.25, 1.5, 2.0, 3.0, None):
        res = [simulate(t, 0.5, stop) for t in usable]
        pnl = [r[0] for r in res]
        wins = [p for p in pnl if p > 0]
        rows.append({"stop": stop, "total": round(sum(pnl)),
                     "win": round(100 * len(wins) / len(pnl), 1),
                     "avg_win": round(sum(wins) / len(wins)) if wins else 0,
                     "worst": round(min(pnl)),
                     "from_marks": sum(1 for r in res if r[2])})
        print("%-8s %9d %9d %7.1f%% %9d %9d"
              % ("none" if stop is None else "%gx" % stop, rows[-1]["total"],
                 rows[-1]["total"] - actual_total, rows[-1]["win"],
                 rows[-1]["avg_win"], rows[-1]["worst"]))
    print("  actual, same %d trades: %+d" % (len(usable), actual_total))
    if args.json:
        json.dump({"actual": actual_total, "n": len(usable), "stops": rows},
                  open(args.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
