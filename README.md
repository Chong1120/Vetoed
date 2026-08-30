# Vetoed

**An autonomous options agent where the AI is the least-trusted component.**

Claude selects from a pre-screened shortlist. Deterministic risk gates it cannot
influence decide what is permitted and how large — and can veto it entirely.
Every decision, including every rejection, is logged and auditable.

Built for the [lablab.ai × Alpaca AI Trading Agents hackathon](https://lablab.ai)
(28 Aug – 4 Sept 2026). Runs on Alpaca **paper trading** through Alpaca's
official **MCP server**.

> **Paper trading only.** `ALPACA_PAPER_TRADE=true` is asserted at every entry
> point and the process refuses to start without it. No real capital is at risk.
> This is not financial advice.

---

## What it trades, and why

The agent sells **defined-risk vertical credit spreads** on SPY, QQQ, IWM and
AAPL. It profits when the underlying *does not move much* — it takes no
directional view. For every short leg there is a long leg further out of the
money, so maximum loss is `width × 100 − credit`, known before the order is sent.

This targets the **volatility risk premium** — implied volatility systematically
exceeds subsequently realised volatility, so option sellers are compensated:

- **Bakshi & Kapadia (2003)**, *Review of Financial Studies* 16(2), 527–566 —
  delta-hedged S&P 500 option portfolios underperform zero
- **Carr & Wu (2009)**, *RFS* 22(3), 1311–1341 — variance risk premiums across
  5 indices and 35 stocks
- **CBOE PUT Index** (Jun 1986–Dec 2018) — one-month **at-the-money
  cash-secured** put writing returned 9.54% at 9.95% volatility, against 9.80%
  at 14.93% for the S&P 500

These motivate *why* a volatility premium should exist. None of them validate
this agent: the PUT index in particular is a different instrument (ATM
cash-secured puts, monthly) from the short-dated OTM defined-risk spreads
traded here. Supporting context, not proof.

**We aim to be paid for carrying volatility risk; we do not predict direction.**

## The measurement that makes the edge explicit

Under the risk-neutral measure a fairly-priced trade has **zero** expected
P&L by construction. So an EV built only from risk-neutral inputs carries no
edge information. The signal has to be a *difference*, not a level.

Everything runs through **one model** — a zero-drift lognormal, `E[S_T] = S₀`
— and **volatility is the only input that changes**:

| | Volatility fed in | What it estimates |
|---|---|---|
| `ev_rn` | short leg's **implied** vol | roughly how the market values this spread |
| `ev_rw` | 20-day **realised** vol | what it would be worth if vol matched recent history |
| **`vrp_edge`** | — | **`ev_rw − ev_rn`**: the volatility gap, in dollars |

Same credit, same drift, same closed form on both sides — so the difference
isolates the volatility gap and is **exactly zero** when the two vols agree
(pinned by a test). It is our operational signal, not the academic variance
risk premium of Carr & Wu, which is defined on variance swap rates over a
matched horizon.

**Not delta.** Delta is N(d₁); the risk-neutral probability of finishing ITM is
N(d₂). The gap between them has *opposite sign for calls and puts*, so
substituting one for the other biases the two sides of the book in opposite
directions. An earlier build did exactly that and reported an edge on trades
that had none — see [`docs/how-it-works.md`](docs/how-it-works.md) §6.

`vrp_edge` is the **gate and the ranking key**: it must clear $2.00, and the
shortlist sorts on `vrp_edge / max_loss`. Ranking on `ev_rw` alone would rank
on how low the realised-vol estimate happened to land — estimation error, not
compensation. On the live screen recorded in §5.9 of the technical doc this
cut **20 structurally valid spreads to 7** on a single snapshot, put AAPL
(implied/realised **1.260**) on top, and had QQQ (**0.894**, implied below
realised) produce **zero** candidates.

## Architecture

```
                        ┌──────────────────────┐
                        │  loop.py  scheduler  │  APScheduler, US market hours
                        └──────────┬───────────┘
             ┌─────────────────────┴─────────────────────┐
             │  0. manage open positions FIRST           │  realise P&L
             └─────────────────────┬─────────────────────┘
                                   ▼
     ┌───────────────┐    ┌─────────────────┐    ┌──────────────────┐
     │  data.py      │───▶│  screener.py    │───▶│  brain.py        │
     │  Alpaca APIs  │    │  DETERMINISTIC  │    │  Claude          │
     │  chain+Greeks │    │  dual-measure EV│    │  selects only    │
     └───────────────┘    └─────────────────┘    └────────┬─────────┘
              ▲                    ▲                      │
              │            ┌───────┴────────┐             ▼
              │            │  adapt.py      │    ┌──────────────────┐
              └────────────│  guardrails    │    │  risk.py         │
                           │  restrict only │    │  HARD GATES      │
                           └────────────────┘    │  can VETO, SIZES │
                                                 └────────┬─────────┘
                                                          ▼
                                                 ┌──────────────────┐
                                                 │  executor.py     │
                                                 │  Alpaca MCP      │
                                                 │  atomic mleg     │
                                                 └────────┬─────────┘
                                                          ▼
                                        journal/trades.db  ──▶  dashboard/
```

The LLM sits in the middle and cannot reach the broker without passing a gate it
has no ability to influence. It *selects* from a pre-vetted shortlist; it never
*constructs* a trade, sizes a position, or overrides a limit.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
git clone <your-repo-url>
cd Vetoed

uv venv --python 3.11
$env:VIRTUAL_ENV = "$PWD\.venv"
uv pip install alpaca-py python-dotenv alpaca-mcp-server mcp anthropic apscheduler fastapi uvicorn pytest

Copy-Item .env.example .env
notepad .env    # add your PAPER keys — never commit this file
```

```
ALPACA_API_KEY=PK...          # paper keys start with PK
ALPACA_SECRET_KEY=...
ALPACA_PAPER_TRADE=true       # must stay true
ANTHROPIC_API_KEY=sk-ant-...  # optional — see below
```

## Usage

```powershell
# verify connectivity: account, options level, chain, order round-trip
.venv\Scripts\python.exe scripts\check_setup.py --skip-order   # read-only
.venv\Scripts\python.exe scripts\check_setup.py                # + order test

# inspect what the deterministic screener finds (read-only)
.venv\Scripts\python.exe -m scripts.run_screener

# one full cycle, DRY RUN — no orders submitted
.venv\Scripts\python.exe -m agent.loop --force
.venv\Scripts\python.exe -m agent.loop --force --no-llm   # without Claude

# LIVE — submits real paper orders
.venv\Scripts\python.exe -m agent.loop --live

# unattended, on schedule (weekdays 10:00–15:30 ET, every 30 min)
.venv\Scripts\python.exe -m agent.loop --live --schedule

# dashboard
.venv\Scripts\python.exe -m uvicorn dashboard.api:app --port 8000
```

`--live` is never the default.

### Running without an Anthropic key

The agent is **fully functional with Alpaca alone**. With no `ANTHROPIC_API_KEY`,
with `--no-llm`, or if the Claude API errors mid-session, `brain.py` falls back
to deterministic selection. Every risk gate is identical in both modes — the
difference is judgement, not protection.

## Running Vetoed Unattended

Vetoed is built to run on a small Linux VPS so the agent does not depend on a
laptop being awake. It is **Alpaca paper trading only** — there is no
live-capital mode, and the process refuses to start without
`ALPACA_PAPER_TRADE=true`.

> **Vetoed is not a 24-hour trading process.** It stays online continuously,
> but it only evaluates candidates and opens trades during the configured US
> market session. Outside those hours every cycle exits immediately after
> checking Alpaca's clock.

### 1. VPS requirements

Ubuntu 22.04 or 24.04, 1 vCPU, 1 GB RAM, ~2 GB disk. The agent is
network-bound, not compute-bound — a screen takes about 20 seconds and is
mostly waiting on Alpaca. Any $5/month instance is enough.

### 2. Install

```bash
git clone https://github.com/Chong1120/Vetoed.git
cd Vetoed
sudo bash deploy/install.sh
```

That creates a `vetoed` system user, installs to `/opt/vetoed`, builds a
virtualenv, installs both systemd units, and runs the test suite. **It does not
start the agent** — starting a trading process as a side effect of an install
script would be the wrong default.

### 3. Configure

```bash
sudo -u vetoed nano /opt/vetoed/.env
```

| Variable | Required | Notes |
|---|---|---|
| `ALPACA_API_KEY` | yes | Paper key, starts `PK` |
| `ALPACA_SECRET_KEY` | yes | |
| `ALPACA_PAPER_TRADE` | **yes** | Must be exactly `true` or the process exits |
| `ANTHROPIC_API_KEY` | no | Absent → deterministic selection, agent still runs |
| `POLL_INTERVAL_MINUTES` | no | Default `30`. Range 1–240 |

Secrets live only in `/opt/vetoed/.env`, mode `600`, owned by `vetoed`. They
are **not** in the systemd unit, which is world-readable. `.env` is gitignored
and has never been committed.

### 4. Paper-trading requirement

Enforced in four places, and none of them are overridable:

| Where | What happens |
|---|---|
| `loop.assert_paper_trading()` | Process exits before any network call |
| `data.load_keys()` | Raises before market data is fetched |
| `executor._child_env()` | Raises before the MCP server is spawned |
| `scripts/check_setup.py` | Aborts the connectivity test |

There is no flag, environment variable, or code path that enables live capital.

### 5. Start

```bash
# Prove one cycle works first — dry run, submits nothing
cd /opt/vetoed
sudo -u vetoed .venv/bin/python -m agent.loop --force

# Then run it for real (paper account)
sudo systemctl enable --now vetoed
```

`enable` registers it for boot; `--now` starts it immediately.

### 6. Check status

```bash
systemctl status vetoed
```

Look for `Active: active (running)` and the startup banner showing the poll
interval and mode.

### 7. View logs

```bash
journalctl -u vetoed -f                    # follow live
journalctl -u vetoed --since "1 hour ago"  # recent
journalctl -u vetoed -p err                # errors only
journalctl -u vetoed | grep -E "ORDER|VETO|reconcile"
```

Every line is `ISO-8601 UTC  LEVEL  message`.

### 8. Restart

```bash
sudo systemctl restart vetoed
```

Safe at any time. SIGTERM lets the in-flight cycle finish and release its lock;
the next cycle reconciles against the broker before doing anything.

### 9. Stop

```bash
sudo systemctl stop vetoed              # stop now
sudo systemctl disable vetoed           # and don't start at boot
```

Stopping does **not** close open positions. Exits are evaluated by the running
agent, so a stopped agent stops managing. To flatten, close positions in the
Alpaca dashboard.

### 10. Verify it is alive

```bash
systemctl is-active vetoed                       # -> active
cat /opt/vetoed/journal/health.json              # heartbeat
journalctl -u vetoed --since "2 hours ago" | grep "cycle start"
```

Or over HTTP, if the dashboard unit is running:

```bash
sudo systemctl enable --now vetoed-dashboard
ssh -N -L 8000:127.0.0.1:8000 you@your-vps
curl -s localhost:8000/api/health | python3 -m json.tool
```

`/api/health` returns **200** when the last heartbeat is recent and **503**
when it is stale, so a plain uptime monitor can watch the URL without parsing
the body. It reports process liveness, last successful cycle, market status,
last error, equity, and open positions. It has no route that can place an
order.

The dashboard binds to `127.0.0.1` deliberately — it shows account equity and
position detail. Reach it through an SSH tunnel rather than exposing it.

### 11. Deploy a new version

```bash
cd /opt/vetoed
sudo -u vetoed git pull
sudo -u vetoed .venv/bin/pip install -q -e '.[dashboard]'
sudo -u vetoed .venv/bin/python -m pytest -q     # gate the deploy on green
sudo systemctl restart vetoed
```

Or re-run `sudo bash deploy/install.sh`, which is idempotent.

Restarting mid-session is safe by design: the next cycle reconciles against
Alpaca before evaluating anything, so a deploy cannot produce a duplicate
position.

### What makes a restart safe

A remote process restarts for reasons you do not control. Three mechanisms
stop that becoming a duplicate trade:

- **Broker reconciliation.** Risk gates are fed positions and open orders from
  Alpaca, not from the local journal. The journal records what this process
  *believes*; a crash between submitting and journalling makes those differ.
- **Deterministic `client_order_id`.** Derived from the trade intent and the
  date, so a retry produces the *same* id and Alpaca rejects it as a
  duplicate. The previous timestamp-based id threw that protection away.
- **Single-flight lock.** A cross-process lock file, so a systemd restart or a
  manual run cannot overlap a cycle already in flight.

An order whose fate is unknown — a timeout, not a rejection — is journalled
`uncertain` and **counted as live risk** until the broker resolves it.
Over-counting costs one skipped trade; under-counting can double a position.
There are no blind retries anywhere in the order path.

Full detail in [`docs/deploy.md`](docs/deploy.md).

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

**184 tests** covering the risk gates (naked-short rejection, loss-cap
verification, daily loss stop, concentration, sizing), the brain's defensive
JSON parsing (hallucinated legs, flipped sides, garbage types), the probability
maths, and the guardrail restrict-only invariant.

## Safety properties

| Property | How it is enforced |
|---|---|
| Never naked short | Structure gate verifies the long leg exists and sits further OTM; executor re-checks; both legs move as one atomic `mleg` order |
| Loss always capped | `max_loss` reconciled against `width × 100 − credit × 100` |
| LLM cannot size | `risk.size_position()` is a pure function; the model's `contracts` field is discarded |
| LLM cannot invent a trade | Echoed legs verified against the shortlist; mismatch → no-trade |
| Guardrails cannot loosen | `adapt.build()` clamps every override back to the defaults |
| Prompt boundary | No MCP output reaches the model. Everything in the prompt is numeric or from a vocabulary this repo controls — enforced by a whitelist in `build_prompt()`, tested by trying to smuggle an instruction through the error channel |
| Paper only | `ALPACA_PAPER_TRADE=true` asserted at every entry point |
| Bounded bad day | Daily loss stop halts the session at −3% |

## Repo layout

```
agent/      data · screener · brain · risk · adapt · executor · journal · loop
dashboard/  api.py + static/    read-only view of the journal
scripts/    check_setup.py · run_screener.py
tests/      risk gates · brain parsing · probability maths · guardrails
docs/       writeup.md  ← the one-page submission write-up
journal/    trades.db (gitignored)
```

## Security

`.gitignore` was written **before** any credential existed and is verified with
`git check-ignore`. `.env`, `*.db`, `.venv/`, and MCP config files are excluded.
`.env.example` holds placeholders only, and `check_setup.py` hard-fails if a
real-looking key is ever found in it.

## Limitations

See [`docs/writeup.md`](docs/writeup.md) §7 — no OPRA entitlement, no option
volume from Alpaca, IV rank substituted with IV-vs-realised, lognormal
underestimates tails, and **five days is statistical noise**.
