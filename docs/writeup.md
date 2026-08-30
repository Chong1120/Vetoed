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
| **CBOE PUT Index** (Jun 1986–Dec 2018) | One-month **at-the-money cash-secured** put writing: 9.54% return at **9.95%** volatility, vs 9.80% at **14.93%** for the S&P 500. |

**What these do and do not establish.** They support the existence and
persistence of a volatility risk premium in equity index options. They do
**not** validate this agent. The PUT index in particular is a different
instrument on a different horizon — at-the-money cash-secured puts rolled
monthly, versus the 2–14 DTE out-of-the-money defined-risk spreads traded
here. It is the economic prior, not evidence about this strategy.

The distinction from technical-indicator strategies still matters:
**we aim to be paid for carrying volatility risk, not to predict direction.**
A premium can exist because someone is buying insurance — it does not require
the market to be inefficient.

## 2. The measurement that makes the edge explicit

Under the risk-neutral measure a fairly-priced trade has an expected P&L of
exactly **zero** — a no-arbitrage identity. So an EV built purely from
risk-neutral inputs cannot contain edge information. **The signal must be a
difference between two volatility parameterisations, not a level.**

Everything runs through **one model**: a zero-drift lognormal with
`E[S_T] = S₀` — no rates, no dividends, and deliberately no equity risk
premium. **Volatility is the only input that changes.**

| Quantity | Volatility fed in | What it estimates |
|---|---|---|
| Credit received | — | taken from the market quote, identical on both sides |
| `ev_rn` | short leg's **implied** vol | approximately how the market values this spread |
| `ev_rw` | 20-day **realised** vol | what it would be worth if vol matched recent history |
| **`vrp_edge`** | — | **`ev_rw − ev_rn`** — the volatility gap, in dollars |

The expected payoff is computed in **closed form, exactly** — not approximated:

```
payoff:  +max_profit          beyond the short strike
         linear ramp          between the strikes
         −max_loss            beyond the long strike

the ramp is integrated analytically using
    E[S_T · 1{S_T < K}] = S₀ · N(d(K) − σ√T)
```

**Two corrections got us here, and both changed decisions.**

*First,* an earlier build computed `ev_rn` from **delta**. Delta is N(d₁);
the risk-neutral probability of finishing ITM is N(d₂). They differ by roughly
σ√T — and the gap has **opposite sign for calls and puts**, so it biased the
two sides of the book in opposite directions. Journal run 3 traded IWM at
implied 14.84% against realised 14.58% — a ratio of 1.018, so essentially no
gap existed — and still printed `vrp_edge = 2.75`.

*Second,* the ramp between the strikes was valued at its **midpoint payoff**,
`(max_profit − max_loss) / 2`. That is only correct if `E[S_T | in the band]`
lands on the arithmetic midpoint, which a lognormal does not do. Across a
sweep of realistic candidates the error had a median of $0.13 but reached
**$37**, and exceeded the $2.00 gate in **17%** of cases — large enough to
flip trade decisions, not merely to misreport them.

Both are now pinned by tests. The decisive one checks the closed form against
a brute-force numerical integral of the payoff; the old midpoint rule fails it
by more than the gate threshold.

**The signal is the gate, not a footnote.** `vrp_edge` must clear $2.00 or the
spread is discarded, and the shortlist is ranked on `vrp_edge / max_loss` — not
on `ev_rw`. Ranking on `ev_rw` ranks on how *low* the 20-day realised-vol
estimate happened to come in, which is estimation error rather than
compensation. (Under a normal-iid assumption that estimate carries roughly 16%
relative standard error; real returns are fat-tailed and vol-clustered, so the
true figure is worse.) A spread whose implied vol is unavailable is dropped
outright: a signal that cannot be computed cannot be claimed.

One snapshot, counted at each gate — **20 structurally valid spreads, 13
rejected for carrying no measurable gap**. (This is a *historical* snapshot,
30 Aug 2026, market closed. Reproduce the current equivalent with
`python -m scripts.run_screener`; the numbers will differ.)

| Underlying | implied / realised | valid | cleared $2.00 | best signal |
|---|---|---|---|---|
| AAPL | **1.260** | 2 | **2** | **+$42.45** |
| SPY | 1.069 | 3 | 3 | +$5.44 |
| IWM | 1.067 | 13 | 2 | +$11.67 |
| QQQ | **0.894** | 2 | **0** | — |
| **total** | | **20** | **7** | |

QQQ is the one to look at: implied volatility *below* realised, so both its
structurally valid spreads scored a negative gap and neither was taken.

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

**144 tests**, covering every gate that must never fail open: missing long leg,
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
- **Prompt boundary, enforced by a whitelist.** No MCP output reaches the
  model, and `build_prompt()` copies across only numbers this codebase
  computed plus an exception class name — because a data-provider error string
  would otherwise have reached the prompt. Not "zero attack surface"; a
  controlled vocabulary, with a test that attempts to smuggle an instruction
  through. Separately, the MCP server marks responses
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
