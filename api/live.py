"""Read-only Alpaca proxy, so the published dashboard can be live.

WHY THIS EXISTS
GitHub Pages serves static files and has no server. For the browser to poll
Alpaca directly, the API keys would have to be embedded in the page - readable
by anyone who opens it, and sufficient to place trades. This function holds
the keys instead: they live in the host's environment, never in the page, and
the browser only ever sees the answer.

WHAT IT WILL AND WILL NOT DO
Two GETs, account and positions, and nothing else. There is no code path here
that places, cancels, or modifies an order, and it refuses to run against a
non-paper configuration for the same reason the agent does.

It is public, so treat what it returns as public: equity and open positions
are visible to anyone with the URL. That is the intended trade-off for a demo
dashboard, and it is why nothing here can act on the account.

Deploy target: Vercel (api/live.py -> /api/live). Stdlib only, no build.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

PAPER_BASE = "https://paper-api.alpaca.markets/v2"
TIMEOUT = 6      # two calls, comfortably inside Vercel's limit


def _get(path: str, key: str, secret: str):
    req = urllib.request.Request(
        PAPER_BASE + path,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "User-Agent": "vetoed-dashboard/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        return json.loads(fh.read().decode("utf-8"))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build():
    key = (os.environ.get("ALPACA_API_KEY") or "").strip()
    secret = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if not key or not secret:
        return 503, {"available": False, "reason": "no credentials configured"}

    # Same fail-closed rule the agent uses. A dashboard is not a reason to
    # relax it, and this endpoint is not intended to read a live-money account.
    if (os.environ.get("ALPACA_PAPER_TRADE") or "").strip().lower() != "true":
        return 503, {"available": False,
                     "reason": "ALPACA_PAPER_TRADE is not 'true'"}

    try:
        acct = _get("/account", key, secret)
        positions = _get("/positions", key, secret)
    except urllib.error.HTTPError as exc:
        return 502, {"available": False,
                     "reason": "alpaca returned HTTP %d" % exc.code}
    except Exception as exc:
        return 502, {"available": False,
                     "reason": "%s" % type(exc).__name__}

    return 200, {
        "available": True,
        "equity": _num(acct.get("equity")),
        "last_equity": _num(acct.get("last_equity")),
        "cash": _num(acct.get("cash")),
        "buying_power": _num(acct.get("buying_power")),
        "positions": [{
            "symbol": p.get("symbol"),
            "qty": _num(p.get("qty")),
            "avg_price": _num(p.get("avg_entry_price")),
            "current_price": _num(p.get("current_price")),
            "market_value": _num(p.get("market_value")),
            "unrealised": _num(p.get("unrealized_pl")),
        } for p in positions],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):                                    # noqa: N802
        # Never let this return a bare 500. A crashed function shows the host's
        # generic error page, which says nothing about what went wrong and
        # cannot be diagnosed from the browser. Report the failure as JSON so
        # the cause is visible at the URL itself.
        try:
            status, body = build()
        except Exception as exc:                         # noqa: BLE001
            status = 500
            body = {"available": False,
                    "reason": "%s: %s" % (type(exc).__name__, exc)}
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # The page is served from github.io and this from elsewhere, so the
        # browser will not read the response without this.
        self.send_header("Access-Control-Allow-Origin", "*")
        # Never let a CDN serve a stale account.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):                                # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
