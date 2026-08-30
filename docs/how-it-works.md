# How Vetoed works — complete technical walkthrough

This document explains every stage of the agent with a single worked example
carried end to end: an **AAPL 310/305 put credit spread, 12 DTE**.

Its starting quotes are round numbers so the arithmetic can be followed by
hand. **Every figure derived from them was computed by running the real
functions in the codebase**, not worked out by hand and not rounded for
convenience — so the intermediate values shown at each step are exactly what
the agent computes. Live screener output appears separately in §5.9, and every
figure in it is reproducible with `python -m scripts.run_screener`.

**Contents**

1. [Running it](#1-running-it)
2. [The economic thesis](#2-the-economic-thesis)
3. [Architecture](#3-architecture)
4. [Stage 1 — Market data](#4-stage-1--market-data)
5. [Stage 2 — Screening](#5-stage-2--screening)
6. [Why delta was the wrong probability](#6-why-delta-was-the-wrong-probability)
7. [Stage 3 — Adaptive guardrails](#7-stage-3--adaptive-guardrails)
8. [Stage 4 — The brain](#8-stage-4--the-brain)
9. [Stage 5 — Risk gates and sizing](#9-stage-5--risk-gates-and-sizing)
10. [Stage 6 — Execution](#10-stage-6--execution)
11. [Managing the position: profit, loss, time](#11-managing-the-position-profit-loss-time)
12. [The break-even arithmetic](#12-the-break-even-arithmetic)
13. [The journal and dashboard](#13-the-journal-and-dashboard)
14. [What happens when things break](#14-what-happens-when-things-break)
15. [Citation audit](#15-citation-audit)
16. [Known limitations](#16-known-limitations)

---

## 1. Running it

| Command | Effect |
|---|---|
| `python scripts/check_setup.py` | Connectivity proof. Places a $0.01 far-OTM buy order and cancels it in a `finally` block. |
| `python -m scripts.run_screener` | Read-only screen. Places nothing. Works when the market is closed. |
| `python -m agent.loop` | **One cycle, dry run.** This is the default — nothing reaches the broker. |
| `python -m agent.loop --live` | One cycle, orders actually submitted. |
| `python -m agent.loop --force` | Run even when the market is closed. |
| `python -m agent.loop --no-llm` | Skip Claude, use deterministic selection. |
| `python -m agent.loop --schedule` | Continuous. Weekdays 10:00–15:30 ET, every 30 minutes. |
| `uvicorn dashboard.api:app --port 8000` | Read-only dashboard over the journal. |

Dry run is the default everywhere. `--live` is the only way an order reaches
Alpaca, and `ALPACA_PAPER_TRADE=true` is asserted at three separate entry
points — `data.load_keys()`, `executor._child_env()`, and `check_setup.py`.

---

## 2. The economic thesis

The agent sells defined-risk credit spreads to harvest the **volatility risk
premium**. It does not predict direction.

The reasoning matters, because it rules out the obvious wrong approach:

> Under the **risk-neutral measure**, a fairly-priced trade has an expected
> P&L of exactly **zero**. That is a no-arbitrage identity, not an opinion.

So any EV built purely from risk-neutral inputs carries no edge information —
it can only reflect quoting noise and model error. **The signal has to be a
difference between two volatility parameterisations, not a level.**

Two things are worth separating carefully here, because it is easy to blur
them and the earlier version of this document did:

- **Delta is not a probability.** Delta is N(d₁). The risk-neutral probability
  of finishing in the money is N(d₂) = N(d₁ − σ√T). They are close, but the
  gap has **opposite sign for calls and puts** (§6).
- **Option selling is not free money.** The hypothesis is that implied
  volatility tends to exceed subsequently realised volatility, so a seller is
  compensated for bearing volatility risk. That is an empirical regularity
  with an economic rationale — someone is buying insurance — not an
  arbitrage.

The literature motivating the hypothesis:

- **Bakshi & Kapadia (2003)**, *Review of Financial Studies* 16(2), 527–566.
  Delta-hedged S&P 500 option portfolios underperform zero, the
  underperformance is less for away-from-the-money options, and it is greater
  at times of higher volatility.
- **Carr & Wu (2009)**, *Review of Financial Studies* 22(3), 1311–1341.
  Variance risk premiums quantified across five stock indices and 35
  individual stocks.
- **CBOE PUT index**, 30 June 1986 – 31 Dec 2018. Compound return 9.54% versus
  9.80% for the S&P 500, at a standard deviation of **9.95% versus 14.93%** —
  near-identical return at two-thirds the volatility.

**What this literature does and does not establish.** It supports the
existence and persistence of a volatility risk premium in equity index
options. It does **not** validate this agent, and three gaps are worth naming
before a judge names them:

| | The cited work | What Vetoed does |
|---|---|---|
| Instrument | Delta-hedged options; cash-secured ATM puts | Defined-risk vertical credit spreads |
| Horizon | One month (PUT index); monthly rebalancing | 2–14 DTE |
| Moneyness | At-the-money | 0.10–0.35 delta, out-of-the-money |

So the papers are the **economic prior**. They are not evidence about this
strategy, this horizon, or these thresholds. The agent's job is narrow and
testable on its own terms: **measure the implied-versus-realised gap per
trade, take the trades where it is large, refuse the trades where it is not.**

---

## 3. Architecture

```
   DETERMINISTIC          JUDGEMENT           DETERMINISTIC        BROKER
 ┌────────────────┐   ┌───────────────┐   ┌────────────────┐   ┌──────────┐
 │  screener.py   │──▶│   brain.py    │──▶│    risk.py     │──▶│ executor │
 │ what is VALID  │   │ what is GOOD  │   │ what is ALLOWED│   │   MCP    │
 └────────────────┘   └───────────────┘   └────────────────┘   └──────────┘
        ▲                                          │                 │
        │                                          ▼                 ▼
 ┌────────────────┐                        ┌─────────────────────────────┐
 │   adapt.py     │                        │        journal.py           │
 │ tightens rails │                        │  every decision, forever    │
 └────────────────┘                        └─────────────────────────────┘
        ▲                                                 │
 ┌────────────────┐                                       ▼
 │    data.py     │                              ┌──────────────────┐
 │  Alpaca feeds  │                              │  dashboard/      │
 └────────────────┘                              └──────────────────┘
```

The central claim is **separation of powers**. The language model has
authority over exactly one thing: which of N pre-vetted spreads to take, if
any. It has no authority over anything else.

| Module | Owns | Cannot do |
|---|---|---|
| `screener.py` | What is structurally valid and tradable. Pure arithmetic. | Judge attractiveness, place orders |
| `brain.py` | Which shortlisted spread is attractive now | Invent a leg, alter a strike, set size |
| `risk.py` | What is permitted, and how large | Be overridden at any confidence |
| `adapt.py` | Tightening rails from regime and history | Loosen anything — clamped structurally |
| `executor.py` | The only write path to the broker | Leg into a position |

---

## 4. Stage 1 — Market data

`agent/data.py`

### 4.1 Why two APIs are merged

Neither Alpaca surface alone is sufficient:

| API | Provides | Missing |
|---|---|---|
| Trading API `get_option_contracts` | strike, expiry, **open interest**, tradability | quotes, Greeks |
| Market Data API `get_option_snapshot` | bid/ask, delta/gamma/theta/vega, **implied volatility** | open interest |

So `snapshot()` calls both and joins them on the OCC symbol. Open interest is
served separately by Alpaca with a one-day lag reflecting OCC's end-of-day
calculation. Contracts without a two-sided quote are dropped.

### 4.2 The feed

On construction, `Market._detect_feed()` probes for an OPRA subscription and
falls back to `indicative`. **This account has no OPRA agreement, so every
quote is indicative** — a derived value, not the true NBBO. This is the single
largest source of error in the system and it sits upstream of everything else.

### 4.3 Realised volatility — worked example

One fetch of daily closes feeds both the volatility estimate and the 20-day
moving average. Given closes:

```
[312.40, 314.85, 311.20, 316.05, 318.90, 317.44, 321.10, 319.92]
```

**Step 1 — log returns.** `r_i = ln(C_i / C_{i-1})`

```
[0.007812, -0.011661, 0.015465, 0.008977, -0.004589, 0.011464, -0.003682]
```

**Step 2 — sample variance** (n−1 denominator, n = 7 returns):

```
mean          = 0.00339808
sample var    = 0.0001003232
daily std dev = sqrt(0.0001003232) = 0.010016
```

**Step 3 — annualise** by √252 trading days:

```
0.010016 x sqrt(252) = 0.159001  ->  15.90% annualised
```

Verified: `Market.realized_vol_from(closes)` returns `0.159001`.

The production call uses 21 closes (20 returns). A 20-day estimate carries
roughly **16% relative standard error** — `1/sqrt(2 x 19)` — which matters
later and is discussed in §15.

---

## 5. Stage 2 — Screening

`agent/screener.py`. Fully deterministic: identical market data always
produces identical output. No LLM touches this file.

### 5.1 The trend filter runs first

```python
if snap.above_trend:                    # spot > 20-day SMA
    build put credit spreads only
if not snap.above_trend:
    build call credit spreads only
```

Selling put spreads into a market already below its 20-day average is the
easiest way to turn a high-probability trade into a max loss — the short
strike gets approached by the very trend you ignored. Only the side the trend
runs against is blocked; the other side stays available.

### 5.2 Leg filters

| Filter | Short leg | Long leg | Why |
|---|---|---|---|
| Minimum bid | **$0.10** | **$0.02** | The short leg is sold and must pay real premium. The long leg is insurance we *want* cheap. |
| Open interest | SPY/QQQ 500 · IWM 250 · AAPL 100 | same | ETFs carry far deeper books; a flat threshold would delete every AAPL candidate. |
| Bid-ask | `≤ $0.05` **OR** `≤ 10% of mid` | same | A percentage-only test unfairly kills cheap options: $0.05/$0.07 is "33% wide" but costs two cents. |
| Short delta | 0.10 – 0.35 | — | Engineering choice. See the provenance table below. |
| Moneyness | must be OTM | must be further OTM | Defines the risk. |

#### Where each threshold actually comes from

A judge is entitled to ask which of these numbers are derived and which were
chosen. Honestly labelled:

| Threshold | Value | Provenance |
|---|---|---|
| Long leg further OTM | — | **Mathematically required.** Without it the loss is not capped. |
| `max_loss = width×100 − credit×100` | — | **Mathematically required.** Identity, re-checked by a risk gate. |
| Short delta band | 0.10–0.35 | **Engineering choice.** Bakshi & Kapadia find delta-hedged underperformance is *less* for away-from-the-money options, which motivates avoiding the far tail — but the paper says nothing about 0.10, or 0.35, or delta bands at all. |
| DTE | 2–14 | **Contest-design parameter.** Chosen so positions can open and close inside a one-week window, with 0–1 DTE excluded for gamma. |
| Credit / width | 0.12–0.60 | **Risk-control heuristic.** A sanity band: too low is poor compensation, too high usually means a stale quote or mis-paired strikes. |
| Open interest floors | 500/500/250/100 | **Liquidity heuristic**, hand-set per underlying because ETFs carry far deeper books than single names. |
| Bid-ask test | ≤$0.05 **or** ≤10% | **Liquidity heuristic.** The `or` exists because a percentage-only test unfairly kills cheap options. |
| `MIN_EV_RW` | $1.00 | **Engineering choice.** No derivation. |
| `MIN_VRP_EDGE` | $2.00 | **Engineering choice**, motivated by the noise in a 20-day vol estimate. No paper prescribes it. |
| Width preference | 0.80 / 1.00 / 1.00 | **Heuristic.** $1-wide spreads pay too little against two bid-ask crossings. |
| Risk limits (5% / 25% / 3%) | — | **Risk-control choices.** Conventional, not optimised. |

**None of the numeric thresholds are derived from the cited literature.** The
literature supports the existence of a volatility premium. Every specific
number in this system is an engineering decision, and would need out-of-sample
testing to justify beyond "it is defensible and conservative".

### 5.3 Building the spread — the worked example

The quotes below are representative rather than a transcript of one live
snapshot — they are round numbers so the arithmetic is easy to follow by hand.
Every figure derived from them was computed with the real functions in
`screener.py`. Actual live output for this universe appears in §5.9.

Raw quotes for the AAPL contracts, spot **$319.92**, 12 DTE:

```
short 310 put:  bid 3.05  ask 3.15   ->  mid = (3.05 + 3.15) / 2 = 3.100
long  305 put:  bid 2.15  ask 2.25   ->  mid = (2.15 + 2.25) / 2 = 2.200
```

**Credit and the credit ratio:**

```
credit       = 3.100 - 2.200 = 0.900
width        = 310 - 305     = 5.00
credit/width = 0.900 / 5     = 0.1800     must be within 0.12 - 0.60  PASS
```

The credit ratio band is a sanity check on both ends. Below 0.12 you are not
being paid enough for the risk; above 0.60 the "spread" is nearly at the money
and the quote is probably stale or the strikes are wrong.

**Payoff bounds:**

```
max_profit = credit x 100          = 0.900 x 100         = $90.00
max_loss   = width x 100 - max_profit = 5 x 100 - 90.00  = $410.00
```

That $100 multiplier is the standard US equity option contract size. The loss
is capped by construction: the long 305 put pays out below 305, so no matter
how far AAPL falls, the most this spread can lose is $410.

### 5.4 The model, stated precisely

This is the heart of the system, so it is worth being exact about what is
assumed. The terminal price is lognormal with **zero drift**:

```
ln S_T  ~  Normal( ln S₀ − ½σ²T ,  σ²T )        ⟹   E[S_T] = S₀

        ln(K/S) + ½σ²T
d   =  ─────────────────          P(S_T < K) = N(d)
            σ√T
```

Three consequences, each of which a judge is entitled to ask about:

- **`E[S_T] = S₀` means no rates, no dividends, and no equity risk premium.**
  We are not claiming the stock has zero expected return; we are declining to
  take a directional view. On the put side that is the conservative choice —
  a positive drift would flatter put spreads.
- **These are model probabilities, not observed frequencies.** `P(S_T < K)` is
  what a lognormal with the supplied σ implies. Feeding it realised volatility
  does **not** make it a "real-world probability" — it makes it a
  *counterfactual valuation* under the same martingale model with a different
  volatility. This document previously called it real-world. That was wrong on
  two counts: the drift is not the real-world drift, and the volatility is an
  estimate rather than a known parameter.
- **σ is the only thing that changes** between the two valuations below. That
  is the entire design.

**Time to expiry:**

```
T = 12 / 365 = 0.032877 years        sqrt(T) = 0.181319
```

Now the same function is called twice — **only the volatility changes**:

| Measure | σ | d (short 310) | P(breach) | d (long 305) | P(breach) |
|---|---|---|---|---|---|
| **Real-world** (realised) | 0.1891 | −0.90152 | **0.18366** | −1.37576 | **0.08445** |
| **Risk-neutral** (implied) | 0.2383 | −0.70739 | **0.23966** | −1.08372 | **0.13925** |

Implied volatility is higher than realised (23.83% vs 18.91%), so the market
prices a **larger** chance of breaching the strike than history suggests.
That difference is exactly what we are being paid for.

### 5.5 Expected value — computed exactly

A vertical credit spread has three payoff regions at expiry:

```
S_T ≥ 310   (beyond neither strike)   ->  keep the whole credit   +$90.00
305 < S_T < 310 (between the strikes) ->  payoff runs LINEARLY
                                          from +$90.00 at 310
                                          to   -$410.00 at 305
S_T ≤ 305   (beyond both strikes)     ->  full max loss          -$410.00
```

The middle band is where this is easy to get wrong, and where an earlier
version of this system **was** wrong. The payoff there is not "about half the
max loss" — at the short strike the spread still expires for the entire
credit. But nor is it the payoff at the midpoint of the band, which is what
the earlier code assumed. **That is only correct if `E[S_T | in the band]`
lands on the arithmetic midpoint, and under a lognormal it does not.**

The ramp is therefore integrated in closed form, using the standard result

```
E[S_T · 1{S_T < K}]  =  S₀ · N( d(K) − σ√T )
```

so the whole expectation is **exact** — no quadrature, no midpoint rule:

```
ev = max_profit · (1 − N(d_short))                        region 1
   + 100 · [ (credit − K_short)·(N(d_short) − N(d_long))
              + S₀·(N(d_short−σ√T) − N(d_long−σ√T)) ]      region 2, the ramp
   − max_loss · N(d_long)                                  region 3
```

**Valuation A — at 20-day realised volatility (σ = 0.1891):**

```
d_short = −0.90152  ->  N = 0.18366        d_long = −1.37576  ->  N = 0.08445
P(band) = 0.09921                          E[S·1{band}] = 30.5279
ramp    = 100 · [ (0.90 − 310)·0.09921 + 30.5279 ]  =  −13.7355

ev_rw = 90.00·(1 − 0.18366) − 13.7355 − 410.00·0.08445  =  +$25.11
pop   = 1 − 0.18366 = 0.8163
```

**Valuation B — at the short leg's implied volatility (σ = 0.2383):**

```
d_short = −0.70739  ->  N = 0.23966        d_long = −1.08372  ->  N = 0.13925
ramp    = −14.7340

ev_rn = 90.00·(1 − 0.23966) − 14.7340 − 410.00·0.13925  =  −$3.39
```

**How wrong was the old midpoint rule?** On this spread it gave $22.97 and
−$4.73 — errors of −$2.14 and −$1.33. Across a sweep of realistic candidates
the error had a **median of $0.13** but reached **$37**, and exceeded the
$2.00 gate in **17% of cases**. It was not a cosmetic inaccuracy; it could
flip a trade decision. `test_spread_ev_matches_numerical_integration` now
checks the closed form against a brute-force integral of the payoff across 54
parameter combinations, and a companion test asserts the old midpoint rule
would fail by more than the gate threshold.

### 5.6 The signal

```
vrp_edge = ev_rw − ev_rn = 25.11 − (−3.39) = +$28.51 per spread
```

**Both numbers came from the same closed form, on the same credit, under the
same zero-drift model.** The only input that differed was the volatility. That
is what makes the difference interpretable: it is the P&L attributable purely
to the implied-versus-realised volatility gap, and it is **exactly zero** when
the two volatilities agree — pinned to 1e-9 by a test.

**Two gates apply:**

```
ev_rw    ≥ $1.00   (MIN_EV_RW)     ->  25.11  PASS
vrp_edge ≥ $2.00   (MIN_VRP_EDGE)  ->  28.51  PASS
```

The $2.00 floor is an **engineering choice, not a derived quantity**. Its
rationale: below roughly that level the measured gap is comparable to the
noise in a 20-day volatility estimate, so it is not evidence of anything. No
paper prescribes $2.00.

**Why `ev_rn` is not zero.** Under exact risk-neutral pricing it would be. It
is not, for three reasons worth stating plainly:

1. **Skew is not modelled.** Both legs are priced at the *short* leg's implied
   volatility. Real vertical skew means the 305 put trades at a higher IV than
   the 310 put, so a single-vol model misprices the long leg — here making the
   observed credit look too small, which pushes `ev_rn` negative.
2. **The credit comes from mid quotes**, which are not fair value, and on this
   account are `indicative` rather than true NBBO (§4.2).
3. **No discounting.** At 2–14 DTE the effect is negligible, but it is an
   omission, not an absence.

So `ev_rn` should be read as *"what this spread is worth under our model when
fed the market's own volatility"*, and its distance from zero is a rough
indicator of how much skew and quote noise the single-vol simplification is
absorbing. It is not a claim about mispricing.

**And what `vrp_edge` is not.** It is not the variance risk premium of
Carr & Wu (2009), which is defined on variance swap rates over a matched
horizon. Ours is a spread-level dollar figure from one quote snapshot under a
simplified model. The literature explains *why* such a gap should tend to
exist; it does not certify this estimator. It is an operational screening
signal with an economic rationale — that is the honest description, and it is
the one used throughout this document.

**If the short leg has no implied volatility, the candidate is discarded.** A
signal that cannot be computed cannot be claimed.

### 5.7 Ranking

```
worst_spread_pct = max( (3.15-3.05)/3.100, (2.25-2.15)/2.200 )
                 = max( 0.0323, 0.0455 ) = 0.0455

score = (vrp_edge / max_loss) x (1 - worst_spread_pct) x width_preference
      = (28.51 / 410.00)      x (1 - 0.0455)          x 1.00
      = 0.0664
```

`vrp_edge / max_loss` is dimensionless (dollars per dollar), and the other two
factors are dimensionless multipliers, so the score is a **dimensionless
ranking heuristic**. It is not a utility function, a Sharpe ratio, or an
optimal objective — it is a defensible ordering, and no more than that.

Three factors, each earning its place:

- **`vrp_edge / max_loss`** — premium per dollar actually risked. Ranking on
  `ev_rw` instead would rank on how *low* the realised-vol estimate happened
  to come in, which is estimation error, not edge.
- **`(1 − worst_spread_pct)`** — the bid-ask spread is a real cost paid twice
  (entry and exit). A wide market is penalised proportionally.
- **`width_preference`** — `{1.0: 0.80, 2.0: 1.00, 5.0: 1.00}`. A $1-wide
  spread pays too little against a double bid-ask crossing.

### 5.8 Deduplication and balancing

`dedupe_best()` keeps only the highest-scoring width per
`(underlying, expiry, kind, short_strike)`. Without it the same short strike
appears three times at three widths.

`balanced_top()` then round-robins across `(underlying, direction)` buckets up
to `MAX_CANDIDATES = 8`. A pure top-N would happily return eight variations of
one directional bet — a concentration risk dressed up as a shortlist, leaving
the brain nothing to actually choose between.

### 5.9 Live output

**Labelled honestly: this is one historical snapshot**, taken 30 Aug 2026 with
the market closed, so quotes are the last known ones from the `indicative`
feed. Reproduce the current equivalent with `python -m scripts.run_screener` —
your numbers will differ, because the market will have moved.

Counted at each gate **within a single snapshot**, which an earlier version of
this document did not do (it compared a pre-gate count from one run against a
post-gate count from another):

| Underlying | implied/realised | structurally valid | cleared $2.00 | best signal |
|---|---|---|---|---|
| **AAPL** | **1.260** | 2 | **2** | **+$42.45** |
| SPY | 1.069 | 3 | 3 | +$5.44 |
| IWM | 1.067 | 13 | 2 | +$11.67 |
| **QQQ** | **0.894** | 2 | **0** | — |
| **total** | | **20** | **7** | 13 rejected |

Two things to read off it. AAPL, with implied volatility 26% above realised,
carries by far the largest signal. QQQ, where implied sits *below* realised,
produces **zero** candidates — its two structurally valid spreads both scored a
negative gap and were discarded.

**QQQ sitting out is the load-bearing observation.** The agent is not looking
for trades. It is looking for compensated risk, and it declines when the
measurement says there is none.

One caveat against over-reading this: 20 spreads on four correlated tickers at
one instant is an illustration of the mechanism, **not** evidence that the
mechanism is profitable. See §15.

---

## 6. Why delta was the wrong probability

An earlier build computed `ev_rn` from **delta** instead of from implied
volatility. This was a real defect, and understanding it explains the design.

For a Black-Scholes option:

```
call delta = N(d₁)              P(call finishes ITM) = N(d₂)
|put delta| = N(-d₁)            P(put finishes ITM)  = N(-d₂)

where  d₂ = d₁ - σ√T
```

**Delta is not the probability of finishing in the money.** It is off by
roughly σ√T, and — critically — **the error flips sign between calls and
puts**:

- For a **call**, `d₁ > d₂`, so `N(d₁) > N(d₂)`: delta **overstates** the
  breach probability.
- For a **put**, `−d₁ < −d₂`, so `N(−d₁) < N(−d₂)`: delta **understates** it.

On the worked example (a put), at the short leg's implied volatility:

```
short 310P:  |delta| = N(-d₁) = 0.22645   P(ITM) = N(-d₂) = 0.23966   gap = 0.01321
long  305P:  |delta| = N(-d₁) = 0.12989   P(ITM) = N(-d₂) = 0.13925   gap = 0.00936
```

Because delta *understates* the ITM probability for puts, it made the spread
look safer than the model says, inflating `ev_rn` and therefore **shrinking**
the reported gap. On a call spread the sign flips and it **inflates** the gap
instead.

The clearest demonstration is journal run 3: IWM traded at implied 14.84%
against realised 14.58% — a ratio of 1.018, so **essentially no gap existed** —
and the old code still reported `vrp_edge = 2.75`, of which only about $0.34
survived once both sides used one model.

That is the failure mode the design now forbids by construction: a signal
defined as a difference between two runs of *the same function* cannot report
a gap when its two inputs are equal.

The fix was to price both sides with the same `prob_below()`. Five regression
tests now pin the invariant, the most important being:

```python
def test_no_premium_is_reported_when_implied_equals_realised():
    assert _build_spreads(_snapshot(iv=0.147, realised=0.147), "call") == []
```

Equal volatilities must produce zero edge, and zero edge must fail the gate.

---

## 7. Stage 3 — Adaptive guardrails

`agent/adapt.py`. Runs **before** the main screen, off a one-symbol probe.

This is not machine learning and it is not self-improvement. Over a five-day
contest the agent might close 10–20 trades. Distinguishing a 60% win rate from
70% at any real statistical confidence needs **hundreds**. So adaptation is
split by how much data actually backs it.

### Tier 1 — Regime (thousands of price observations)

```
avg(iv/rv) >= 1.10  ->  premium is rich  ->  DTE 3-14 allowed
avg(iv/rv) <= 0.95  ->  premium is thin  ->  DTE 2-7 only
otherwise           ->  neutral          ->  defaults
```

Measured from price history, so it is allowed to matter.

### Tier 2 — Circuit breaker (our own trades, tiny sample)

```
fewer than 5 closed trades           ->  NO CHANGE, logged as such
3 consecutive losses on a symbol     ->  ban that underlying
>70% losers in the delta>=0.25 bucket ->  cap delta at 0.25
```

It can only ever **disable** something. It can never widen a limit, increase
size, or enable a setup.

### The hard invariant

After both tiers, every override is clamped against the screener's own
constants:

```python
g.dte_min   = max(g.dte_min,   screener.DTE_MIN)        # never below 2
g.dte_max   = min(g.dte_max,   screener.DTE_MAX)        # never above 14
g.delta_min = max(g.delta_min, screener.SHORT_DELTA_MIN)
g.delta_max = min(g.delta_max, screener.SHORT_DELTA_MAX)
```

**Guardrails can only ever restrict.** A small sample can make the agent more
cautious, never more reckless. This is enforced structurally, not by
convention, and is unit-tested in `tests/test_adapt.py`.

---

## 8. Stage 4 — The brain

`agent/brain.py`. Claude Sonnet 4.6, adaptive thinking, JSON-schema
structured output.

### What the model sees

Only screener-derived numbers and three account scalars. **No MCP tool output
enters the prompt.**

The precise claim is worth stating carefully, because "zero prompt-injection
surface" — which this document used to say — was too strong.
`screener.screen()` records a failed underlying with its exception message,
and an Alpaca error string is partly chosen by the server. Forwarding that
context dict wholesale put externally-influenced text into the prompt.

`build_prompt()` now **whitelists**: it copies across only the numeric fields
this codebase computed itself, plus the exception *class name*, which comes
from a fixed vocabulary. The feed name is normalised to `opra` / `indicative`
/ `unknown`. So the defensible claim is not that there is no attack surface,
but that every string reaching the model comes from a vocabulary this
repository controls — and `tests/test_brain.py` attempts to smuggle an
instruction through the error channel to prove it.

Per candidate:

```json
{
  "candidate_id": 0,
  "underlying": "AAPL",  "kind": "put_credit",
  "expiry": "2026-09-11", "dte": 12,
  "short_symbol": "AAPL260911P00310000",
  "long_symbol":  "AAPL260911P00305000",
  "short_strike": 310.0, "long_strike": 305.0, "width": 5.0,
  "credit": 0.90, "max_profit": 90.0, "max_loss": 410.0,
  "pop": 0.8163,  "pop_rn": 0.7603,
  "ev": 22.97,    "ev_rn": -4.73,   "vrp_edge": 27.70,
  "short_delta": -0.2265, "short_iv": 0.2383, "realized_vol": 0.1891,
  "distance_pct": 0.0310, "open_interest": 1019
}
```

The prompt explains that `vrp_edge` is the most important number, that both
EVs come from one model with only the volatility differing, that every
candidate has already cleared the $2.00 floor, and that `quote_feed` of
`indicative` means the credit is approximate.

### Three constraints on the model

**1. It selects, it does not construct.** The response carries a
`candidate_id` indexing into the shortlist.

**2. Echoed legs are verified byte-for-byte.**

```python
got      = {(l["symbol"], l["side"].lower()) for l in legs}
expected = {(chosen["short_symbol"], "sell"),
            (chosen["long_symbol"],  "buy")}
if got != expected:
    return no_trade("legs did not match candidate %d - refusing to execute")
```

A hallucinated symbol, an invented strike, or a flipped side fails this
comparison and the cycle records a no-trade, with both leg sets logged.

**3. It cannot size.** The schema requires a `contracts` field, but that value
is **advisory only and discarded**. `risk.py` sizes every position. When the
two disagree, both are logged — an honest audit trail.

### The fallback

If no Anthropic key is configured, or the API errors, or the response cannot
be parsed, `deterministic_decide()` runs instead: the first candidate clearing
`POP >= 0.60` and `EV >= $2.00`. The agent remains fully autonomous and fully
safe — it simply stops exercising judgement and follows arithmetic. **Every
downstream risk gate is unchanged**, so the safety properties do not depend on
the LLM being available.

---

## 9. Stage 5 — Risk gates and sizing

`agent/risk.py`. Every gate is a pure function of `(candidate, account_state)`,
which is why they are unit-testable and why the model cannot argue with them.

**Any veto rejects the trade outright.** There is no override, no confidence
threshold that buys an exception, and no path by which a persuasive rationale
can widen a limit.

### The eight gates

| # | Gate | Rule |
|---|---|---|
| 1 | **Session halt** | Day P&L ≤ −3% of equity → nothing trades today |
| 2 | **Structure** | Both legs present, distinct, long leg further OTM on the correct side, `max_loss` reconciled against `width×100 − credit×100` |
| 3 | **DTE bounds** | 2 ≤ DTE ≤ 14 |
| 4 | **Liquidity** | OI ≥ 250, bid-ask ≤ 25% of mid, credit ≥ $0.10 |
| 5 | **Volatility** | 0.03 ≤ IV ≤ 1.50 |
| 6 | **Concentration** | ≤ 5 concurrent positions, ≤ 2 per underlying |
| 7 | **Sizing** | See below |
| 8 | **Buying power** | Re-asserted *after* sizing |

Gate 2 is the most important one in the codebase. It proves the position is
defined-risk:

```python
if c.kind == "put_credit" and not c.long_strike < c.short_strike:
    veto("put spread long strike not below short - loss is NOT capped")

expected = c.width * 100.0 - c.credit * 100.0
if abs(c.max_loss - expected) > 1.0:
    veto("max_loss disagrees with width*100 - credit*100")
```

Gate 4 deliberately re-checks liquidity rather than trusting the screener,
because quotes move between screening and execution. Its threshold (25%) is
looser than the screener's (10%) for exactly that reason.

### Sizing — worked example on a fresh $100,000 account

`size_position()` takes the **smallest** of four independent constraints:

```
Account: equity $100,000  ·  options buying power $200,000  ·  no open positions
Candidate: max_loss $410 per spread

per-position budget   $100,000 x 5%  = $5,000    / $410 =  12
portfolio headroom    $100,000 x 25% = $25,000   / $410 =  60
usable buying power   $200,000 x 50% = $100,000  / $410 = 243
hard contract cap                                        =  25
                                                          ────
final size = min(12, 60, 243, 25)                        =  12
```

**Result: 12 contracts. Total risk $4,920 = 4.92% of equity.**

The per-position budget binds here, which is the intended behaviour — a single
trade should never be able to hurt the account materially.

### Sizing when the portfolio is already loaded

```
Account: equity $100,000  ·  options BP $180,000  ·  day P&L -$1,200
         3 open positions  ·  open risk $21,000
Candidate: SPY 760/759, max_loss $77 per spread

per-position budget   $5,000                    / $77 =   64
portfolio headroom    $25,000 - $21,000 = $4,000 / $77 =   51
usable buying power   $90,000                    / $77 = 1168
hard contract cap                                      =   25
                                                        ────
final size = min(64, 51, 1168, 25)                     =   25
```

Now portfolio headroom is nearly binding at 51, and the hard 25-contract
blast-radius cap takes over. That cap is independent of every percentage
calculation precisely so that a bug in one of them cannot produce an enormous
order.

---

## 10. Stage 6 — Execution

`agent/executor.py`. Orders route through **Alpaca's official MCP server**,
spawned as a stdio subprocess. One session per agent cycle, because spawning
costs a couple of seconds.

The installed server was verified directly: **v3.4.7, 72 tools**, with
`place_option_order` present and its schema accepting exactly the parameters
sent below.

### The entry order

```json
{
  "qty": "12",
  "type": "limit",
  "time_in_force": "day",
  "order_class": "mleg",
  "limit_price": 0.86,
  "legs": [
    {"symbol": "AAPL260911P00310000", "ratio_qty": "1",
     "side": "sell", "position_intent": "sell_to_open"},
    {"symbol": "AAPL260911P00305000", "ratio_qty": "1",
     "side": "buy",  "position_intent": "buy_to_open"}
  ],
  "client_order_id": "alpha-1756543210123"
}
```

**The limit price:**

```
entry_limit_price(0.90) = max(round(0.90 x 0.95, 2), 0.05) = $0.86
```

We ask for slightly less than the modelled mid so we actually get filled.
Quotes come from the indicative feed, which is an estimate rather than true
NBBO, so conceding a nickel beats chasing a price that may not exist.

### Why `mleg` matters

Both legs fill together or neither does. **Legging in — selling the short leg
and then trying to buy protection — is how you end up accidentally naked.** It
is never done, on entry or on exit.

The same applies closing: buying back the short leg alone would leave a naked
long, and selling the long leg alone would leave you **naked short**. Both
move together.

### The security envelope

The MCP server tags responses:

```json
{"_alpaca_mcp_security": {"trust": "untrusted_tool_output", ...},
 "data": {...}}
```

`_unwrap()` drops the envelope and returns only `data`. The `instructions`
field is never interpreted — it is metadata for a human, not for the agent —
and **no tool output is ever fed back to the model**.

---

## 11. Managing the position: profit, loss, time

`agent/loop.py`. This runs **first** in every cycle, before any new entry,
because closing trades is what converts paper gains into realised P&L. An
agent that opens positions while ignoring existing ones ends up holding losers
into expiry.

### The position

```
12 x AAPL 310/305 put credit @ $0.90
max profit  $90.00/spread   ->  $1,080.00 total
max loss   $410.00/spread   ->  $4,920.00 total
breakeven at expiry = 310.00 - 0.90 = $309.10
risk : reward = 4.56 : 1
```

### The four exit triggers

```
credit_total = 0.90 x 100 x 12 = $1,080.00
```

**1. Take profit — 50% of max profit**

```
close when unrealised P&L >= +0.50 x 1,080.00 = +$540.00
```

The spread would be worth roughly $0.45/share to buy back, half the credit
received. Taking half the maximum early is standard credit-spread practice:
the last 50% of the profit takes disproportionately longer to earn and carries
the same tail risk throughout.

**2. Stop loss — 2× the credit received**

```
close when unrealised P&L <= -2.0 x 1,080.00 = -$2,160.00
```

The spread would be worth roughly $2.70/share. Critically, **that is only
43.9% of the $4,920 max loss** — the stop fires well before the position is
lost, saving $2,760 versus riding it to expiry.

**3. Delta stop — the short leg's delta doubles**

```
entry |delta| 0.2265  ->  close when current |delta| >= 0.4530
```

A doubling delta means the underlying is moving at us and the probability of
loss has roughly doubled since entry. Cutting here converts some max-losses
into partial losses. This fires *before* the price stop in a slow grind, which
is exactly when the price stop is least useful.

**4. Time exit — 1 DTE**

```
close when (expiry - today).days <= 1
```

Never carry into expiry day. Gamma on the final day is brutal and pin risk
around the short strike is real.

**Plus the session-level halt:** day P&L ≤ −3% of equity (−$3,000 on $100k)
stops all trading for the session, checked as gate 1 before anything else.

### P&L at expiry — every region

| AAPL at expiry | Region | Per spread | 12 spreads |
|---:|---|---:|---:|
| $330.00 | above short — full credit | +$90.00 | **+$1,080.00** |
| $320.00 | above short — full credit | +$90.00 | +$1,080.00 |
| $311.00 | above short — full credit | +$90.00 | +$1,080.00 |
| $310.00 | at short strike — full credit | +$90.00 | +$1,080.00 |
| **$309.10** | **breakeven** | **$0.00** | **$0.00** |
| $307.50 | between strikes | −$160.00 | −$1,920.00 |
| $305.00 | at long strike — max loss | −$410.00 | −$4,920.00 |
| $300.00 | below long — max loss | −$410.00 | −$4,920.00 |
| $290.00 | below long — max loss | −$410.00 | −$4,920.00 |

Note the flat regions on both ends. That flat left tail is the entire point of
buying the 305 put: AAPL could go to zero and the loss is still $4,920.

### How a close is submitted

```json
{
  "qty": "12", "type": "market", "time_in_force": "day",
  "order_class": "mleg",
  "legs": [
    {"symbol": "AAPL260911P00310000", "ratio_qty": "1",
     "side": "buy",  "position_intent": "buy_to_close"},
    {"symbol": "AAPL260911P00305000", "ratio_qty": "1",
     "side": "sell", "position_intent": "sell_to_close"}
  ]
}
```

Closes default to a **market** order. When an exit is triggered — profit
target, stop, delta stop, or approaching expiry — certainty of exit beats
price improvement.

---

## 12. The break-even arithmetic

This section exists because it is the most fragile part of the design, and
because the previous version of it made a comparison that does not hold.

The exit rules define a fixed reward-to-risk ratio, independent of which
spread is chosen:

```
win  = TAKE_PROFIT_FRACTION x credit = 0.50 x credit
loss = STOP_LOSS_MULTIPLE   x credit = 2.00 x credit

reward : risk = 1 : 4
break-even win rate = 4 / (1 + 4) = 80.0%
```

| Take profit | Stop loss | Reward : risk | Break-even win rate |
|---|---|---|---|
| **50% of credit** | **−2.0× credit** | **1 : 4.00** | **80.0%** |
| 50% of credit | −1.5× credit | 1 : 3.00 | 75.0% |
| 50% of credit | −1.0× credit | 1 : 2.00 | 66.7% |
| 65% of credit | −2.0× credit | 1 : 3.08 | 75.5% |
| 75% of credit | −2.0× credit | 1 : 2.67 | 72.7% |

### The comparison this document used to make, and why it was wrong

An earlier version placed the modelled **81.6% probability of profit** next to
the **80.0% break-even win rate** and called the margin thin. That comparison
is not valid, because the two numbers describe different events:

| | What it actually is |
|---|---|
| `pop` = 81.6% | P(short strike not breached **at expiry**), under the model at realised vol. A property of the **terminal** distribution, assuming the position is **held to expiry**. |
| 80.0% break-even | The win rate a **binary** bet would need, if every trade ended at either +0.50×credit or −2.0×credit. |

Neither assumption holds. The position is rarely held to expiry — that is the
entire purpose of the exit rules. And the outcome is **not binary**: a cycle
can end at the take-profit, at the stop, at the delta stop, or at the 1-DTE
close, and the last two land at essentially arbitrary intermediate P&L.

Computing P(take-profit before stop) correctly is a **first-passage problem**
on the spread's mark-to-market value, not a question about the terminal price
distribution. This system does not compute it, and `pop` is not an estimate of
it. Nothing in the codebase claims otherwise; only this document did.

### What can honestly be said

- The exit rules commit to a **1:4 reward-to-risk** shape. That demands a high
  hit rate, and it is a deliberate choice, not an accident.
- Taking profit at 50% **truncates the holding period**, which mechanically
  raises the realised win rate above the hold-to-expiry `pop` — you close
  before the distribution has time to widen. By how much is not modelled here.
- The stop, the delta stop, and the 1-DTE close all cut trades that would
  otherwise have had time to become max losses.
- **Whether the net of those effects clears 80% is an empirical question this
  project has not answered**, and 10–20 contest trades cannot answer it (§15).

The take-profit and stop-loss pair is therefore the **most sensitive
unvalidated parameter choice in the system.** Moving the stop to −1.5× credit
would drop the break-even requirement to 75%. That has not been changed,
because changing a risk parameter to make the arithmetic look friendlier —
without evidence — would be exactly the wrong response to noticing this.

---

## 13. The journal and dashboard

`agent/journal.py` — SQLite, four tables, append-only in spirit. Rows are
inserted; only order and position status is ever updated. **No decision is
ever deleted, including the bad ones.**

| Table | Holds |
|---|---|
| `runs` | One row per cycle: timestamp, feed, equity, day P&L, candidate count, guardrail notes |
| `decisions` | Full candidate JSON, the model's action / confidence / rationale / raw output / any error, the risk verdict with every reason and veto |
| `orders` | Alpaca order id, legs, contracts, limit, credit, max loss, status, entry delta, entry DTE, fill data, realised P&L |
| `equity_snapshots` | Equity curve for the dashboard |

One subtlety worth highlighting, since it was a real bug:

```sql
SELECT * FROM orders WHERE closed_ts IS NULL
  AND status NOT IN ('canceled','cancelled','rejected','expired',
                     'dry_run','failed')
```

`risk.py` reads this to enforce the concentration limit and the portfolio risk
cap. Counting **dry-run or failed orders** here would invent risk that does not
exist and could veto real trades.

The dashboard (`dashboard/api.py` + `static/index.html`) is **read-only by
design** — there is no route that places, cancels, or modifies an order. It
renders the equity curve, open positions, and every decision including the
vetoed ones, with an **Edge** column showing `vrp_edge`, the two EVs it came
from, and the implied-versus-realised volatility beneath it.

---

## 14. What happens when things break

| Failure | Behaviour |
|---|---|
| No Anthropic key | Deterministic selection. Agent runs fully. |
| Claude API error / outage | Caught, logged to `llm_error`, deterministic fallback. |
| Model returns unparseable output | `no_trade`, raw output journaled for inspection. |
| Model hallucinates a leg | Leg-set comparison fails → `no_trade`, both sets logged. |
| Model returns out-of-range `candidate_id` | `no_trade`. |
| No implied volatility for a leg | Candidate discarded — edge unmeasurable. |
| Alpaca data fetch fails for one symbol | That symbol is skipped, recorded in context, others continue. |
| MCP server missing | `MCPError` with the install command. |
| Order submission fails | Recorded with status `failed`, **excluded from open risk**. |
| Market closed | Cycle exits immediately unless `--force`. |
| Scheduled cycle throws | Caught by the job wrapper; the scheduler survives. |

---

## 15. Citation audit

Every academic claim this project makes, checked against what the source
actually establishes.

| Claim as stated | Source | What the source actually establishes | Verdict |
|---|---|---|---|
| Delta-hedged S&P 500 option portfolios underperform zero; underperformance is greater at higher volatility | Bakshi & Kapadia (2003), *RFS* 16(2), 527–566 | Exactly this, plus: underperformance is **less** for away-from-the-money options. Evidence for a negative market volatility risk premium. | **Supported.** Volume, issue, pages verified. |
| Variance risk premiums quantified across 5 indices and 35 stocks | Carr & Wu (2009), *RFS* 22(3), 1311–1341 | Exactly this. Defines the premium via **variance swap rates** over a matched horizon. | **Supported**, but note the definition differs from our `vrp_edge` (§5.6). |
| CBOE PUT index: 9.95% vol vs 14.93% for the S&P 500 at near-identical return, Jun 1986–Dec 2018 | CBOE / Bondarenko (2019) | Figures verified. The index sells **one-month at-the-money cash-secured puts**, rolled monthly. | **Supported as stated** — but a different instrument and horizon from what we trade. Was previously cited without that caveat. |
| "Premium is richer nearer the money", used to justify a 0.10–0.35 delta band | Bakshi & Kapadia (2003) | Supports the *direction* (less underperformance away from the money). Says nothing about delta bands or these specific values. | **Was too strong.** Now labelled an engineering choice (§5.2). |
| `vrp_edge` is "the volatility risk premium" | — | No source defines the premium as a spread-level dollar EV difference under a single-vol lognormal. | **Was too strong.** Now described as an operational signal with an economic rationale (§5.6). |
| "Delta is the risk-neutral probability of finishing in the money" | — | False. Delta is N(d₁); risk-neutral P(ITM) is N(d₂). | **Was wrong.** Corrected throughout; §6 explains the sign flip between calls and puts. |
| Alpaca `mleg` order structure, 2–4 legs, `position_intent` per leg | Alpaca options docs | Matches the implementation exactly. Paper accounts get Level 3 by default. | **Supported.** |
| MCP tool `place_option_order` and its parameters | Installed server, queried directly | v3.4.7, 72 tools; schema accepts exactly the parameters sent. | **Supported**, verified against the running server rather than the docs. |

**Nothing in the literature validates this strategy, this horizon, or these
thresholds.** The papers establish that a volatility risk premium exists and
persists in equity index options. Everything between that prior and this
implementation is our own engineering, and is presented as such.

---

## 16. Known limitations

Stated here rather than left for a reviewer to find.

**Quotes are indicative, not NBBO.** Without an OPRA agreement the credit —
and therefore both EVs, the edge, and the score — is computed from a derived
quote. This is the largest error source in the system and it sits upstream of
every calculation in this document.

**Four correlated underlyings are not diversification.** `balanced_top()`
spreads the shortlist across SPY, QQQ, IWM and AAPL, but all four are US
equity beta. It diversifies across *tickers*, not across *risk factors*.

**Selection bias on the volatility estimate.** Under a normal-iid assumption a
20-day realised-vol estimate carries roughly 16% relative standard error —
`1/sqrt(2(n−1))` with n = 20 returns, an asymptotic result that real
fat-tailed, vol-clustered returns make optimistic. `ev_rw` falls as that
estimate rises. Filtering on measured edge therefore preferentially selects
names whose realised volatility is *currently underestimated*. Gating on the
premium rather than on real-world EV substantially reduces this, because a
low realised-vol estimate raises `ev_rw` and `ev_rn` is unaffected — but it
does not eliminate it.

**The lognormal model underestimates tails.** Real equity returns are
fat-tailed. `P(max loss)` is therefore biased low, which biases every EV high.
Both valuations share the bias so it partly cancels in `vrp_edge`, but not
exactly — the two are evaluated at different volatilities.

**Vertical skew is not modelled.** Both legs are priced at the short leg's
implied volatility. Real skew means the further-OTM long leg usually trades at
a higher IV, so `ev_rn` absorbs that mispricing rather than isolating
volatility (§5.6). This is the largest *modelling* simplification in the
system, as distinct from the largest *data* limitation, which is the feed.

**`vrp_edge` is a screening signal, not a measured risk premium.** It is a
dollar figure from one quote snapshot under a single-vol zero-drift lognormal.
It is not the variance risk premium of the literature (§5.6), and it has not
been validated out-of-sample.

**The 20-day realised volatility is an estimator, not a forecast.** It is
computed from 20 daily closes and used as though it described volatility over
a 2–14 day horizon. The horizons are deliberately not matched (§9).

**The take-profit and stop-loss pair is unvalidated.** It commits to a 1:4
reward-to-risk shape, and whether the realised win rate clears the implied
break-even is an open empirical question (§12).

**Fixed dollar widths across different price levels.** `WIDTHS = [1, 2, 5]` is
in dollars while SPY trades near $770 and IWM near $296. A $1-wide spread is
0.13% of SPY but 0.34% of IWM — the same width list is a structurally
different trade on each name.

**No option volume.** Alpaca's snapshot exposes open interest but not daily
volume, and fetching daily bars for ~2,400 contracts per cycle is not
practical. OI alone is the liquidity proxy.

**Five days is statistical noise.** Perhaps 10–20 closed trades. Distinguishing
a 60% win rate from 70% at any real confidence needs hundreds. This is exactly
why `adapt.py` refuses to react to fewer than five closed trades and can only
ever tighten.

---

## Appendix — reproducing every number here

```bash
python -m pytest -q                  # 184 tests
python -m scripts.run_screener       # live screen, read-only
python -m agent.loop --force         # one dry-run cycle, market closed
uvicorn dashboard.api:app --port 8000
```

**Sources**

- Bakshi & Kapadia (2003), *Delta-Hedged Gains and the Negative Market
  Volatility Risk Premium*, RFS 16(2), 527–566 —
  https://academic.oup.com/rfs/article-abstract/16/2/527/1579962
- Carr & Wu (2009), *Variance Risk Premiums*, RFS 22(3), 1311–1341 —
  https://academic.oup.com/rfs/article-abstract/22/3/1311/1581057
- CBOE, *Historical Performance of Put-Writing Strategies* (Bondarenko, 2019) —
  https://cdn.cboe.com/resources/education/research_publications/PutWriteCBOE19_v14_by_Prof_Oleg_Bondarenko_as_of_June_14.pdf
- Alpaca, *Options Level 3 Trading* —
  https://docs.alpaca.markets/us/docs/options-level-3-trading
- Alpaca, *MCP Server* — https://docs.alpaca.markets/us/docs/alpaca-mcp-server
