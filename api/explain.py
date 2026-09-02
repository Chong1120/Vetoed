"""Explain one position, in words, from the journal's own record.

WHY THIS EXISTS
The facts are all recorded and scattered across three panels: the entry
rationale sits in the decision log, the exit reason in the closed-trade table,
and the numbers in a fourth place. A reader has to stitch them together to
answer the simplest question about a trade - why did it open, and why did it
end.

WHY IT IS SAFE TO USE A MODEL HERE
This is the same principle as the rest of the system, applied to a second
job. The model cannot open a position; here it cannot invent one either. The
browser sends an identifier, not text - the server finds that position in the
journal and builds the entire prompt itself from recorded values. There is no
path by which a caller can put words in the model's mouth, and nothing it
writes can reach the broker.

If the model is unavailable the endpoint says so and the page keeps showing
the raw facts, exactly as it does now.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

JOURNAL = "https://api.github.com/repos/%s/contents/%s?ref=main"
REPO = os.environ.get("GITHUB_REPO", "Chong1120/Vetoed")
PATH = os.environ.get("JOURNAL_PATH", "journal/data.json")
LLM_URL = "https://api.featherless.ai/v1/chat/completions"
MODEL = os.environ.get("FEATHERLESS_MODEL") or "Qwen/Qwen2.5-72B-Instruct"

SYSTEM = (
    "You are the trading agent, writing to the person whose account you "
    "manage. Explain the TECHNICAL reasoning for one position: what you "
    "measured, what it meant, and what you did about it. Write as yourself "
    "- I measured, I sold, I closed.\n\n"
    "WHAT YOU ARE LOOKING AT:\n"
    "- The two expected values are the SAME spread under the SAME model, "
    "priced once at what the underlying has actually been doing and once "
    "at what the market is charging. Only the volatility differs. The "
    "measured edge is the gap between them - the premium being harvested."
    "\n"
    "- The two probabilities of profit are that same gap expressed as a "
    "probability rather than as money.\n"
    "- Open interest and bid-ask are the liquidity gates it had to clear "
    "before anything else was considered.\n\n"
    "RULES, and they are absolute:\n"
    "- NEVER calculate. Every figure is precomputed. Quote what you are "
    "given, exactly, and nothing else.\n"
    "- NEVER print a field name. Each key states its own unit: a key "
    "ending DOLLARS is money, one ending PERCENT is a percentage. "
    "Describe it in English with that unit attached. Reporting a dollar "
    "figure as a volatility, or a percentage as a dollar amount, is the "
    "worst error you can make here.\n"
    "- Cite the specific measurements. Do not write that the edge was "
    "strong; write what it was and what it was measured against.\n"
    "- Never speculate about the market or what happens next. If "
    "something is not recorded, leave it out.\n"
    "- Four to six sentences, 90 to 140 words. No bullet points, no "
    "markdown, no headings.\n"
    "- If the facts say the reasoning was lost, say so plainly and "
    "describe only what is recorded."
)


def _journal():
    req = urllib.request.Request(
        JOURNAL % (REPO, PATH),
        headers={"Accept": "application/vnd.github.raw",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "vetoed-dashboard/1.0",
                 **({"Authorization": "Bearer " + os.environ["GITHUB_DISPATCH_TOKEN"]}
                    if os.environ.get("GITHUB_DISPATCH_TOKEN") else {})})
    with urllib.request.urlopen(req, timeout=8) as fh:
        return json.loads(fh.read().decode("utf-8"))


def _facts(data, leg):
    """Find the position and gather everything recorded about it."""
    for row in data.get("closed_positions") or []:
        if row.get("short_symbol") == leg:
            out = dict(row); out["state"] = "closed"; break
    else:
        for row in data.get("positions") or []:
            if row.get("short_symbol") == leg:
                out = dict(row); out["state"] = "open"; break
        else:
            return None

    # The screener's whole measurement, as recorded. A note that says "a
    # strong edge" explains nothing; the numbers behind that judgement are all
    # in the journal and this is where they earn their keep.
    for d in data.get("decisions") or []:
        cand = d.get("candidate_json") or {}
        if cand.get("short_symbol") == leg:
            # Every field carries its unit in its own name.
            #
            # Handed the raw schema, the model read "ev 25.73, ev_rn 11.12"
            # and wrote "realised volatility of 25.73 and implied volatility
            # of 11.12". Those are expected values in DOLLARS and it reported
            # them as volatility percentages - confidently, and to a reader
            # with no way to tell. The prompt explained the units in prose at
            # the time; prose was not enough. A name like ev_rn means nothing
            # without the schema, so the unit goes in the key where it cannot
            # be separated from the number.
            #
            # Fractions are converted here too. pop 0.8527 invites the model
            # to print "0.85% probability", so it arrives as 85.3.
            def _num(key, scale=1.0, nd=2):
                v = cand.get(key)
                return round(float(v) * scale, nd) if v is not None else None

            screening = {
                "expected_value_at_realised_vol_DOLLARS": _num("ev"),
                "expected_value_at_implied_vol_DOLLARS": _num("ev_rn"),
                "measured_edge_DOLLARS_per_spread": _num("vrp_edge"),
                "max_profit_per_spread_DOLLARS": _num("max_profit"),
                "max_loss_per_spread_DOLLARS": _num("max_loss"),
                "probability_of_profit_real_world_PERCENT": _num("pop", 100.0, 1),
                "probability_of_profit_risk_neutral_PERCENT": _num("pop_rn", 100.0, 1),
                "implied_volatility_PERCENT": _num("short_iv", 100.0, 1),
                "realised_volatility_20day_PERCENT": _num("realized_vol", 100.0, 1),
                "short_strike_delta": _num("short_delta", 1.0, 4),
                "short_strike_distance_out_of_the_money_PERCENT":
                    _num("distance_pct", 100.0, 2),
                "days_to_expiry_at_entry": cand.get("dte"),
                "strike_width_DOLLARS": cand.get("width"),
                "open_interest_on_the_thinner_leg": cand.get("min_open_interest"),
                "widest_bid_ask_as_PERCENT_of_mid":
                    _num("worst_spread_pct", 100.0, 1),
            }
            out["screening"] = {k: v for k, v in screening.items()
                                if v is not None}
            if d.get("llm_rationale"):
                out["model_rationale_at_entry"] = d["llm_rationale"]
            if d.get("llm_confidence") is not None:
                out["model_confidence"] = d["llm_confidence"]
            if d.get("risk_reasons"):
                out["sizing_notes"] = d["risk_reasons"]
            break

    # Every money figure, precomputed. Asked to work one out, the model
    # multiplied 1.39 by 14 and reported $19.46 for a spread that collected
    # $1,946 - options are a hundred shares a contract and it did not know
    # that. It is never asked to calculate anything now.
    try:
        qty = int(out.get("contracts") or 0)
        per = float(out.get("credit") or 0)
        if not out.get("credit_total") and qty and per:
            out["credit_total"] = round(per * 100 * qty, 2)
        out["credit_collected_dollars"] = out.get("credit_total")
        out["max_loss_dollars"] = out.get("max_loss_total")
        for k in ("credit", "credit_total", "max_loss_total"):
            out.pop(k, None)            # ambiguous per-share values, removed
    except (TypeError, ValueError):
        pass

    if out.get("status") == "adopted":
        out["note"] = ("This position was rebuilt from the broker's record "
                       "because its journal entry was lost. The reasoning "
                       "recorded at the time is not available.")
    return out


def explain(leg):
    key = (os.environ.get("FEATHERLESS_API_KEY") or "").strip()
    if not key:
        return 503, {"error": "no model configured"}
    try:
        facts = _facts(_journal(), leg)
    except Exception as exc:                              # noqa: BLE001
        return 502, {"error": "could not read the journal (%s)" % type(exc).__name__}
    if not facts:
        return 404, {"error": "no position with that leg"}

    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(facts, default=str)}],
        "temperature": 0.2,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": "vetoed/0.1 (+https://github.com/Chong1120/Vetoed)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as fh:
            payload = json.loads(fh.read().decode("utf-8"))
        text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not text:
            return 502, {"error": "model returned nothing for %r" % MODEL}
    except urllib.error.HTTPError as exc:
        return 502, {"error": "model returned HTTP %d" % exc.code}
    except Exception as exc:                              # noqa: BLE001
        return 502, {"error": "%s" % type(exc).__name__}

    return 200, {"leg": leg, "explanation": text.strip(), "state": facts["state"]}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):                                     # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        leg = (q.get("leg") or [""])[0].strip().upper()
        if not leg.isalnum() or not 10 <= len(leg) <= 24:
            status, body = 400, {"error": "leg must be an option symbol"}
        else:
            status, body = explain(leg)
        out = json.dumps(body).encode("utf-8")
        # Writing the note costs about six seconds of model time; serving one
        # already written costs a tenth of one. The old five-minute cache threw
        # the result away while the position it describes was still open, so a
        # reader arriving six minutes after the last one paid the full six
        # seconds again for a paragraph that had not changed and could not.
        #
        # A CLOSED position is finished: its entry, its exit and its result are
        # all final, and its note is immutable, so it is cached hard. An OPEN
        # one is cached for an hour and then served stale while a fresh copy
        # is fetched behind it - the reader waits for nothing, and the only
        # thing that can age is the position closing, which the row's own
        # numbers report correctly regardless.
        if status != 200:
            cache = "no-store"
        elif body.get("state") == "closed":
            cache = "public, max-age=600, s-maxage=604800, immutable"
        else:
            cache = ("public, max-age=300, s-maxage=3600, "
                     "stale-while-revalidate=86400")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_OPTIONS(self):                                 # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
