"""
Freeze the dashboard into a static site for GitHub Pages.

WHY THIS EXISTS. The submitted demo URL has to work whenever a judge opens it,
and nobody knows when that will be. Free application hosting sleeps: Render
spins down after 15 minutes, and a free Hugging Face Space pauses after 48
hours. Either way the first visitor waits through a cold start, or sees a 503.

The dashboard is already read-only - it renders a SQLite file and has no route
that can trade - so it does not need a server at all. This script writes the
same JSON the live API would serve into one file, next to a copy of the page.
The result is a static site: no cold start, no sleep, no runtime.

    python scripts/export_static.py            -> ./site/

Output:
    site/index.html    the dashboard, unmodified
    site/data.json     every API response, plus the time it was frozen

index.html probes /api/summary on load. If a server answers it stays live and
polls; if nothing answers it falls back to data.json and shows a snapshot
banner. One page, both modes, no build step.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import payload  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dashboard", "static", "index.html")
OUT = os.path.join(ROOT, "site")


def build() -> dict:
    """Every endpoint the page calls, under the key the page looks it up by."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": payload.summary(),
        "equity": payload.equity(),
        "positions": payload.positions(),
        "broker_positions": payload.broker_positions(),
        "decisions": payload.decisions(60),
        "runs": payload.runs(30),
        "orders": payload.orders(200),
    }


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    data = build()

    with open(os.path.join(OUT, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), default=str)
    shutil.copyfile(SRC, os.path.join(OUT, "index.html"))

    # .nojekyll stops GitHub Pages running the files through Jekyll, which
    # would otherwise ignore anything it considers a special filename.
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    size = os.path.getsize(os.path.join(OUT, "data.json"))
    print("site/ built  ->  %s" % OUT)
    print("  frozen at      %s" % data["generated_at"])
    print("  runs           %d" % len(data["runs"]))
    print("  decisions      %d" % len(data["decisions"]))
    print("  orders         %d" % len(data["orders"]))
    print("  equity points  %d" % len(data["equity"]))
    print("  data.json      %.1f KB" % (size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
