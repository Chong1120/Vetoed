"""Wait for the market to open, close every spread, publish the journal.

The agent is stopped and the book still has positions, so this is the one
remaining job: close them at the next open, record what Alpaca actually
filled, and push the journal so the dashboard shows it.

It is deliberately dumb. It does not screen, judge, or decide anything - it
runs scripts/close_all.py, which refuses unless the config is paper and the
market is genuinely open, and closes each leg pair exactly once.

    python scripts/close_at_open.py                 # waits, then closes
    python scripts/close_at_open.py --deadline 8    # give up after 8 hours
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

# scripts/ is not a package, so load the closer by path - the same way the
# tests and the other scripts here load their neighbours.
_SPEC = importlib.util.spec_from_file_location(
    "close_all", os.path.join(ROOT, "scripts", "close_all.py"))
close_all = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(close_all)


def log(msg: str) -> None:
    print("%s  %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg),
          flush=True)


def market_open() -> bool:
    from agent.data import Market
    try:
        return Market().is_market_open()
    except Exception as exc:                              # noqa: BLE001
        log("clock unreadable (%s) - will ask again" % type(exc).__name__)
        return False


def publish() -> None:
    """Commit and push the journal, and nothing else."""
    def git(*a):
        return subprocess.run(("git",) + a, cwd=ROOT, capture_output=True, text=True)
    subprocess.run([sys.executable, "scripts/export_journal_json.py"],
                   cwd=ROOT, capture_output=True, text=True)
    git("add", "journal/trades.db", "journal/data.json")
    if not git("diff", "--cached", "--quiet").returncode:
        log("journal unchanged - nothing to publish")
        return
    git("commit", "-q", "-m",
        "Journal: close the book\n\nThe agent is stopped; scripts/close_at_open.py "
        "closed every remaining spread at\nthe open and recorded the fills Alpaca "
        "reported.\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>")
    for attempt in (1, 2, 3):
        if not git("push", "-q", "origin", "main").returncode:
            log("journal pushed")
            return
        log("push rejected (attempt %d) - rebasing" % attempt)
        git("pull", "--rebase", "-q", "origin", "main")
    log("could not push the journal - it is committed locally")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", type=float, default=10.0,
                    help="hours to wait before giving up")
    ap.add_argument("--poll", type=int, default=60, help="seconds between checks")
    args = ap.parse_args()

    give_up = time.time() + args.deadline * 3600
    log("waiting for the market to open (deadline %.1fh)" % args.deadline)
    while time.time() < give_up:
        if market_open():
            log("market open - closing every spread")
            rc = asyncio.run(close_all.run(apply=True))
            log("close_all exited %d" % rc)
            if rc == 0:
                publish()
                log("done")
                return 0
            log("close did not complete - will try again")
        time.sleep(args.poll)
    log("deadline reached without an open market - nothing was closed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
