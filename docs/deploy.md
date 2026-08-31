# Deploying Vetoed

Two things are deployed, and they are deliberately separate:

| | What it is | Where it runs |
|---|---|---|
| **The agent** | Trades. Holds broker credentials. | GitHub Actions, on a schedule |
| **The dashboard** | Read-only journal viewer. No credentials, no order path. | GitHub Pages, as a static build |

Operational steps are in
[README.md](../README.md#running-vetoed-unattended). This document covers *why*
unattended operation is safe, which is the part a reviewer should check.

---

# Part 1 — The agent, unattended

## Why GitHub Actions

The agent needs to run without depending on a laptop being awake. The options
were a paid VPS under systemd, or GitHub Actions. Actions won for this project:

1. **It costs nothing.** Public repositories get unlimited Actions minutes, and
   the repo is already public because the hackathon requires it.
2. **The infrastructure already exists.** The Pages workflow was already
   deploying the dashboard from this repo; the agent is one more workflow.
3. **Nothing to maintain.** No server to patch, no user to manage, no
   credentials sitting on a box.
4. **The safety work was already done.** See below — the reconciliation this
   agent does every cycle makes a stateless runner safe, and that code was
   written before the hosting decision, not to justify it.

An earlier revision of this repository shipped systemd units and an installer
for a VPS. They were removed rather than kept alongside, for the same reason
the Dockerfile was: two deployment stories means a reviewer has to work out
which one is real.

**The agent is not tied to Actions.** `python -m agent.loop --live --schedule`
runs the identical cycle on an internal timer, with `POLL_INTERVAL_MINUTES`
setting the interval, under any supervisor on any host.

## It is not a 24-hour process, and does not need to be

GitHub Actions cannot hold a process open for a day. Each tick is a fresh
container that runs one cycle and exits.

That is a smaller difference than it sounds. The systemd version was a
scheduler that *slept* between cycles — alive, but doing nothing. **No work
happens between ticks in either design.** What actually differs:

| | Long-lived host | GitHub Actions |
|---|---|---|
| Process alive between cycles | Yes, idle | No |
| Work done between cycles | None | None |
| Exit rules evaluated | Once per cycle | Once per cycle |
| Punctuality | On time | Best-effort; may be delayed or dropped |
| Single-flight | `runlock.py` lock file | Workflow `concurrency` group |
| Secrets | File at mode 600 | Encrypted repository secrets |

## Why a stateless runner is safe

Every Actions run begins with an empty machine and a journal checked out from
git. **That is a stale-state cold start by definition** — the exact scenario
`agent/reconcile.py` exists for. The four mechanisms below were built for
restart safety on a server, and they carry over unchanged.

### 1. The broker is authoritative, not the journal

Risk gates were previously fed an `AccountState` built from
`journal.open_spreads()`. The journal records what a process *believes* it did.
On Actions the journal is whatever was last committed, which may be several
cycles stale.

| Situation | Journal says | Broker says | Old behaviour |
|---|---|---|---|
| Runner killed after submit, before the commit | nothing | position open | **opens a second position** |
| Submission timed out | `failed` (excluded from risk) | position open | **opens a second position** |
| Position expired or assigned | still open | nothing | blocks valid trades |
| Position opened by hand | nothing | position open | invisible to every gate |

`reconcile.py` fetches positions and open orders from Alpaca each cycle and
classifies every journal row against them:

```
both legs held at broker         -> genuinely open, counts as risk
client_order_id on a live order  -> resting, counts as risk
status uncertain + broker empty  -> never arrived, mark not_filled
exactly one leg held             -> UNCAPPED position, count it and log loudly
filled once, now absent          -> closed elsewhere, mark closed
leg at broker, no journal row    -> orphan, still counts toward concentration
```

`account_state_from()` then builds the risk state from **broker-confirmed rows
only**, with orphan legs counted as ceil(legs / 2) spreads so an unexplained
holding still consumes concentration budget.

### 2. A deterministic client_order_id

Alpaca rejects a duplicate `client_order_id`. That is the strongest idempotency
primitive available, and a timestamp-based id throws it away — a re-run
produces a *new* id the broker cannot recognise.

```
vetoed-<YYYYMMDD>-<sha1(date|underlying|short|long|contracts)[:12]>
```

Same trade intent on the same day produces the same id, so **Alpaca refuses the
second submission**. This is what makes a re-run of a workflow safe. Keyed on
the date so the same spread can legitimately be reopened tomorrow.

### 3. A pre-submit guard

Immediately before the only write path, `already_working()` checks three
independent things against the broker: is the deterministic id already on a
working order, is the short leg already held, does the short leg appear on any
working order. Any hit means skip and journal `duplicate skipped`.

### 4. Uncertain is not failed, and nothing is retried blindly

A network timeout is **not** a rejection. `executor.py` separates them: a
message matching a definite-rejection marker is `failed`, everything else is
`uncertain`.

Uncertain orders are journalled `uncertain` and **counted as live risk**,
because the asymmetry is not close — counting a phantom position costs one
skipped trade, missing a real one can double a position. The next cycle's
reconciliation resolves it against the broker.

**The order path contains no retry.** A blind retry is precisely how a timeout
becomes a double position. Re-running a failed workflow is safe for the same
reason: the deterministic id collides.

## Single-flight

The workflow declares:

```yaml
concurrency:
  group: vetoed-agent
  cancel-in-progress: false
```

GitHub will not start a second cycle while one is in flight.
`cancel-in-progress: false` is deliberate — killing a cycle halfway through
submitting an order is worse than skipping a tick.

`agent/runlock.py` provides the same guarantee on a long-lived host, where a
lock file can persist. On Actions it is a no-op, since each run has its own
filesystem.

## Market hours

Two layers, and only one is authoritative:

- **Coarse:** `cron: "7,37 13-21 * * 1-5"`. GitHub cron is UTC with no
  timezone, so the window is deliberately **wide** — 13:00–21:00 UTC covers the
  US session under both EDT (13:30–20:00) and EST (14:30–21:00). No
  daylight-saving change can silently stop trading.
- **Authoritative:** `market.is_market_open()` — Alpaca's own clock — checked at
  the top of every cycle. It knows holidays and early closes, which no cron
  expression does. Ticks outside the session exit in about a second.

So the agent fires **approximately every 30 minutes** during the US market
session, and does nothing outside it.

`--force` bypasses the market check for one debugging cycle. The workflow never
combines it with `--live`, and `loop.py` refuses `--force` with `--schedule`.

## Punctuality — the real trade-off

GitHub schedules are best-effort. Runs can be delayed under load and can be
dropped entirely. Stated plainly because it is the one place this hosting
choice costs something:

- A **late entry** costs nothing. The candidate is either still there or it is
  not, and the screener re-evaluates from scratch every cycle.
- A **late exit check** is the real exposure. Stops, the delta stop and the
  1-DTE close are all evaluated per cycle, so a delayed tick means a stop
  evaluated late.

This is a difference of degree, not of kind: exits are evaluated per cycle on a
dedicated server too. Nothing in this system watches positions continuously.
A VPS makes ticks punctual; it does not make them continuous.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| One cycle raises | Job fails, journal still committed, next tick unaffected |
| Claude unavailable | Deterministic fallback; the agent keeps trading |
| MCP spawn fails | Cycle fails safe, no order attempted |
| Broker unreachable | Positions still managed from the journal; **no new entries** |
| One symbol's data fails | That symbol is skipped, the others unaffected |
| Runner cancelled | SIGTERM handled; the deterministic id prevents a duplicate on re-run |
| Two ticks overlap | `concurrency` group prevents it |

## Verifying a cycle actually worked

`scripts/show_journal.py` is the tool for this, and it exists because a green
run proves less than it appears to. Both silent degradations found during
development — a Cloudflare block that looked like a bad key, and an undefined
GitHub variable that expanded to an empty model name — produced a **successful
workflow run** with an order journalled, and were legible only in `llm_error`.

```bash
git pull && python scripts/show_journal.py --check
```

Exits non-zero if anything is worth looking at, so it also works as a
post-session gate.

## Demonstrating it without waiting for a tick

**Actions → Vetoed agent → Run workflow.** Defaults to **dry-run**: a full
cycle is screened, decided, gated and journalled, but nothing is submitted.

Locally:

```bash
python -m agent.loop --force                    # one dry cycle, market closed
POLL_INTERVAL_MINUTES=1 python -m agent.loop --schedule
```

**Those are dry runs.** The dashboard labels such orders `DRY RUN · NOT SENT`
and marks cycles that ran with the market closed, so a demo cannot be mistaken
for live paper activity. Four states are distinguished throughout: *dry run*,
*paper-account activity*, *historical snapshot*, and *current live screen*. No
broker activity and no P&L is ever simulated.

## What is exposed, and what is not

The repository is public, so **workflow logs are public**. They show equity,
open positions and decisions — the same figures already on the public
dashboard, for a paper account.

Secrets are encrypted by GitHub and masked in logs automatically. The Alpaca
account id is never printed. `.env` is gitignored and has never been committed.

---

# Part 2 — The dashboard

Read-only. There is no route in `dashboard/api.py` that can place, cancel, or
modify an order, and it imports neither the Alpaca SDK nor the MCP client.

There are two ways it runs, and they serve different purposes.

## A. GitHub Pages — the public demo URL

**This is the submitted Application URL.** It is a fully static build, so
there is no server to spin down, no cold start, and nothing to sleep.

```
https://chong1120.github.io/Vetoed/
```

`.github/workflows/pages.yml` runs `scripts/export_static.py` on every push to
`main`, which freezes every API response into `site/data.json` beside a copy of
the page. No Python runs at request time.

This matters because free *application* hosting sleeps — Render spins down
after 15 minutes, a free Hugging Face Space pauses after 48 hours — and nobody
knows when a judge will open the link. A static site cannot have that problem.

To refresh it after a trading session:

```bash
git add journal/trades.db
git commit -m "Update journal"
git push
```

The workflow rebuilds and republishes automatically.

## B. Locally — live view while developing

Run it against your own journal, with the `/api/health` endpoint live:

```bash
python -m uvicorn dashboard.api:app --port 8000
# http://localhost:8000
```

Useful when running cycles by hand. `/api/health` returns **200** when the last
heartbeat is recent and **503** when it is stale, so it also works as a check
after a local `--schedule` run.

Do not expose this beyond localhost without a reverse proxy and authentication:
the page shows account equity and position detail. The published version on
Pages is a frozen snapshot, which is a different risk profile from a live
endpoint into a running agent.

## One page, two modes

`index.html` probes `/api/summary` once on load:

- **A server answers** (running locally) → live mode, polling every 15s
- **Nothing answers** (GitHub Pages) → reads the bundled `data.json` and shows
  a snapshot pill with the freeze time

So there is one page and no build step. Preview the static build locally:

```bash
python scripts/export_static.py
cd site && python -m http.server 8080
```

## Keeping the journal safe to publish

`journal/trades.db` is committed — the dashboard has nothing to render without
it. It was audited before being un-ignored: no API keys, no Alpaca account id,
no order ids, paper trading only.

**Re-check after live trading**, when `orders.raw_json` starts holding real
broker responses:

```bash
python - <<'PY'
import sqlite3, re
c = sqlite3.connect("journal/trades.db")
bad = re.compile(r"PK[A-Z0-9]{12,}|sk-ant|secret|api[-_]?key", re.I)
for t in ("runs","decisions","orders","equity_snapshots"):
    for r in c.execute("SELECT * FROM %s" % t):
        for v in r:
            if v and bad.search(str(v)):
                print("CHECK", t, str(v)[:120])
print("scan complete")
PY
```
