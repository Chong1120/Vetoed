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
- **CBOE PUT Index** (since 1986) — 9.9% annualised volatility vs 14.9% for the
  S&P 500, higher Sharpe over 32.5 years

**We harvest a risk premium; we do not make a prediction.**

## The measurement that makes the edge explicit

Delta is the *risk-neutral* probability of finishing in-the-money — and under
risk-neutral pricing, every fairly-priced option trade has expected value of
exactly **zero**. Ranking candidates by delta-derived EV therefore measures
nothing.

So the screener computes EV under **two measures**:

| Quantity | Measure | Source |
|---|---|---|
| Credit received | risk-neutral | market quote (contains the premium) |
| Probability of loss | **real-world** | 20-day realised volatility |

`vrp_edge = ev_rw − ev_rn` is the premium being harvested, as an inspectable
number. Live output behaves exactly as theory predicts: risk-neutral EVs cluster
near zero while real-world EVs are positive, and underlyings whose implied vol
sits *below* realised produce **no candidates at all**.

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

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

**68 tests** covering the risk gates (naked-short rejection, loss-cap
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
| No prompt injection | MCP output marked `untrusted_tool_output` never enters the model's context |
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
