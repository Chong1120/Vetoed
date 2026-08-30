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

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

**144 tests** covering the risk gates (naked-short rejection, loss-cap
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
