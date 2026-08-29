# Alpha Options Agent

An autonomous AI trading agent that sells **defined-risk options credit spreads**
on SPY and QQQ, running unattended on **Alpaca paper trading** through Alpaca's
official **MCP server**.

Built for the [lablab.ai × Alpaca AI Trading Agents hackathon](https://lablab.ai)
(28 Aug – 4 Sept 2026).

> **Paper trading only.** `ALPACA_PAPER_TRADE=true` is asserted at every entry
> point and the process refuses to start without it. No real capital is ever at
> risk. This is not financial advice.

---

## The idea in one paragraph

Selling a credit spread pays you up front to take the other side of a move that
probably will not happen. You win if the underlying *does nothing* — no
directional call required. The risk is that "probably" occasionally means "not
this time", so every position is **structurally capped**: for each short leg
there is a long leg further out of the money, and the maximum loss is
`width × 100 − credit`, known before the order is sent. The agent's job is to
find spreads where the premium is worth the risk, and to be strictly forbidden
from taking any position where it is not.

## Architecture

```
                        ┌──────────────────────┐
                        │  loop.py  scheduler  │   APScheduler, US market hours
                        └──────────┬───────────┘
                                   │  each cycle:
             ┌─────────────────────┴─────────────────────┐
             │  0. manage open positions FIRST           │  realise P&L
             └─────────────────────┬─────────────────────┘
                                   ▼
     ┌───────────────┐    ┌─────────────────┐    ┌──────────────────┐
     │  data.py      │───▶│  screener.py    │───▶│  brain.py        │
     │  Alpaca APIs  │    │  DETERMINISTIC  │    │  Claude          │
     │  chain+Greeks │    │  no LLM         │    │  strict JSON     │
     └───────────────┘    └─────────────────┘    └────────┬─────────┘
                                                          │ selects one
                                                          ▼
                                                 ┌──────────────────┐
                                                 │  risk.py         │
                                                 │  HARD GATES      │
                                                 │  can VETO        │
                                                 │  always SIZES    │
                                                 └────────┬─────────┘
                                                          │ approved only
                                                          ▼
                                                 ┌──────────────────┐
                                                 │  executor.py     │
                                                 │  Alpaca MCP      │
                                                 │  atomic mleg     │
                                                 └────────┬─────────┘
                                                          ▼
                                        journal/trades.db  ──▶  dashboard/
```

**Why this shape:** the LLM sits in the middle, and cannot reach the broker
without passing a deterministic gate that it has no ability to influence. It
*selects* from a pre-vetted shortlist; it never *constructs* a trade, never
sizes a position, and never overrides a limit.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
git clone <your-repo-url>
cd alpha-options-agent

uv venv --python 3.11
$env:VIRTUAL_ENV = "$PWD\.venv"
uv pip install alpaca-py python-dotenv alpaca-mcp-server mcp anthropic apscheduler fastapi uvicorn pytest

Copy-Item .env.example .env
notepad .env    # add your PAPER keys — never commit this file
```

`.env`:

```
ALPACA_API_KEY=PK...          # paper keys start with PK
ALPACA_SECRET_KEY=...
ALPACA_PAPER_TRADE=true       # must stay true
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```powershell
# 1. verify connectivity: account, options level, chain, order round-trip
.venv\Scripts\python.exe scripts\check_setup.py --skip-order   # read-only
.venv\Scripts\python.exe scripts\check_setup.py                # + order test

# 2. inspect what the deterministic screener finds (read-only)
.venv\Scripts\python.exe -m scripts.run_screener

# 3. one full cycle, DRY RUN — no orders submitted
.venv\Scripts\python.exe -m agent.loop --force

# 3b. same cycle with NO Claude at all — pure deterministic selection
.venv\Scripts\python.exe -m agent.loop --force --no-llm

# 4. LIVE — submits real paper orders
.venv\Scripts\python.exe -m agent.loop --live

# 5. unattended, on schedule (weekdays 10:00–15:30 ET, every 30 min)
.venv\Scripts\python.exe -m agent.loop --live --schedule

# 6. dashboard
.venv\Scripts\python.exe -m uvicorn dashboard.api:app --port 8000
```

`--live` is never the default. Every command above is a dry run unless you pass it.

### Running without an Anthropic key

The agent is **fully functional with Alpaca alone**. If no `ANTHROPIC_API_KEY`
is configured — or if `--no-llm` is passed, or if the Claude API errors
mid-session — `brain.py` falls back to deterministic selection: the highest
expected value per dollar risked that clears `POP >= 0.60` and `EV >= $2.00`.

Every risk gate is identical in both modes, so the safety properties never
depend on whether the LLM ran. The difference is judgement, not protection.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests\ -q
```

36 tests covering the risk gates (naked-short rejection, loss-cap verification,
daily loss stop, concentration, sizing) and the brain's defensive JSON parsing
(hallucinated legs, flipped sides, out-of-range indices, garbage types).

## Safety properties

| Property | How it is enforced |
|---|---|
| Never naked short | Structure gate verifies the long leg exists and sits further OTM; executor re-checks before submit; both legs move as one atomic `mleg` order |
| Loss always capped | `max_loss` reconciled against `width × 100 − credit × 100` before approval |
| LLM cannot size | `risk.size_position()` is a pure function of candidate + account; the model's `contracts` field is discarded |
| LLM cannot invent a trade | Echoed legs verified against the shortlist entry; mismatch → no-trade |
| No prompt injection | MCP output marked `untrusted_tool_output` never enters the model's context |
| Paper only | `ALPACA_PAPER_TRADE=true` asserted at every entry point |
| Bounded bad day | Daily loss stop halts the session at −3% |

## Repo layout

```
agent/      data · screener · brain · risk · executor · journal · loop
dashboard/  api.py + static/    read-only view of the journal
scripts/    check_setup.py · run_screener.py
tests/      risk gates + brain parsing
docs/       writeup.md  ← the one-page submission write-up
journal/    trades.db (gitignored)
```

## Security

`.gitignore` was written **before** any credential existed and is verified with
`git check-ignore`. `.env`, `*.db`, `.venv/`, and MCP config files are excluded.
`.env.example` contains placeholders only, and `check_setup.py` hard-fails if a
real-looking key is ever found in it.

## Limitations

See [`docs/writeup.md`](docs/writeup.md) §4. In short: no OPRA entitlement
(indicative quotes, not true NBBO), IV rank substituted with IV-vs-realised
volatility, and delta used as a risk-neutral probability proxy.
