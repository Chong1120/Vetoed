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
FEATHERLESS_API_KEY=rc_...    # optional — see below
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

# unattended, one session job (weekdays; analyses every 5 min, opens every 30)
.venv\Scripts\python.exe -m agent.loop --live --schedule

# dashboard
.venv\Scripts\python.exe -m uvicorn dashboard.api:app --port 8000
```

`--live` is never the default.

### Running without an Anthropic key

The agent is **fully functional with Alpaca alone**. With no LLM provider,
with `--no-llm`, or if the Claude API errors mid-session, `brain.py` falls back
to deterministic selection. Every risk gate is identical in both modes — the
difference is judgement, not protection.

## Running Vetoed Unattended

Vetoed runs itself on **GitHub Actions**. No server, no cost, nothing to keep
powered on. It is **Alpaca paper trading only** — there is no live-capital
mode, and the workflow refuses to run without `ALPACA_PAPER_TRADE=true`.

> **Vetoed is not a 24-hour process, and does not need to be.** Each tick is a
> fresh container that runs one cycle and exits. It evaluates candidates and
> opens trades only during the US market session; ticks outside it exit in
> about a second having done nothing.

### How it stays honest without a server

Every run starts with an empty machine and a journal checked out from git —
which is a stale-state restart by definition. That is exactly what
[`agent/reconcile.py`](agent/reconcile.py) was built for:

- **The broker is authoritative.** Risk gates are fed positions and open orders
  read from Alpaca, never from the journal.
- **Deterministic `client_order_id`.** Derived from the trade intent and the
  date, so a re-run cannot fill the same spread twice — Alpaca rejects the
  duplicate id.
- **`concurrency` group.** GitHub will not start a second cycle while one is
  in flight, with `cancel-in-progress: false` so a cycle is never killed
  mid-submission.

### 1. Add repository secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Required | Notes |
|---|---|---|
| `ALPACA_API_KEY` | yes | Paper key, starts `PK` |
| `ALPACA_SECRET_KEY` | yes | |
| `ALPACA_PAPER_TRADE` | **yes** | Must be exactly `true` or the workflow fails |
| `FEATHERLESS_API_KEY` | no | Judgement layer. Absent → deterministic selection, agent still trades |
| `ANTHROPIC_API_KEY` | no | Alternative provider, used only if Featherless is absent |

GitHub encrypts secrets and masks them in logs automatically.

### 2. Enable the workflow

**Actions → Vetoed agent → Enable workflow.** From then on it fires on its own.

### 3. Prove it works without waiting for a tick

**Actions → Vetoed agent → Run workflow.** It defaults to **dry-run**, which
journals a full decision but submits nothing — safe to click, and the fastest
way to demo autonomy.

Set `force: true` to run against a closed market (dry-run only; the workflow
will not combine `--force` with `--live`).

### 4. Schedule

```
cron: "7,37 13-21 * * 1-5"      # UTC, Mon–Fri
```

It analyses approximately every 5 minutes and may open a position on at most one pass in six, so entries stay ~30 minutes apart. Exits are checked on every pass: a late entry costs nothing, a late exit is the real exposure. The window is deliberately wide — 13:00–21:00
UTC covers the US session under both EDT and EST, so no daylight-saving change
can silently stop it.

The window is only a coarse filter. **Alpaca's own clock is checked at the top
of every cycle and is authoritative**, because it knows holidays and early
closes that no cron expression does.

### 5. Watch it

| | |
|---|---|
| Runs and logs | **Actions → Vetoed agent** |
| Per-cycle summary | Click any run — equity, open positions, last outcome |
| Decisions and P&L | <https://chong1120.github.io/Vetoed/> |
| Raw state | `journal/health.json`, committed after each cycle |

The agent commits `journal/trades.db` back after every cycle, which also
triggers the Pages rebuild — so the public dashboard updates itself.

### 6. Check what it actually did

**A green workflow run is not evidence the agent worked.** Twice during
development a cycle finished, journalled an order and reported success while
the judgement layer had silently degraded to arithmetic — once because a
Cloudflare block looked like a bad key, once because an undefined GitHub
variable expanded to an empty model name. Neither was visible in the run
status.

So read the journal, not the checkmark:

```bash
git pull                                  # the agent commits its journal back
python scripts/show_journal.py            # last 10 cycles
python scripts/show_journal.py --all      # everything
python scripts/show_journal.py --check    # warnings only; exit 1 if any
```

It prints equity and realised P&L, every cycle with its guardrail notes, every
decision labelled **MODEL** or **ARITHMETIC**, every order with its status, and
then flags what is easy to miss:

| Warning | Means |
|---|---|
| *every decision came from arithmetic* | The model is configured but never answering — run `scripts/check_llm.py` |
| *N fell back while M came from the model* | Intermittent failures; the `ERROR:` lines name the cause |
| *N orders are UNCERTAIN* | We do not know whether Alpaca received them. Counted as live risk until the next cycle reconciles |
| *N dry-run orders and no live ones* | Nothing has reached the broker |

The three lines worth knowing by sight:

```
#8  ... open_spread  approved   MODEL        <- the model chose
    AAPL's put credit spread ... VRP edge of $42.34 ...
#7  ... open_spread  approved   ARITHMETIC   <- it did not; see ERROR below
    ERROR: HTTPError 422: The model must be provided in the request
```

Two other views of the same data: the workflow run's **Summary** tab shows
`health.json` for that cycle, and <https://chong1120.github.io/Vetoed/> renders
the whole journal as a page.

### 7. Stop it

**Actions → Vetoed agent → ⋯ → Disable workflow.** Takes effect immediately.

Stopping does **not** close open positions. Exits are evaluated by the running
agent, so a disabled workflow stops managing. To flatten, close positions in
the Alpaca dashboard.

### 8. Deploy a new version

`git push` to `main`. The next tick uses it. Tests run separately on push, so
check Actions is green before letting a tick pick up a change.

### What you give up

Honest accounting, because this is the real trade-off:

- **Punctuality.** GitHub schedules are best-effort — runs can be delayed under
  load, and can be dropped. A late *entry* costs nothing. A late *exit check*
  is the actual exposure: a stop-loss evaluated 30 minutes late is real
  slippage. If that matters more than cost, the agent runs equally well under
  any scheduler on a small VPS — `python -m agent.loop --live --schedule`, with
  `POLL_INTERVAL_MINUTES` controlling the interval.
- **Public logs.** This repo is public, so workflow logs are too. Equity and
  positions are visible. Secrets are masked by GitHub; the account itself is
  paper, and the same figures are already on the public dashboard.
- **Granularity is unchanged.** Exits are evaluated per cycle in every design,
  including on a dedicated server. Nothing watches positions continuously.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

**207 tests** covering the risk gates (naked-short rejection, loss-cap
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
