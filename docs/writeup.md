# Vetoed — one-page write-up

**An autonomous options agent where the AI is the least-trusted component.**

Claude selects from a pre-screened shortlist; deterministic risk gates it cannot
influence decide what is permitted and how large — and can veto it entirely.
Every decision, including every rejection, is logged and auditable.

Built for the lablab.ai × Alpaca AI Trading Agents hackathon (28 Aug – 4 Sept 2026).
Alpaca paper account `PA3S1NN3SHKV` · options level 3 · $100,000 starting equity.

---

## 1. Strategy: harvesting a documented risk premium

The agent sells **defined-risk vertical credit spreads** on SPY, QQQ, IWM and
AAPL. It profits when the underlying *does not move much* — it takes no
directional view.

This is not a hand-picked heuristic. It targets the **volatility risk premium**,
one of the better-documented anomalies in empirical finance:

| Source | Finding |
|---|---|
| **Bakshi & Kapadia (2003)**, *Review of Financial Studies* 16(2), 527–566 | Delta-hedged S&P 500 option portfolios **underperform zero**; underperformance is greater at higher volatility. Option *sellers* are compensated. |
| **Carr & Wu (2009)**, *RFS* 22(3), 1311–1341 | Variance risk premiums quantified across 5 indices and 35 individual stocks. |
| **CBOE PUT Index** (June 1986 onward) | Put-writing: **9.9%** annualised volatility vs **14.9%** for the S&P 500, with higher Sharpe and Sortino ratios over 32.5 years. |

The distinction from technical-indicator strategies matters:
**we harvest a risk premium, we do not make a prediction.** A premium exists
because someone is paying for insurance — it does not require the market to be
inefficient.

## 2. The measurement that makes the edge explicit

Most retail options tooling ranks trades by delta-derived probability. **That is
mathematically empty.** Delta is the *risk-neutral* probability of finishing
in-the-money, and under risk-neutral pricing every fairly-priced option trade
has an expected value of exactly **zero** — a no-arbitrage identity.

So `screener.py` computes EV under **two different probability measures**:

| Quantity | Measure | Source |
|---|---|---|
| Credit received | risk-neutral | the market quote (contains the premium) |
| Probability of loss | **real-world** | 20-day realised volatility |

```
p(keep credit)  = 1 − P(short strike breached)
p(partial loss) = P(short breached) − P(long breached)   → ~½ max loss
p(max loss)     = P(long strike breached)

ev_rn  ← delta            (should hover near zero for fair quotes)
ev_rw  ← realised vol     (what we actually rank on)
vrp_edge = ev_rw − ev_rn  ← the premium being harvested, as a number
```

Live output confirms the theory: risk-neutral EVs cluster around zero
(−8.10, −1.53, +0.41, +3.21) while real-world EVs are positive. AAPL, with
implied 23.8% against realised 18.9%, showed the largest edge at **$29.92**.
QQQ — implied *below* realised — produced **zero candidates** and correctly
sat out.

## 3. Architecture

```
     deterministic              LLM                deterministic
  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │  screener.py     │─▶│  brain.py        │─▶│  risk.py         │
  │  what is VALID   │  │  what is GOOD    │  │  what is ALLOWED │
  │  no LLM          │  │  Claude          │  │  no LLM, VETOES  │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**`brain.py` — the model SELECTS, it does not CONSTRUCT.** The response carries
a `candidate_id` indexing the shortlist; echoed legs are verified against that
entry exactly. A hallucinated OCC symbol, invented strike, or flipped side fails
validation and becomes a no-trade. The model also returns `contracts` — **it is
discarded.** Sizing is not a judgement call.

**Graceful degradation.** No Anthropic key, an API error, or `--no-llm` all fall
back to deterministic selection. The agent keeps trading autonomously; every
risk gate is identical in both modes. Safety is a property of the architecture,
not of whether the model answered.

## 4. Risk gates

| Gate | Rule |
|---|---|
| Session halt | Day P&L ≤ −3% → no further trades that session |
| **Structure** | Both legs present; long leg strictly further OTM; `max_loss` reconciled against `width × 100 − credit × 100`. **Never naked.** |
| DTE | 2–14. 1DTE refused — the gamma cliff is not worth the theta |
| Liquidity | Per-underlying OI floors (SPY/QQQ 500, IWM 250, AAPL 100); short leg bid ≥ $0.10, long leg ≥ $0.02; spread ≤ $0.05 **or** ≤ 10% |
| Volatility | IV outside 3%–150% rejected |
| Concentration | ≤ 5 positions, ≤ 2 per underlying |
| **Sizing** | ≤ 5% equity per position, ≤ 25% portfolio, ≤ 25 contracts, ≤ 50% options buying power |
| Trend | No put spreads below the 20-day average; no call spreads above it |

**Exits** run *before* new entries each cycle: +50% of max profit, −2× credit,
1 DTE, or **short-leg delta doubling** — an early warning that converts some
max-losses into partial losses.

**68 tests**, covering every gate that must never fail open: missing long leg,
long strike on the wrong side, tampered `max_loss`, flipped legs, and the model
attempting to influence size.

## 5. Adaptive guardrails — deliberately not "self-improving AI"

Over five days the agent closes perhaps 10–20 trades. Distinguishing a 60% from
a 70% win rate needs **hundreds**. Claiming a model learned from a dozen samples
would be overclaiming, so adaptation is split by how much data backs it:

| Tier | Adapts from | Sample | Can it loosen? |
|---|---|---|---|
| **Regime** | implied vs realised volatility | thousands of observations | no |
| **Circuit breaker** | own closed trades | tiny | **no — only disables** |

A hard invariant, unit-tested: guardrails may only ever **restrict** relative to
the defaults. A small sample can make the agent more cautious, never more
reckless.

## 6. Alpaca infrastructure

- **MCP server** (`alpaca-mcp-server` 2.3.0) — every order routes through
  `place_option_order` on the official server, spawned as a stdio subprocess.
- **Atomic multi-leg.** Spreads enter *and* exit as one `mleg` order. Legging in
  would leave a naked short between fills.
- **`alpaca-py`** for bulk chain/Greeks screening — ~1,800 contracts per
  underlying per cycle, impractical as individual tool calls.
- **Prompt-injection surface: zero.** The MCP server marks responses
  `"trust": "untrusted_tool_output"`. Tool output is parsed for structured
  fields and never enters the model's context.
- **Paper-only**, asserted at every entry point.

## 7. Known limitations

Stated plainly, because they affect how the numbers should be read:

- **No OPRA entitlement** — quotes come from Alpaca's `indicative` feed, a
  derived estimate rather than true NBBO. Entry limits concede 5% off mid.
- **No option volume available.** Alpaca's snapshot exposes only open interest;
  fetching daily bars for ~2,400 contracts per cycle is impractical. OI alone is
  the liquidity proxy.
- **IV rank is not computable** — Alpaca exposes no IV history. `iv_vs_rv`
  (implied vs 20-day realised) is substituted.
- **Realised volatility is backward-looking**, and the lognormal model
  underestimates tail risk. Regime shifts break both.
- **The delta band (0.10–0.35) is convention, not an optimisation.** Bakshi &
  Kapadia support the upper half — premium is richer nearer the money — but the
  exact bounds, credit ratio, and OI floors are judgement calls.
- **Five days is statistical noise.** No strategy's edge is visible over a
  single week. Results should be read as a demonstration of process, not proof
  of edge.

## 8. Auditability

Every run, LLM rationale, risk veto, guardrail change, and order is written to
SQLite and rendered by a read-only FastAPI dashboard — including the trades the
risk layer refused. The dashboard has no route that can place, modify, or cancel
an order.

The agent is built so that its worst possible day is bounded by arithmetic
rather than by the model's judgement.
