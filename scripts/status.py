"""Is the agent running by itself, and has it actually traded?

Two questions the journal alone cannot answer. The journal records what the
agent did; it does not record whether anything *invoked* it. So this asks
GitHub what ran, and separates a scheduled tick from a button press - which
is the whole difference between "it works" and "it works unattended".

    python scripts/status.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = os.getenv("VETOED_REPO", "Chong1120/Vetoed")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "journal", "trades.db")
RUNS = "https://api.github.com/repos/%s/actions/workflows/agent.yml/runs?per_page=30"


def clocks() -> None:
    now = datetime.now(timezone.utc)
    ny = now - timedelta(hours=4)          # EDT; the agent asks Alpaca, not this
    print("  now      %s UTC   |   %s New York   |   %s Malaysia"
          % (now.strftime("%a %H:%M"), ny.strftime("%H:%M"),
             (now + timedelta(hours=8)).strftime("%H:%M")))
    open_ = ny.weekday() < 5 and "09:30" <= ny.strftime("%H:%M") < "16:00"
    print("  market   %s" % ("OPEN" if open_ else "closed"))


def github() -> None:
    req = urllib.request.Request(RUNS % REPO,
                                 headers={"User-Agent": "vetoed-status"})
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=20) as fh:
            runs = json.load(fh).get("workflow_runs", [])
    except Exception as exc:                       # offline, rate limited, private
        print("  could not reach GitHub: %s" % exc)
        return

    sched = [r for r in runs if r["event"] == "schedule"]
    print("  scheduled runs (ran with nobody watching): %d" % len(sched))
    if not sched:
        print("    none yet - every run so far was a button press")
    for r in sched[:3]:
        print("    #%-4s %s  %s" % (r["run_number"], r["created_at"],
                                    r["conclusion"] or r["status"]))
    print("  most recent runs:")
    for r in runs[:4]:
        kind = "SCHEDULED" if r["event"] == "schedule" else "manual"
        print("    #%-4s %-10s %s  %s" % (r["run_number"], kind, r["created_at"],
                                          r["conclusion"] or r["status"]))


def journal() -> None:
    if not os.path.exists(DB):
        print("  no journal at %s" % DB)
        return
    c = sqlite3.connect(DB)
    real = c.execute("select count(*) from orders where status <> 'dry_run'").fetchone()[0]
    dry = c.execute("select count(*) from orders where status = 'dry_run'").fetchone()[0]
    print("  orders: %d submitted to Alpaca, %d dry run" % (real, dry))
    if not real:
        print("    nothing has been sent to the broker yet")
    for r in c.execute("select ts,underlying,kind,contracts,status,alpaca_order_id "
                       "from orders order by id desc limit 3"):
        print("    %s  %s %s x%s  %s  %s"
              % (r[0][:16], r[1], r[2], r[3], r[4], r[5] or "-"))

    print("  most recent decisions:")
    for r in c.execute("select ts,underlying,outcome,risk_vetoes from decisions "
                       "order by id desc limit 3"):
        vetoes = json.loads(r[3] or "[]")
        print("    %s  %-5s %-9s %s"
              % (r[0][:16], r[1] or "-", r[2] or "-",
                 "; ".join(vetoes) if vetoes else ""))


def main() -> int:
    print("\n" + "-" * 74)
    clocks()
    print("-" * 74)
    print("  DOES IT RUN ITSELF?")
    github()
    print("-" * 74)
    print("  HAS IT TRADED?")
    journal()
    print("-" * 74 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
