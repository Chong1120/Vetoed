"""
Preflight for the judgement layer. Proves an LLM key works BEFORE the agent
depends on it during a market session.

    python scripts/check_llm.py
    python scripts/check_llm.py --model Qwen/Qwen2.5-7B-Instruct

Read-only and free of market data: it sends one synthetic shortlist through
the real prompt and the real validator, and reports what came back. No broker
connection, no order path, nothing journaled.

Worth running because a misconfigured key fails SILENTLY in production - the
agent falls back to deterministic selection and keeps trading, which is the
correct behaviour but means a broken key looks like a working agent until you
read the journal.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv                                  # noqa: E402

from agent import brain                                         # noqa: E402

# One plausible candidate. Real shape, invented numbers - the point is to
# exercise the prompt and the validator, not to evaluate a trade.
SHORTLIST = [{
    "underlying": "AAPL", "kind": "put_credit", "expiry": "2026-09-11",
    "dte": 12, "short_symbol": "AAPL260911P00310000",
    "long_symbol": "AAPL260911P00305000", "short_strike": 310.0,
    "long_strike": 305.0, "width": 5.0, "credit": 0.94, "max_profit": 94.0,
    "max_loss": 406.0, "pop": 0.82, "pop_rn": 0.76, "ev": 28.59,
    "ev_rn": -13.85, "vrp_edge": 42.45, "short_delta": -0.24,
    "short_iv": 0.2383, "realized_vol": 0.1891, "distance_pct": 0.031,
    "min_open_interest": 1019,
}, {
    "underlying": "SPY", "kind": "put_credit", "expiry": "2026-09-11",
    "dte": 12, "short_symbol": "SPY260911P00758000",
    "long_symbol": "SPY260911P00757000", "short_strike": 758.0,
    "long_strike": 757.0, "width": 1.0, "credit": 0.23, "max_profit": 23.0,
    "max_loss": 77.0, "pop": 0.78, "pop_rn": 0.72, "ev": 1.61,
    "ev_rn": -2.68, "vrp_edge": 4.29, "short_delta": -0.24,
    "short_iv": 0.111, "realized_vol": 0.1038, "distance_pct": 0.0148,
    "min_open_interest": 900,
}]

CONTEXT = {"underlyings": {
    "AAPL": {"spot": 319.92, "sma20": 309.80, "above_trend": True,
             "atm_iv": 0.2383, "realized_vol_20d": 0.1891, "iv_vs_rv": 1.26,
             "contracts_examined": 198}},
    "feed": "indicative"}
ACCOUNT = {"equity": 100000.0, "day_pnl": 0.0, "open_positions": 0}


def main() -> int:
    ap = argparse.ArgumentParser(description="Test the LLM decision layer")
    ap.add_argument("--model", help="override FEATHERLESS_MODEL for this run")
    args = ap.parse_args()

    load_dotenv()
    if args.model:
        os.environ["FEATHERLESS_MODEL"] = args.model
        brain.FEATHERLESS_MODEL = args.model

    print("=" * 70)
    provider = brain.resolve_provider()
    print("  provider resolved : %s" % provider)
    if provider == "featherless":
        key = os.getenv("FEATHERLESS_API_KEY", "")
        print("  model             : %s" % brain.FEATHERLESS_MODEL)
        print("  key               : %d chars, starts %s..."
              % (len(key), key[:4]))
    elif provider == "anthropic":
        print("  model             : %s" % brain.ANTHROPIC_MODEL)
    print("=" * 70)

    if provider == "none":
        print("\nNo LLM provider configured.\n")
        print("The agent still runs - brain.py falls back to deterministic")
        print("selection and every risk gate is unchanged - but the judgement")
        print("layer is not exercised, and the journal will say so.\n")
        print("To enable Featherless, put this in .env:")
        print("    FEATHERLESS_API_KEY=your_key_here")
        return 1

    print("\nSending one synthetic shortlist through the real prompt...\n")
    started = time.time()
    decision = brain.decide(SHORTLIST, CONTEXT, ACCOUNT, [], use_llm=True)
    elapsed = time.time() - started

    print("  action      : %s" % decision.action)
    print("  candidate_id: %s" % decision.candidate_id)
    print("  confidence  : %.2f" % decision.confidence)
    print("  elapsed     : %.1fs" % elapsed)
    print("  rationale   : %s" % (decision.rationale or "")[:200])
    if decision.error:
        print("  error       : %s" % decision.error[:400])

    # Three outcomes, not two. An earlier version of this script conflated the
    # last two and reported success on a response that had been rejected.
    fell_back = "Deterministic selection" in (decision.rationale or "")
    answered_but_invalid = (not fell_back) and bool(decision.error)

    print("\n" + "=" * 70)
    if fell_back:
        print("  RESULT: the call failed - fell back to deterministic selection")
        print("=" * 70)
        print("\nThe LLM did not answer. The agent would still trade, using")
        print("arithmetic instead of judgement. Common causes:\n")
        print("  401        -> key wrong, or not yet activated")
        print("  403 + 1010 -> Cloudflare browser-signature ban; the client")
        print("                must send a User-Agent (brain.USER_AGENT)")
        print("  404        -> model not available on your plan;")
        print("                try --model Qwen/Qwen2.5-7B-Instruct")
        print("  429        -> out of credits or rate limited")
        print("  timeout    -> model too large or cold; try a smaller one")
        return 1

    if answered_but_invalid:
        print("  RESULT: the model answered, but the response was REJECTED")
        print("=" * 70)
        print("\nThis is the safety layer working - a malformed answer cannot")
        print("trade. But the judgement layer is not usable while it happens")
        print("every time.\n")
        print("  error: %s" % decision.error[:300])
        if decision.raw:
            print("\n  the model actually said:\n")
            print(decision.raw[:400])
        print("\nUsually the model ignored the required JSON shape. Try a")
        print("stronger instruct model:")
        print("  python scripts/check_llm.py --model Qwen/Qwen2.5-72B-Instruct")
        return 1

    print("  RESULT: the model answered and the response validated")
    print("=" * 70)
    if decision.raw:
        print("\nRaw model output (first 500 chars):\n")
        print(decision.raw[:500])
    print("\nThe judgement layer is live. Its choice still passes through")
    print("validate() and every risk gate before any order is sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
