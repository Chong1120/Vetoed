"""
brain.py - the judgement layer. Claude reasons over the screener's shortlist.

CRITICAL DESIGN CONSTRAINT: the model SELECTS, it does not CONSTRUCT.

The response carries a `candidate_id` indexing into the shortlist the screener
already produced. The chosen candidate's legs are then verified against that
shortlist entry byte-for-byte. A hallucinated option symbol, an invented
strike, or a mutated leg cannot become an order - it fails validation and the
cycle records a no-trade. The model's freedom is exactly: "which of these N
pre-vetted, defined-risk spreads, if any, and how strongly do you believe it."

The model also returns `contracts`, because the schema calls for it, but that
number is ADVISORY ONLY and is discarded. risk.py sizes every position. When
the two disagree we log both, which makes for an honest audit trail.

Nothing from the MCP server or any other tool output is fed to the model. It
sees only the screener's own numeric candidates plus account-level scalars.
That keeps the prompt-injection surface at zero.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict

import anthropic
from dotenv import load_dotenv

MODEL = os.getenv("BRAIN_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You are the decision layer of an autonomous options-trading agent running on \
Alpaca paper trading. You are NOT the risk manager and you are NOT the \
position sizer - a separate deterministic module owns both and can veto you.

Your only job: given a pre-screened shortlist of defined-risk vertical CREDIT \
SPREADS, decide whether to open exactly one of them, or none at all.

Every candidate has already passed deterministic filters for liquidity, \
defined risk, delta band, expected value, and a floor on measured \
volatility risk premium. You cannot propose a spread \
that is not on the list, and you cannot alter a spread's legs or strikes.

How to think about it:
- A credit spread profits when the underlying does NOT move past the short \
strike. You are selling probability, not predicting direction.
- `vrp_edge` is the most important number on each candidate. It is the \
volatility risk premium in dollars: `ev` (this spread priced at 20-day \
REALISED volatility) minus `ev_rn` (the same spread priced at the \
market's IMPLIED volatility). Both run through one probability model, so \
the only thing separating them is the volatility - which is what makes \
the difference the premium being harvested rather than an artefact of \
comparing two different formulas. Every candidate here has already \
cleared $2.00 of it. Larger is better; one sitting near the floor is a \
trade the market is barely paying us for.
- `pop` is the REAL-WORLD probability of profit, computed from realised \
volatility. `pop_rn` is its risk-neutral counterpart, computed from \
implied volatility. `pop` exceeding `pop_rn` is that same edge expressed \
as a probability rather than as dollars.
- All of these are estimates from one quote snapshot, not guarantees. \
Check `quote_feed`: `indicative` means derived quotes rather than true \
NBBO, so treat the credit as approximate.
- Prefer candidates whose short strike sits comfortably out of the money \
relative to how much the underlying actually moves (compare atm_iv against \
realized_vol_20d - iv_vs_rv above 1.0 means options are pricing more movement \
than has recently occurred, which favours the premium seller).
- Fewer DTE means faster theta decay but sharper gamma risk.
- Concentration is real risk: if open positions already lean one direction, \
adding more of the same is worse than it looks.
- "no_trade" is a legitimate and often correct answer. A thin edge is not \
worth capital. Do not manufacture a trade to look busy.

Return ONLY the JSON object matching the schema. No prose, no markdown fences.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["open_spread", "no_trade"],
            "description": "open_spread to take a candidate, no_trade to pass",
        },
        "candidate_id": {
            "type": "integer",
            "description": "Index into the shortlist. -1 when action is no_trade.",
        },
        "symbol": {
            "type": "string",
            "description": "Underlying ticker of the chosen candidate, or empty.",
        },
        "legs": {
            "type": "array",
            "description": "Must exactly match the chosen candidate's legs.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                },
                "required": ["symbol", "side"],
                "additionalProperties": False,
            },
        },
        "contracts": {
            "type": "integer",
            "description": "Advisory only. The risk module decides actual size.",
        },
        "rationale": {
            "type": "string",
            "description": "2-4 sentences. Why this candidate, or why no trade.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0.",
        },
    },
    "required": ["action", "candidate_id", "symbol", "legs", "contracts",
                 "rationale", "confidence"],
    "additionalProperties": False,
}


@dataclass
class BrainDecision:
    action: str
    candidate_id: int
    symbol: str
    legs: list
    contracts: int
    rationale: str
    confidence: float
    raw: str = ""
    error: str = ""

    @property
    def wants_trade(self) -> bool:
        return self.action == "open_spread" and self.candidate_id >= 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


def no_trade(reason: str, raw: str = "", error: str = "") -> BrainDecision:
    return BrainDecision("no_trade", -1, "", [], 0, reason, 0.0, raw, error)


# --------------------------------------------------------------------------- #
# deterministic fallback - the agent runs with NO LLM at all
# --------------------------------------------------------------------------- #

FALLBACK_MIN_POP = 0.60     # don't take a coin-flip
FALLBACK_MIN_EV = 2.00      # dollars per spread, after the EV model


def deterministic_decide(shortlist: list[dict],
                         note: str = "LLM disabled") -> BrainDecision:
    """Rule-based selection: best expected value that clears a fixed bar.

    Used when no Anthropic key is configured, or when --no-llm is passed. The
    agent remains fully autonomous and fully safe - it simply stops exercising
    judgement and follows arithmetic instead. Every downstream risk gate is
    unchanged, so the safety properties do not depend on this choice.

    The shortlist arrives sorted by score (EV per dollar risked), so the first
    entry clearing the bar is the pick.
    """
    if not shortlist:
        return no_trade("screener returned no candidates")

    for i, c in enumerate(shortlist):
        pop = float(c.get("pop") or 0)
        ev = float(c.get("ev") or 0)
        if pop >= FALLBACK_MIN_POP and ev >= FALLBACK_MIN_EV:
            return BrainDecision(
                "open_spread", i, c["underlying"],
                [{"symbol": c["short_symbol"], "side": "sell"},
                 {"symbol": c["long_symbol"], "side": "buy"}],
                0,
                "Deterministic selection (%s): highest EV per dollar risked "
                "that clears POP >= %.2f and EV >= $%.2f. Chose %s %s %s/%s, "
                "%dDTE, POP %.2f, EV $%.2f."
                % (note, FALLBACK_MIN_POP, FALLBACK_MIN_EV, c["underlying"],
                   c["kind"], c["short_strike"], c["long_strike"], c["dte"],
                   pop, ev),
                round(min(pop, 0.99), 2))

    return no_trade(
        "Deterministic selection (%s): no candidate cleared POP >= %.2f and "
        "EV >= $%.2f. Sitting out." % (note, FALLBACK_MIN_POP, FALLBACK_MIN_EV))


# --------------------------------------------------------------------------- #
# defensive parsing
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> dict:
    """Parse model output into a dict, tolerating the usual failure modes.

    Structured outputs make fences unlikely, but this path also runs when the
    schema is unavailable or the model is swapped, so it stays defensive.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} span.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    return json.loads(cleaned[start:end + 1])


def validate(payload: dict, shortlist: list[dict]) -> BrainDecision:
    """Coerce and verify. The model may only pick a candidate, never invent one."""
    if not isinstance(payload, dict):
        return no_trade("model returned a non-object", error="type error")

    action = str(payload.get("action", "")).strip()
    if action not in ("open_spread", "no_trade"):
        return no_trade("unrecognised action %r" % action,
                        error="bad action: %r" % action)

    rationale = str(payload.get("rationale", ""))[:2000]
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if action == "no_trade":
        return BrainDecision("no_trade", -1, "", [], 0, rationale, confidence)

    try:
        cid = int(payload.get("candidate_id", -1))
    except (TypeError, ValueError):
        return no_trade("candidate_id not an integer",
                        error="bad candidate_id")

    if not (0 <= cid < len(shortlist)):
        return no_trade("candidate_id %d out of range (0..%d)"
                        % (cid, len(shortlist) - 1),
                        error="candidate_id out of range")

    chosen = shortlist[cid]

    # The legs the model echoed back must match the real candidate exactly.
    # This is what makes hallucinated contracts unexecutable.
    legs = payload.get("legs") or []
    got = {(str(l.get("symbol")), str(l.get("side")).lower())
           for l in legs if isinstance(l, dict)}
    expected = {(chosen["short_symbol"], "sell"), (chosen["long_symbol"], "buy")}
    if got != expected:
        return no_trade(
            "legs did not match candidate %d - refusing to execute" % cid,
            error="leg mismatch: model=%s expected=%s" % (sorted(got),
                                                          sorted(expected)))

    symbol = str(payload.get("symbol") or chosen["underlying"])
    if symbol != chosen["underlying"]:
        symbol = chosen["underlying"]  # trust the shortlist, not the model

    try:
        contracts = int(payload.get("contracts", 0))
    except (TypeError, ValueError):
        contracts = 0

    return BrainDecision("open_spread", cid, symbol,
                         [{"symbol": chosen["short_symbol"], "side": "sell"},
                          {"symbol": chosen["long_symbol"], "side": "buy"}],
                         contracts, rationale, confidence)


# --------------------------------------------------------------------------- #

def build_prompt(shortlist: list[dict], context: dict,
                 account: dict, open_positions: list[dict]) -> str:
    """Only screener-derived numbers and account scalars. No tool output."""
    slim = []
    for i, c in enumerate(shortlist):
        slim.append({
            "candidate_id": i,
            "underlying": c["underlying"],
            "kind": c["kind"],
            "expiry": c["expiry"],
            "dte": c["dte"],
            "short_symbol": c["short_symbol"],
            "long_symbol": c["long_symbol"],
            "short_strike": c["short_strike"],
            "long_strike": c["long_strike"],
            "width": c["width"],
            "credit": c["credit"],
            "max_profit": c["max_profit"],
            "max_loss": c["max_loss"],
            "pop": c["pop"],
            "pop_rn": c["pop_rn"],
            "ev": c["ev"],
            "ev_rn": c["ev_rn"],
            "vrp_edge": c["vrp_edge"],
            "short_delta": c["short_delta"],
            "short_iv": c["short_iv"],
            "realized_vol": c["realized_vol"],
            "distance_pct": c["distance_pct"],
            "open_interest": c["min_open_interest"],
        })

    payload = {
        "market": context.get("underlyings", {}),
        "quote_feed": context.get("feed"),
        "account": {
            "equity": account.get("equity"),
            "day_pnl": account.get("day_pnl"),
            "open_positions": account.get("open_positions"),
        },
        "currently_open": [
            {"underlying": p.get("underlying"), "kind": p.get("kind"),
             "contracts": p.get("contracts")}
            for p in open_positions
        ],
        "shortlist": slim,
    }
    return (
        "Here is the current screening output.\n\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n\nChoose at most one candidate, or return no_trade."
    )


def decide(shortlist: list[dict], context: dict, account: dict,
           open_positions: list[dict] | None = None,
           use_llm: bool = True) -> BrainDecision:
    """Pick one candidate. Never raises - always returns a decision.

    Falls back to deterministic selection when the LLM is unavailable or
    disabled, so the agent is never blocked on an Anthropic key.
    """
    load_dotenv()
    if not shortlist:
        return no_trade("screener returned no candidates")

    if not use_llm:
        return deterministic_decide(shortlist, "--no-llm")

    # An unset ANTHROPIC_API_KEY does not necessarily mean no credentials -
    # the SDK also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles.
    # Only the .env placeholder is treated as definitely-absent.
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key.startswith("sk-ant-your"):
        api_key = ""
    if not api_key and not os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return deterministic_decide(shortlist, "no ANTHROPIC_API_KEY")

    prompt = build_prompt(shortlist, context, account, open_positions or [])

    try:
        client = anthropic.Anthropic(api_key=api_key) if api_key \
            else anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema",
                                      "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        # An outage, an expired balance, or a bad key must not stop the agent.
        d = deterministic_decide(shortlist, "Claude API error %s" % exc.status_code)
        d.error = "%s: %s" % (type(exc).__name__, exc)
        return d
    except Exception as exc:
        d = deterministic_decide(shortlist, "Claude call failed")
        d.error = "%s: %s" % (type(exc).__name__, exc)
        return d

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    try:
        payload = extract_json(text)
    except Exception as exc:
        return no_trade("could not parse model output", raw=text[:4000],
                        error="%s: %s" % (type(exc).__name__, exc))

    decision = validate(payload, shortlist)
    decision.raw = text[:4000]
    return decision
