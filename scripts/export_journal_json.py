"""Write the dashboard payload into the repository, beside the journal.

WHY THIS EXISTS
The published page is rebuilt by GitHub Pages, and Pages only rebuilds on a
push event. The agent's journal commits are made with GITHUB_TOKEN, which
deliberately raises no events, so during a long session the page's decision
log, funnel and closed-trade panels do not update at all - they wait for the
session to END, which can be six hours.

Committing the payload as a plain file makes it readable without any of that:
raw.githubusercontent.com serves the newest commit, sends
Access-Control-Allow-Origin: *, and needs no token. The page reads it there
and falls back to its own bundled copy when it cannot.

The one cost is GitHub's five-minute CDN cache on raw, so the log is up to
five minutes behind rather than up to six hours.

    python scripts/export_journal_json.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.export_static import build            # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "journal", "data.json")


# Fields the page never renders, and which dominate the file. llm_raw is the
# model's whole response and raw_json the broker's whole order object; both
# stay in the journal, neither belongs in a payload committed every cycle.
_DROP = {"decisions": ("llm_raw",), "orders": ("raw_json",)}


def slim(data: dict) -> dict:
    for key, fields in _DROP.items():
        rows = data.get(key)
        if isinstance(rows, list):
            data[key] = [{k: v for k, v in row.items() if k not in fields}
                         for row in rows]
    return data


def main() -> int:
    data = slim(build())
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    size = os.path.getsize(OUT)
    print("  wrote %s (%.1f KB, generated %s)"
          % (os.path.relpath(OUT), size / 1024.0, data["generated_at"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
