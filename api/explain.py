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
    "You are the trading agent, writing a short note to the person whose "
    "account you manage, explaining one position you took. Write as yourself: "
    "I sold, I closed, I held. You are given your own recorded facts "
    "about the trade as JSON.\n\n"
    "RULES, and they are absolute:\n"
    "- NEVER calculate anything. Every figure you need is already given "
    "as a dollar amount. If a number is not in the facts, do not state "
    "it.\n"
    "- Never speculate about the market, your timing, or what happens "
    "next. If something is not recorded, leave it out rather than "
    "guessing.\n"
    "- Do not congratulate yourself and do not apologise. Say what you "
    "did and why you did it.\n"
    "- Two or three sentences, 45 to 75 words. Plain English, no bullet "
    "points, no markdown, no headings, no dates unless they matter.\n"
    "- A credit spread pays when the underlying stays on the safe side "
    "of the strike you sold. Explain it that way, in ordinary words.\n"
    "- If the facts say the reasoning was lost, say plainly that you no "
    "longer have your notes from the time, and describe only what is "
    "recorded."
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

    # The reasoning recorded when it was opened, if the decision survived.
    for d in data.get("decisions") or []:
        cand = d.get("candidate_json") or {}
        if cand.get("short_symbol") == leg and d.get("llm_rationale"):
            out["entry_rationale"] = d["llm_rationale"]
            out["entry_edge"] = cand.get("vrp_edge")
            out["entry_pop"] = cand.get("pop")
            out["entry_dte"] = cand.get("dte")
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
        "max_tokens": 220,
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_OPTIONS(self):                                 # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
