"""
Read the decision journal. The one command to run after a trading session.

    python scripts/show_journal.py            last 10 cycles
    python scripts/show_journal.py --all      everything
    python scripts/show_journal.py --check    warnings only, exit 1 if any

WHY THIS EXISTS. A green workflow run is not evidence the agent worked. Twice
during development a cycle finished, journalled an order and reported success
while the judgement layer had silently degraded to arithmetic - once because a
Cloudflare block looked like a bad key, once because an undefined GitHub
variable expanded to an empty model name. Both were invisible in the run
status and legible only in `llm_error` here.

So this prints what actually happened, and `--check` names the things that are
easy to miss: fallbacks, uncertain orders, vetoes, and stale runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import journal  # noqa: E402

BAR = "-" * 78


def _ts(value: str, width: int = 16) -> str:
    return str(value or "").replace("T", " ").replace("+00:00", "")[:width]


def _money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    # format(), not %-formatting: the "," thousands separator is not a
    # %-format feature and raises ValueError there.
    return ("-${:,.2f}".format(abs(v))) if v < 0 else ("${:,.2f}".format(v))


def show(limit: int) -> dict:
    """Print the journal. Returns counters the --check pass reasons about."""
    journal.init()
    runs = journal.recent_runs(limit)
    decisions = journal.recent_decisions(limit * 3)
    orders = journal.all_orders(limit * 3)
    curve = journal.equity_curve()
    open_rows = journal.open_spreads()

    stats = {"fallbacks": 0, "llm_answers": 0, "uncertain": 0, "vetoes": 0,
             "dry_runs": 0, "live_orders": 0, "errors": 0}

    # ---- account ---------------------------------------------------------
    print("\n" + BAR)
    print("  ACCOUNT")
    print(BAR)
    if curve:
        start, latest = curve[0]["equity"], curve[-1]["equity"]
        change = latest - start
        pct = (change / start * 100) if start else 0.0
        print("  equity        %s   (started %s, %s%.2f%%)"
              % (_money(latest), _money(start), "+" if change >= 0 else "", pct))
    else:
        print("  equity        no snapshots yet")
    realised = sum(float(o["realised_pnl"] or 0) for o in orders
                   if o.get("realised_pnl") is not None)
    closed = [o for o in orders if o.get("realised_pnl") is not None]
    print("  realised P&L  %s across %d closed trade(s)" % (_money(realised), len(closed)))
    print("  open now      %d spread(s)" % len(open_rows))

    # ---- cycles ----------------------------------------------------------
    print("\n" + BAR)
    print("  CYCLES  (most recent first)")
    print(BAR)
    if not runs:
        print("  none yet - the agent has never run")
    for r in runs:
        print("  #%-4s %s  market=%-6s  candidates=%-3s %s"
              % (r["id"], _ts(r["ts"]),
                 "open" if r["market_open"] else "CLOSED",
                 r["candidates"], "HALTED" if r["halted"] else ""))
        if r.get("note"):
            for part in str(r["note"]).split("; "):
                if part.strip():
                    print("        %s" % part.strip()[:100])

    # ---- decisions -------------------------------------------------------
    print("\n" + BAR)
    print("  DECISIONS  (who actually chose, and why)")
    print(BAR)
    for d in decisions[:limit]:
        rationale = d.get("llm_rationale") or ""
        # A genuine model answer leaves raw output behind. Matching only on
        # "Deterministic selection" mislabels the cases that never reached a
        # model at all - no key configured, or a shortlist of zero.
        fell_back = ("Deterministic selection" in rationale
                     or not (d.get("llm_raw") or "").strip())
        if d.get("llm_action"):
            stats["fallbacks" if fell_back else "llm_answers"] += 1
        if d.get("llm_error"):
            stats["errors"] += 1
        if (d.get("risk_vetoes") or "[]") not in ("[]", "", "null"):
            stats["vetoes"] += 1

        who = "ARITHMETIC" if fell_back else "MODEL"
        print("  #%-4s %s  %-11s %-10s %s"
              % (d["id"], _ts(d["ts"]), d.get("llm_action") or "-",
                 d.get("outcome") or "-", who))
        if rationale:
            print("        %s" % rationale[:110])
        if d.get("llm_error"):
            print("        ERROR: %s" % str(d["llm_error"])[:110])
        vetoes = d.get("risk_vetoes")
        if vetoes and vetoes not in ("[]", "null"):
            try:
                for v in json.loads(vetoes):
                    print("        VETO: %s" % str(v)[:100])
            except (json.JSONDecodeError, TypeError):
                pass

    # ---- orders ----------------------------------------------------------
    print("\n" + BAR)
    print("  ORDERS")
    print(BAR)
    if not orders:
        print("  none")
    for o in orders[:limit]:
        status = str(o.get("status") or "").lower()
        if status == "dry_run":
            stats["dry_runs"] += 1
        elif status == "uncertain":
            stats["uncertain"] += 1
        else:
            stats["live_orders"] += 1
        pnl = ("  realised %s" % _money(o["realised_pnl"])) \
            if o.get("realised_pnl") is not None else ""
        print("  #%-4s %s  %-5s x%-3s %-11s credit %s  max loss %s%s"
              % (o["id"], _ts(o["ts"]), o.get("underlying") or "?",
                 o.get("contracts") or "?", status,
                 _money(o.get("credit")), _money(o.get("max_loss_total")), pnl))
    return stats


def check(stats: dict) -> int:
    """The things a green workflow run will not tell you."""
    print("\n" + BAR)
    print("  CHECK")
    print(BAR)
    problems = []

    if stats["fallbacks"] and not stats["llm_answers"]:
        problems.append(
            "EVERY decision came from arithmetic, not the model. The judgement\n"
            "     layer is configured but never answering. Run:\n"
            "         python scripts/check_llm.py")
    elif stats["fallbacks"]:
        problems.append(
            "%d decision(s) fell back to arithmetic while %d came from the\n"
            "     model. Intermittent - look at the ERROR lines above."
            % (stats["fallbacks"], stats["llm_answers"]))

    if stats["uncertain"]:
        problems.append(
            "%d order(s) are UNCERTAIN - we do not know whether Alpaca received\n"
            "     them. They count as live risk until the next cycle reconciles\n"
            "     against the broker. Check positions in the Alpaca dashboard."
            % stats["uncertain"])

    if stats["live_orders"] == 0 and stats["dry_runs"]:
        problems.append(
            "%d dry-run order(s) and no live ones. Nothing has reached the\n"
            "     broker. Scheduled ticks pass --live; manual runs do not\n"
            "     unless you pick 'live'." % stats["dry_runs"])

    if not problems:
        print("  nothing to flag.")
        print("    model answered   : %d" % stats["llm_answers"])
        print("    live orders      : %d" % stats["live_orders"])
        print("    risk vetoes      : %d" % stats["vetoes"])
        return 0

    for i, p in enumerate(problems, 1):
        print("  %d. %s" % (i, p))
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Read the Vetoed decision journal")
    ap.add_argument("--all", action="store_true", help="no limit")
    ap.add_argument("--check", action="store_true",
                    help="only the warnings; exit 1 if any")
    args = ap.parse_args()

    limit = 10_000 if args.all else 10
    if args.check:
        # Still needs the data, so gather it quietly.
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            stats = show(limit)
        return check(stats)

    stats = show(limit)
    rc = check(stats)
    print("\n  journal: %s" % journal.DB_PATH)
    print("  updated: %s UTC\n"
          % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
