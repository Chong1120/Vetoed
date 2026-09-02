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

import datetime
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


def _series(hist):
    """Timestamped equity points, with the padding removed."""
    if not isinstance(hist, dict):
        return []
    ts = hist.get("timestamp") or []
    eq = hist.get("equity") or []
    out = []
    for t, v in zip(ts, eq):
        n = _num(v)
        if n is None or n <= 0:             # padding, not a reading
            continue
        out.append({"t": int(t), "equity": n})
    return out


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
        # Alpaca's own clock. The page used to read a market_open flag off the
        # last journalled cycle, which freezes the moment a session ends - so
        # the badge sat on "market open" all night. This is the authority, and
        # it knows holidays and early closes that no clock arithmetic does.
        try:
            clock = _get("/clock", key, secret)
        except Exception:
            clock = None
        # The broker's own equity series. Ours is one point per cycle - a dot
        # every ten minutes at best, and nothing at all overnight. This is the
        # same account at 15-minute resolution, and it is the broker's record
        # rather than our reading of it.
        try:
            hist = _get("/account/portfolio/history?period=1W&timeframe=15Min",
                        key, secret)
        except Exception:
            hist = None                     # never fail the whole call for it
    except urllib.error.HTTPError as exc:
        return 502, {"available": False,
                     "reason": "alpaca returned HTTP %d" % exc.code}
    except Exception as exc:
        return 502, {"available": False,
                     "reason": "%s" % type(exc).__name__}

    return 200, {
        "available": True,
        # When the account was actually read. The page shows this, so a stale
        # or cached answer is visible rather than being passed off as current.
        "fetched_at": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "equity": _num(acct.get("equity")),
        "last_equity": _num(acct.get("last_equity")),
        "cash": _num(acct.get("cash")),
        "buying_power": _num(acct.get("buying_power")),
        # Alpaca pads the series with zeros where the account did not exist
        # yet; plotting those draws a cliff to the axis that never happened.
        "equity_series": _series(hist),
        "market_open": (clock or {}).get("is_open"),
        "next_open": (clock or {}).get("next_open"),
        "next_close": (clock or {}).get("next_close"),
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
