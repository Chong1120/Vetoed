"""Start the trading session, on a schedule that actually fires.

WHY THIS EXISTS
GitHub's scheduled workflows have never run in this repository. Eighteen runs,
all of them manual, across two different cron expressions and every due tick
of a trading day - with valid YAML on the default branch of a public repo,
Actions enabled and the workflow reporting state active. Everything checkable
is correct, and it still does not fire. Rewriting the cron expression does not
address that, because the expression was never the problem.

So the trigger moves somewhere that does fire. Vercel runs this on a schedule
and it dispatches the workflow through the GitHub API, which is an ordinary
authenticated request rather than a scheduled event - the part that has been
failing. GitHub's own cron stays in the workflow as a second chance; if it
ever starts working, the concurrency group means the two cannot both trade.

SECURITY
This URL can start real (paper) trading, so it refuses anything that does not
present CRON_SECRET. Without that secret set it refuses everything, rather
than defaulting to open - a public endpoint that starts a trading session is
not something to leave unauthenticated by accident.

The token it uses needs Actions: read and write on this repository only.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

REPO = os.environ.get("GITHUB_REPO", "Chong1120/Vetoed")
WORKFLOW = "agent.yml"
API = "https://api.github.com/repos/%s/actions/workflows/%s/dispatches"


def dispatch():
    token = (os.environ.get("GITHUB_DISPATCH_TOKEN") or "").strip()
    if not token:
        return 503, {"started": False, "reason": "GITHUB_DISPATCH_TOKEN not set"}

    body = json.dumps({
        "ref": "main",
        "inputs": {
            "mode": "session",
            "interval_minutes": os.environ.get("VETOED_INTERVAL", "10"),
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        API % (REPO, WORKFLOW), data=body, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "vetoed-session-starter/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            # 204 No Content is success for this endpoint.
            return 200, {"started": True, "status": res.status, "repo": REPO}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return 502, {"started": False,
                     "reason": "github returned HTTP %d" % exc.code,
                     "detail": detail}
    except Exception as exc:                              # noqa: BLE001
        return 502, {"started": False, "reason": type(exc).__name__}


class handler(BaseHTTPRequestHandler):
    def _reply(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                                     # noqa: N802
        secret = (os.environ.get("CRON_SECRET") or "").strip()
        if not secret:
            # Fail closed. An unauthenticated endpoint that starts trading is
            # worse than one that never starts it.
            return self._reply(503, {"started": False,
                                     "reason": "CRON_SECRET not configured"})
        if self.headers.get("Authorization", "") != "Bearer " + secret:
            return self._reply(401, {"started": False, "reason": "unauthorised"})
        try:
            status, body = dispatch()
        except Exception as exc:                          # noqa: BLE001
            status, body = 500, {"started": False,
                                 "reason": "%s: %s" % (type(exc).__name__, exc)}
        self._reply(status, body)
