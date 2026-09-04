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

THE PROMPT BOUNDARY, STATED PRECISELY. No MCP tool output reaches the model.
What does reach it is the screener's own candidate numbers, account scalars
that were passed through float(), and a whitelisted set of per-underlying
figures this codebase computed itself.

The whitelist in build_prompt() is the enforcement point, and it exists
because of a real hole: screener.screen() records a failed underlying with the
exception message, and an Alpaca error string is partly chosen by the server.
Forwarding that dict wholesale would have put externally-influenced text in
the prompt. Only the exception CLASS NAME crosses now.

So the accurate claim is not "zero attack surface" - it is that every string
reaching the model comes from a fixed vocabulary this repository controls, and
tests/test_brain.py tries to smuggle an instruction through the error channel
to prove it.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
# Two are supported, and neither is required. The judgement layer is optional
# by design: `deterministic_decide` below runs the same shortlist through fixed
# arithmetic, and every risk gate downstream is identical either way. That is
# what makes an API outage a degradation rather than an outage.
#
#   featherless   OpenAI-compatible, open-weight models. The hackathon's
#                 partner, so this is the default when its key is present.
#   anthropic     Claude, via the official SDK. Imported lazily so the package
#                 is not a hard dependency of the agent.
#
# Selection is automatic unless BRAIN_PROVIDER forces one.

FEATHERLESS_URL = "https://api.featherless.ai/v1/chat/completions"

# Featherless sits behind Cloudflare, which rejects the default
# "Python-urllib/3.x" User-Agent with a 403 and Cloudflare error 1010 - a
# browser-signature ban, not an auth failure, so it looks exactly like a bad
# key until you read the body. The requests and openai packages avoid it only
# because they set a User-Agent of their own. This identifies the client
# honestly rather than impersonating a browser.
USER_AGENT = "vetoed/0.1 (+https://github.com/Chong1120/Vetoed)"

def _env_or(name: str, default: str) -> str:
    """os.getenv with a default, treating an EMPTY value as absent too.

    os.getenv returns its default only when the variable is missing, not when
    it is set to "". GitHub Actions expands an undefined `vars.X` to the empty
    string, so `FEATHERLESS_MODEL: ${{ vars.FEATHERLESS_MODEL }}` set the
    variable to "" and the model name went out blank - Featherless answered
    422 "The model must be provided in the request", the agent fell back to
    deterministic selection, and the run still looked green.
    """
    return (os.getenv(name, "") or "").strip() or default


# Overridable because model availability depends on the plan. If this one is
# not on yours, set FEATHERLESS_MODEL - scripts/check_llm.py will tell you.
DEFAULT_FEATHERLESS_MODEL = "Qwen/Qwen2.5-72B-Instruct"
FEATHERLESS_MODEL = _env_or("FEATHERLESS_MODEL", DEFAULT_FEATHERLESS_MODEL)
ANTHROPIC_MODEL = _env_or("BRAIN_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 16000
LLM_TIMEOUT_SECONDS = 90
# Two asks, not more. The faults this covers clear within a second or
# resample cleanly; a provider that fails twice is having an outage, and
# the deterministic selector is the right answer to that, not a third
# ask eating into a 600-second cycle.
LLM_ATTEMPTS = 2
LLM_RETRY_PAUSE_SECONDS = 1.0


def _placeholder(value: str) -> bool:
    """.env.example ships placeholders; treat them as absent, not as keys."""
    v = (value or "").strip().lower()
    return (not v) or ("your" in v) or ("here" in v)


def resolve_provider() -> str:
    """Which judgement layer to use this cycle: featherless, anthropic, none.

    Explicit beats implicit: BRAIN_PROVIDER wins if set. Otherwise whichever
    key is actually present, preferring Featherless.
    """
    forced = os.getenv("BRAIN_PROVIDER", "").strip().lower()
    if forced in ("featherless", "anthropic", "none"):
        return forced
    if not _placeholder(os.getenv("FEATHERLESS_API_KEY", "")):
        return "featherless"
    if not _placeholder(os.getenv("ANTHROPIC_API_KEY", "")) or \
            os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    return "none"

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
- Concentration limits are NOT yours to enforce. A deterministic module \
caps positions per underlying, total open positions, and total capital \
at risk, and it vetoes anything that would breach them - using the real \
numbers, which you do not have. So do NOT refuse a genuinely strong edge \
on the grounds that it adds exposure: that judgement is already owned, \
and making it twice leaves good trades untaken for a limit that was \
never close. Correlation is still yours to weigh, as a question of PRICE \
rather than of permission - a second spread on the same underlying, or \
on a closely correlated index, should be held to a higher edge than the \
first was, not to an impossible one.
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

    # WHITELIST, not passthrough. `context["underlyings"]` is assembled from
    # Alpaca responses, and on a failure it carries an exception message whose
    # text an external service partly controls. Forwarding the dict wholesale
    # would put that string in the prompt. Only numbers this codebase computed
    # itself are copied across, plus an exception class name, which comes from
    # a fixed vocabulary. This is what lets the docs say no externally
    # controlled free text reaches the model.
    numeric = ("spot", "sma20", "atm_iv", "realized_vol_20d", "iv_vs_rv",
               "contracts_examined")
    market: dict = {}
    for sym, d in (context.get("underlyings") or {}).items():
        if not isinstance(d, dict):
            continue
        key = str(sym)[:12]
        if "error" in d:
            market[key] = {"error": str(d.get("error"))[:40]}
            continue
        clean = {k: d[k] for k in numeric if d.get(k) is not None}
        clean["above_trend"] = bool(d.get("above_trend"))
        market[key] = clean

    feed = str(context.get("feed") or "unknown")
    if feed not in ("opra", "indicative"):
        feed = "unknown"

    payload = {
        "market": market,
        "quote_feed": feed,
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


# Claude is given RESPONSE_SCHEMA through output_config and the API enforces
# it. Featherless has no equivalent, and an open-weight model asked for "the
# JSON object matching the schema" will happily invent its own key names - the
# first live call came back with {"decision": ..., "reason": ...} and no legs
# at all. validate() rejected it, correctly, but a judgement layer that is
# always rejected is not a judgement layer. So the shape is spelled out.
JSON_INSTRUCTIONS = """

Reply with ONE JSON object and nothing else. No prose before or after it, no \
markdown fences.

Every key below is required and must be spelled exactly as shown:

{
  "action": "open_spread" or "no_trade",
  "candidate_id": <integer index into the shortlist above, or -1 for no_trade>,
  "symbol": "<underlying ticker of the candidate you chose, or empty string>",
  "legs": [
    {"symbol": "<the candidate's short_symbol, copied exactly>", "side": "sell"},
    {"symbol": "<the candidate's long_symbol, copied exactly>",  "side": "buy"}
  ],
  "contracts": <integer; advisory only, the risk module decides the real size>,
  "rationale": "<2-4 sentences explaining the choice>",
  "confidence": <number from 0.0 to 1.0>
}

The two leg symbols must be copied character for character from the candidate \
you selected. They are checked against the shortlist, and any mismatch means \
the trade is discarded.

To pass: {"action": "no_trade", "candidate_id": -1, "symbol": "", "legs": [], \
"contracts": 0, "rationale": "...", "confidence": 0.0}
"""


def call_featherless(prompt: str, api_key: str, model: str | None = None,
                     timeout: int = LLM_TIMEOUT_SECONDS) -> str:
    """One chat completion against Featherless. Returns the raw text.

    Deliberately stdlib urllib rather than the `openai` package: this is a
    single JSON POST, and an extra SDK would be a dependency, a version
    constraint and another supply-chain edge for no benefit.

    No `response_format` is sent. Support for it varies across
    OpenAI-compatible servers, and a rejected parameter would fail the whole
    call - whereas `extract_json` already tolerates fenced and prose-wrapped
    output, which is the failure mode we would be guarding against anyway.
    """
    body = json.dumps({
        # Re-resolved per call, not read once at import: check_llm.py --model
        # and the tests both set this after the module is loaded.
        "model": model or _env_or("FEATHERLESS_MODEL", FEATHERLESS_MODEL
                                  or DEFAULT_FEATHERLESS_MODEL),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        # Low but not zero: this is a selection task, not a creative one.
        "temperature": 0.2,
        "max_tokens": 1500,
    }).encode("utf-8")

    req = urllib.request.Request(
        FEATHERLESS_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % api_key,
                 "User-Agent": USER_AGENT})

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    choices = payload.get("choices") or []
    if not choices:
        # Name the model. The provider answers a bad model id with a generic
        # "request was rejected as invalid", which says nothing about the one
        # setting most likely to be wrong - and the model can differ between a
        # laptop and CI, where it comes from a repository variable. A failure
        # that does not name it sends you looking at the prompt instead.
        raise ValueError("no choices for model %r: %s"
                         % (FEATHERLESS_MODEL, str(payload)[:280]))
    return (choices[0].get("message") or {}).get("content") or ""


def _decide_featherless(shortlist, prompt, api_key):
    """Ask the model, once more if the first answer was not usable.

    WHY A SECOND ASK
    The fallback below is sound - deterministic selection is a real decision,
    not a skipped cycle - but it is not the judgement layer, and a cycle that
    silently used it is a cycle where the model did no work. On 4 Sep 2026
    seven of sixteen decisions fell back: three because the provider answered
    HTTP 200 with no choices at all ({"code": "no_response"}, which clears in
    seconds), and the rest because the model returned malformed JSON or echoed
    a contract symbol with a typo in it. Every one of those is a transient
    sampling or provider fault that a second ask fixes.

    WHAT IS DELIBERATELY NOT RETRIED
    An HTTPError. A refused request is deterministic - a bad key, a bad model
    id, a rejected body - and asking again buys the same refusal.

    THIS IS NOT AN ORDER RETRY. Nothing here reaches the broker. `validate()`
    still checks the answer against the shortlist and `risk.py` still sizes
    it, so a second ask cannot widen what the model is allowed to do; it can
    only change which shortlisted candidate is named. Order placement keeps
    its no-retry rule, because there a second attempt could open a second
    spread.
    """
    # Claude's schema is enforced by the API, so its prompt stays lean.
    worst = None
    for attempt in range(LLM_ATTEMPTS):
        if attempt:
            time.sleep(LLM_RETRY_PAUSE_SECONDS)
        try:
            text = call_featherless(prompt + JSON_INSTRUCTIONS, api_key)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            d = deterministic_decide(shortlist, "Featherless HTTP %s" % exc.code)
            d.error = "HTTPError %s: %s" % (exc.code, detail or exc.reason)
            return d
        except Exception as exc:
            worst = deterministic_decide(shortlist, "Featherless call failed")
            worst.error = "%s: %s" % (type(exc).__name__, exc)
            continue
        d = _parse_and_validate(text, shortlist)
        # A model that answers "no_trade" has judged, and `validate` leaves
        # `error` clear when it does. Only a faulty answer sets it, so this
        # asks again for a blank or a mangled reply and never for a real one.
        if not d.error:
            return d
        worst = d
    return worst


def _decide_anthropic(shortlist, prompt, api_key):
    # Imported here so `anthropic` is only needed when it is actually used.
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=api_key) if api_key \
            else anthropic.Anthropic()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema",
                                      "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        # An outage, an expired balance, or a bad key must not stop the agent.
        d = deterministic_decide(shortlist, "Claude call failed")
        d.error = "%s: %s" % (type(exc).__name__, exc)
        return d

    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_and_validate(text, shortlist)


def _parse_and_validate(text: str, shortlist: list[dict]) -> BrainDecision:
    """Shared tail: parse whatever the model said, then verify it.

    Unparseable output falls back to the deterministic selector, exactly as a
    network failure does. `decide` promises to "fall back whenever the
    judgement layer is unavailable", and a model returning something that is
    not JSON IS unavailable - it just fails in a different place. This path
    returned no_trade instead, so on 2026-09-03 two cycles skipped entirely
    with "could not parse model output" while the screener had a perfectly
    good shortlist sitting there. A provider that errors trades; a provider
    that babbles did not, which is backwards.

    The raw text and the parse error are still journalled, so a cycle that
    fell back is never mistaken for one the model answered.
    """
    try:
        payload = extract_json(text)
    except Exception as exc:
        d = deterministic_decide(shortlist, "model output was not valid JSON")
        d.raw = text[:4000]
        d.error = "%s: %s" % (type(exc).__name__, exc)
        return d
    decision = validate(payload, shortlist)
    decision.raw = text[:4000]
    return decision


def decide(shortlist: list[dict], context: dict, account: dict,
           open_positions: list[dict] | None = None,
           use_llm: bool = True) -> BrainDecision:
    """Pick one candidate. Never raises - always returns a decision.

    Falls back to deterministic selection whenever the judgement layer is
    unavailable or disabled, so the agent is never blocked on any vendor.
    Whichever path runs, `validate()` verifies the answer against the
    shortlist and `risk.py` sizes it, so a provider swap cannot widen what the
    model is allowed to do.
    """
    load_dotenv()
    if not shortlist:
        return no_trade("screener returned no candidates")

    if not use_llm:
        return deterministic_decide(shortlist, "--no-llm")

    provider = resolve_provider()
    if provider == "none":
        return deterministic_decide(shortlist, "no LLM provider configured")

    prompt = build_prompt(shortlist, context, account, open_positions or [])

    if provider == "featherless":
        return _decide_featherless(
            shortlist, prompt, os.getenv("FEATHERLESS_API_KEY", "").strip())

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if _placeholder(api_key):
        api_key = ""
    return _decide_anthropic(shortlist, prompt, api_key)
