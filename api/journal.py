"""Serve the committed journal with no cache in front of it.

WHY THIS EXISTS
The page needs three things to be current: the account, and the agent's own
record of what it decided and closed. The account arrives through api/live.py.
The journal was arriving two ways, and neither was current:

  - GitHub Pages rebuilds only on a push event, and the agent's journal
    commits are made with GITHUB_TOKEN, which raises none. During a session
    the bundled copy never updates at all.
  - raw.githubusercontent.com serves the newest commit but sits behind a
    five-minute CDN cache. Measured: 58 seconds after a push it was still
    returning the previous file, and a cache-busting query string did not
    change that.

So this reads the file through the GitHub contents API, which is not that
CDN, and returns it with no-store. The page sees a cycle's decisions within
seconds of the commit rather than within five minutes of it.

Read-only, like everything else in this directory: it fetches one file from a
public repository and returns it. It cannot write, and it cannot trade.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

REPO = os.environ.get("GITHUB_REPO", "Chong1120/Vetoed")
PATH = os.environ.get("JOURNAL_PATH", "journal/data.json")
API = "https://api.github.com/repos/%s/contents/%s?ref=main"
TIMEOUT = 8


def fetch():
    req = urllib.request.Request(
        API % (REPO, PATH),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vetoed-dashboard/1.0",
            # Optional. Without it this still works at 60 requests an hour per
            # IP, which a single viewer will not reach; with it, 5000.
            **({"Authorization": "Bearer " + os.environ["GITHUB_DISPATCH_TOKEN"]}
               if os.environ.get("GITHUB_DISPATCH_TOKEN") else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        meta = json.loads(fh.read().decode("utf-8"))
    if meta.get("encoding") != "base64" or not meta.get("content"):
        raise ValueError("unexpected contents response")
    return json.loads(base64.b64decode(meta["content"]).decode("utf-8"))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):                                    # noqa: N802
        try:
            body, status = fetch(), 200
        except urllib.error.HTTPError as exc:
            body, status = {"error": "github returned HTTP %d" % exc.code}, 502
        except Exception as exc:                         # noqa: BLE001
            body, status = {"error": "%s" % type(exc).__name__}, 502

        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        # The whole point. Nothing may hold this between the commit and the page.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):                                # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
