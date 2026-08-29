# Alpha Options Agent — one-page write-up

**An autonomous options-trading agent that sells defined-risk credit spreads on
SPY and QQQ, running unattended on Alpaca paper trading.**

Built for the lablab.ai × Alpaca AI Trading Agents hackathon (28 Aug – 4 Sept 2026).
Paper account: `PA3S1NN3SHKV`. Options level 3. Starting equity $100,000.

---

## 1. The AI logic

The core design decision is a strict separation between **arithmetic**,
**judgement**, and **permission** — three layers that cannot substitute for one
another.

```
        deterministic                LLM                  deterministic
   ┌─────────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
   │  screener.py        │──▶│  brain.py        │──▶│  risk.py             │
   │  what is VALID      │   │  what is GOOD    │   │  what is ALLOWED     │
   │  and tradable       │   │  right now       │   │  + how BIG           │
   └─────────────────────┘   └──────────────────┘   └──────────────────────┘
        no LLM                 Claude Sonnet 4.6         no LLM, can VETO
```

**`screener.py` — structure (no LLM).** Merges Alpaca's Trading API (strikes,
expiries, open interest) with the Market Data API (quotes, Greeks, IV) and
builds every structurally valid vertical credit spread on SPY/QQQ at 1–7 DTE.
Legs are filtered on bid, open interest ≥ 250, and bid-ask ≤ 20% of mid; short
legs must sit in a 0.10–0.35 delta band.

Candidates are ranked by **expected value**, not reward/risk. Ranking by
`max_profit / max_loss` is a trap — it always prefers the narrowest spread
closest to the money, which is also the most likely to lose. Using delta as the
standard proxy for the probability of finishing in-the-money:

```
p(keep full credit) = 1 − |Δ_short|
p(partial loss)     = |Δ_short| − |Δ_long|   → ~half of max loss on average
p(full max loss)    = |Δ_long|
```

Negative-EV candidates are discarded outright (this removed ~53% of the raw
pool in live testing). Selection then round-robins across
`(underlying, direction)` buckets, because a top-N list is otherwise happy to
return eight variations of the same directional bet.

**`brain.py` — judgement (Claude).** The model receives the shortlist plus
account scalars and returns strict JSON. The critical constraint:

> **The model SELECTS. It does not CONSTRUCT.**

The response carries a `candidate_id` indexing the shortlist, and the echoed
legs are verified against that entry exactly. A hallucinated OCC symbol, an
invented strike, or a flipped side fails validation and becomes a no-trade.
The model cannot express a trade that the deterministic layer did not already
approve as structurally sound. `no_trade` is an explicitly encouraged answer.

The model also returns a `contracts` field — **it is discarded**. Sizing is not
a judgement call. When the model's suggestion differs from the risk module's
sizing, both are logged.

**Graceful degradation.** If the LLM is unavailable — no key, API error, or
`--no-llm` — the agent does not stop. It falls back to deterministic selection
(highest EV per dollar risked clearing a fixed POP/EV bar) and keeps trading
autonomously. Every risk gate is unchanged in both modes: the safety properties
are a property of the architecture, not of whether the model answered.

**`risk.py` — permission (no LLM, can veto).** Pure functions of
`(candidate, account_state)`. There is no override path, no confidence
threshold that buys an exception, and no way for a persuasive rationale to
widen a limit.

## 2. Risk gates

| Gate | Rule |
|---|---|
| Session halt | Day P&L ≤ −3% of equity → no further trades this session |
| **Structure** | Both legs present; long leg strictly further OTM; `max_loss` reconciled against `width × 100 − credit × 100`. **Never naked, never undefined risk.** |
| DTE bounds | 1–10 days. 0DTE is refused outright — the gamma cliff near expiry is not worth the theta |
| Liquidity | OI ≥ 250, bid-ask ≤ 25% of mid, credit ≥ $0.05 |
| Volatility | IV outside 3%–150% rejected as bad data or event risk |
| Concentration | ≤ 5 concurrent positions, ≤ 2 per underlying |
| **Sizing** | ≤ 2% of equity per position, ≤ 10% portfolio-wide, ≤ 10 contracts, ≤ 50% of options buying power |

Every gate is unit-tested, including the ones that must never fail open:
missing long leg, long strike on the wrong side, tampered `max_loss`,
flipped legs, and a model attempting to influence position size. **36 tests, all passing.**

Positions are actively managed *before* new entries each cycle — closed at 50%
of max profit, at 2× credit loss, or at 1 DTE. In a five-day contest, realising
P&L matters more than holding for the last few dollars of theta.

## 3. Alpaca infrastructure

- **MCP server (`alpaca-mcp-server` 2.3.0)** — every order goes through
  `place_option_order` on the official MCP server, spawned as a stdio
  subprocess and driven by the `mcp` client SDK. 72 tools available; the agent
  uses account, positions, orders, and option-order placement.
- **Atomic multi-leg execution.** Spreads enter and exit as a single `mleg`
  order. Legging in would leave a naked short between fills — never done.
- **Trading API via `alpaca-py`** for bulk chain and Greeks screening — a
  read-only path where fetching ~1,200 contracts per underlying in bulk is far
  more efficient than per-contract tool calls.
- **Prompt-injection surface: zero.** The MCP server tags responses
  `"trust": "untrusted_tool_output"`. We honour that — tool output is parsed
  for specific structured fields and never enters the model's context. The
  model sees only screener-derived numbers.
- **Paper-only enforcement.** `ALPACA_PAPER_TRADE=true` is asserted at every
  entry point; the process refuses to start otherwise.

## 4. Known limitations

Stated plainly, because they affect how the numbers should be read:

- **No OPRA entitlement** on this account (`"OPRA agreement is not signed"`), so
  quotes come from Alpaca's `indicative` feed — a derived estimate, not true
  NBBO. Entry limits concede 5% off modelled mid rather than trusting it.
- **IV rank is not computable.** Alpaca exposes no IV history, so
  `iv_vs_rv` (ATM implied vs 20-day realised volatility) is substituted. For a
  premium seller this is arguably the more relevant signal, but it is a
  substitution, not the requested metric.
- **Delta as probability** is a risk-neutral approximation, not a real-world
  one. It systematically overstates the probability of profit slightly.
- Thresholds were tuned against a **closed** market, where indicative quotes go
  stale and index skew inverted (calls priced richer than puts, which does not
  happen in a live equity index). They require re-validation at the open.

## 5. Auditability

Every run, every LLM rationale, every risk veto, and every order is written to
SQLite (`journal/trades.db`) and rendered by a read-only FastAPI dashboard —
including trades the risk layer refused. The dashboard has no route that can
place, modify, or cancel an order.

The agent is designed so that its worst possible day is bounded by arithmetic
rather than by the model's judgement.
