# Deploying Vetoed

Two things can be deployed, and they are deliberately separate:

| | What it is | Where it runs |
|---|---|---|
| **The agent** | Trades. Holds broker credentials. | A Linux VPS you control |
| **The dashboard** | Read-only journal viewer. No credentials, no order path. | Anywhere — VPS, GitHub Pages, a container |

Operational commands live in
[README.md](../README.md#running-vetoed-unattended). This document covers *why*
unattended operation is safe, which is the part a reviewer should check.

---

# Part 1 — The agent, unattended

## Why systemd and not Docker

Both were viable. systemd won on four counts specific to this project:

1. **Secrets.** `EnvironmentFile=` reads `/opt/vetoed/.env` at mode 600, owned
   by the service user. Docker's equivalents are worse: `--env-file` contents
   show up in `docker inspect`, and build-time env leaks into the image.
2. **Restart semantics.** `Restart=always` with `StartLimitBurst=5` restarts on
   crashes but *stops* on a crash loop — which is what a bad key produces. A
   Docker restart policy retries forever and hides the cause.
3. **Signals.** systemd stops units with SIGTERM; `loop.py` handles it,
   finishes the cycle in flight, and releases the run lock. Getting that right
   in Docker needs correct PID-1 handling, which is easy to get wrong.
4. **The repo already uses Docker for the dashboard**, and that image
   deliberately contains no Alpaca SDK and no credentials. Reusing it for the
   agent would undo exactly the property that makes it safe to publish.

The dashboard keeps its Dockerfile. The agent gets systemd.

## The idempotency logic, exactly

A remote process restarts because of reboots, crashes, network drops, Claude
failures, MCP failures, and deploys. **The failure mode that matters is a
restart submitting a trade that already exists.** Four mechanisms prevent it.

### 1. The broker is authoritative, not the journal

Before this work, the risk gates were fed an `AccountState` built from
`journal.open_spreads()`. The journal records what this process *believes* it
did. Those come apart in exactly the situations a restart creates:

| Situation | Journal says | Broker says | Old behaviour |
|---|---|---|---|
| Killed after submit, before `record_order` | nothing | position open | **opens a second position** |
| Submission timed out | `failed` (excluded from risk) | position open | **opens a second position** |
| Position expired or assigned | still open | nothing | blocks valid trades |
| Position opened by hand | nothing | position open | invisible to every gate |

`agent/reconcile.py` now fetches positions and open orders from Alpaca each
cycle and classifies every journal row against them:

```
both legs held at broker         -> genuinely open, counts as risk
client_order_id on a live order  -> resting, counts as risk
status uncertain + broker empty  -> never arrived, mark not_filled
exactly one leg held             -> UNCAPPED position, count it and log loudly
filled once, now absent          -> closed elsewhere, mark closed
leg at broker, no journal row    -> orphan, still counts toward concentration
```

`account_state_from()` then builds the risk state from **broker-confirmed rows
only**, with orphan legs counted as ceil(legs / 2) spreads, so an unexplained
holding still consumes concentration budget rather than being silently free.

### 2. A deterministic client_order_id

Alpaca rejects a duplicate `client_order_id`. That is the strongest idempotency
primitive available, and the old timestamp-based id (`alpha-<epoch_ms>`) threw
it away — a retry produced a *new* id, so the broker had no way to recognise
the resubmission.

```
vetoed-<YYYYMMDD>-<sha1(date|underlying|short|long|contracts)[:12]>
```

Same trade intent on the same day produces the same id, so **Alpaca refuses the
second submission**. Keyed on the date so the same spread can legitimately be
reopened tomorrow.

### 3. A pre-submit guard

Immediately before the only write path in the agent, `already_working()` checks
three independent things against the broker:

1. is the deterministic `client_order_id` already on a working order?
2. is the short leg already held as a position?
3. does the short leg appear on any working order?

Any hit means skip, journal `duplicate skipped`, submit nothing.

### 4. Uncertain is not failed, and nothing is ever retried blindly

A network timeout is **not** a rejection. `executor.py` now distinguishes them:
a message matching a definite-rejection marker is `failed`; everything else is
`uncertain`.

Uncertain orders are journalled as `uncertain` and **counted as live risk**,
because the asymmetry is not close — counting a phantom position costs one
skipped trade, while missing a real one can double a position. The next cycle's
reconciliation resolves it against the broker and marks it `not_filled` if it
never arrived.

**The order path contains no retry.** A blind retry is precisely how a timeout
becomes a double position.

## Single-flight locking

`agent/runlock.py` is a cross-process lock *file*, not a threading primitive,
because the overlaps that matter are between processes: a systemd restart
mid-cycle, or an operator running the agent by hand while the service is up.
APScheduler's `max_instances` handles neither.

Claimed with `O_CREAT|O_EXCL` (atomic on POSIX and Windows), holding the PID and
a timestamp. A lock left behind by a dead process is reclaimed with a logged
message rather than wedging the service forever.

A cycle that cannot take the lock is **skipped, not queued** — the next tick is
minutes away, and a queued cycle would act on a stale shortlist.

## How market hours are enforced

Two layers, and only one is authoritative:

- **Coarse:** a cron window, Mon–Fri 09:00–16:59 `America/New_York`. Explicitly
  US Eastern, never the VPS's local clock.
- **Authoritative:** `market.is_market_open()` — Alpaca's own clock — checked at
  the top of *every* cycle. It knows holidays and early closes, which no cron
  expression does.

`--force` bypasses the market check for a single debugging cycle. **It is
refused in combination with `--schedule`**, because scheduled-and-forced means
"trade stale weekend quotes, forever".

## Poll interval

`POLL_INTERVAL_MINUTES`, default **30**, clamped to 1–240 with a warning and a
fallback on anything invalid.

30 is the default because a cycle's inputs barely move faster than that: a
20-day realised-vol estimate and 2–14 DTE Greeks drift slowly. Polling faster
re-examines the same candidates and burns API and Claude quota without
surfacing better trades.

It is **configuration, not strategy** — every gate, threshold and exit rule is
byte-identical at any interval. That is why it is safe to expose, and why a
shorter value is a demo aid rather than a different system.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| One cycle raises | Caught and logged, health file records it, scheduler continues |
| Claude unavailable | Deterministic fallback; the agent keeps running |
| MCP spawn fails | Cycle fails safe, no order attempted |
| Broker unreachable | Positions still managed from the journal; **no new entries** |
| One symbol's data fails | That symbol is skipped, the others are unaffected |
| SIGTERM / SIGINT | Cycle finishes, lock released, clean exit |
| Crash loop | systemd stops after 5 restarts in 10 minutes so the cause is visible |

## Demonstrating unattended mode without waiting hours

```bash
# One cycle right now, market closed, nothing submitted
python -m agent.loop --force

# Scheduler on a one-minute tick, dry run
POLL_INTERVAL_MINUTES=1 python -m agent.loop --schedule

# Prove the lock: start one, then run another in a second terminal
#   -> "skipping tick: cycle already running (pid N, held Ns)"

# Watch the heartbeat
watch -n2 cat journal/health.json
curl -s localhost:8000/api/health | python3 -m json.tool
```

**Those are dry runs.** The dashboard labels every such order
`DRY RUN · NOT SENT` and marks cycles that ran with the market closed, so a
demo cannot be mistaken for live paper activity. Four states are distinguished
throughout: *dry run*, *paper-account activity*, *historical snapshot*, and
*current live screen*. No broker activity and no P&L is ever simulated.

---

# Part 2 — The dashboard

## What is in the image

`Dockerfile` copies exactly four things — `agent/__init__.py`,
`agent/journal.py`, `dashboard/`, and `journal/trades.db`. It installs only
FastAPI and uvicorn.

Verified on the built image:

```
/app/agent/__init__.py
/app/agent/journal.py
/app/dashboard/api.py
/app/dashboard/static/index.html
/app/journal/trades.db
```

- **No `.env`** — excluded by `.dockerignore` and never copied.
- **No `alpaca-py`, no `anthropic`, no MCP client** — `import alpaca` fails
  inside the container. The image physically cannot place an order.
- **No secret environment variables.**
- Runs as an unprivileged user (`viewer`, uid 1000).

`dashboard/api.py` has no route that places, cancels, or modifies an order.

## Run it locally

```bash
docker build -t vetoed-dashboard .
docker run -p 7860:7860 vetoed-dashboard
# http://localhost:7860
```

## Option A — GitHub Pages (recommended: free, and never sleeps)

**Use this one for the submitted Application URL.** It is a static site, so
there is no server to spin down, no cold start, and no 48-hour pause. It
answers instantly whenever a judge opens it, including at 3am a week from now.

`.github/workflows/pages.yml` is already committed. You only have to switch
Pages on once:

1. Go to **repository → Settings → Pages**.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.
   (Not "Deploy from a branch" — the workflow builds the site.)
3. Go to the **Actions** tab and check the *Deploy dashboard to Pages* run. If
   it did not fire, click it and press **Run workflow**.
4. Your URL appears in Settings → Pages, and looks like
   `https://<user>.github.io/Vetoed/`.

Every later push to `main` rebuilds and republishes automatically.

### How the same page serves both modes

`index.html` probes `/api/summary` once when it loads:

- **A server answers** (local, or Docker) → live mode, polling every 15s.
- **Nothing answers** (GitHub Pages) → reads the bundled `data.json` and shows
  a snapshot pill with the time it was frozen.

So there is one page and no build step. Preview the static build locally:

```bash
python scripts/export_static.py
cd site && python -m http.server 8080
# http://localhost:8080
```

## Option B — Render (free, but sleeps)

`render.yaml` is committed, so Render picks the settings up automatically.

1. Sign in at <https://render.com> with GitHub.
2. **New → Blueprint**, choose the `Vetoed` repository.
3. Render reads `render.yaml`, builds the Dockerfile, and gives you
   `https://vetoed-dashboard.onrender.com`.

The free plan sleeps after 15 minutes idle, so a cold start takes about 30
seconds. Open the link a minute before demoing it.

## Option C — Hugging Face Spaces (free, pauses after 48h)

No card needed, and it serves port 7860 by default, which the Dockerfile
already uses. Free `cpu-basic` Spaces pause after 48 hours of inactivity, so
treat this as a secondary live link rather than the submitted URL.

1. Create a Space at <https://huggingface.co/new-space>, SDK **Docker**, blank
   template.
2. Push this repository to the Space remote:

   ```bash
   git remote add space https://huggingface.co/spaces/<user>/<space-name>
   git push space main
   ```

3. The Space needs a `README.md` with Spaces front matter at the top:

   ```
   ---
   title: Vetoed
   emoji: 🛡️
   colorFrom: gray
   colorTo: green
   sdk: docker
   app_port: 7860
   ---
   ```

   Add it on the Space only — it is not needed in the GitHub repo.

## Keeping the demo current

The dashboard reads `journal/trades.db`, which is committed. The agent writes
to your local copy, so after a trading session:

```bash
git add journal/trades.db
git commit -m "Update journal"
git push
```

GitHub Pages, Render and Spaces all redeploy on push, so the demo updates
itself. Nothing else to run.

**The journal is safe to commit.** It was audited before being un-ignored: no
API keys, no Alpaca account id, no order ids, paper trading only. Re-check
after live trading, because `orders.raw_json` will then contain real Alpaca
order responses:

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
