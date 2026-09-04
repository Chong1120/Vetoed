# Vetoed — an autonomous options agent where the AI is the least-trusted component

**Alpaca AI Trading Agents Hackathon · paper trading only**
Live dashboard: chong1120.github.io/Vetoed · Repo: github.com/Chong1120/Vetoed

---

## The problem

If a language model can pick the trade, choose the size and send the order, one
hallucinated contract is a real loss — and you cannot unit-test a language model.
You *can* unit-test the code that decides whether to obey it. So in Vetoed the
model's entire authority is this: **pick one item from a list it did not write.**

## What it trades

Defined-risk credit spreads on SPY, QQQ, IWM and AAPL. Two legs always, sent as
one atomic multi-leg order, so the maximum loss is a known number before the
order exists and a naked short is structurally impossible.

## How the agent works — the workflow

1. **Screen.** A deterministic screener builds every valid spread from the live
   chain and prices each one **twice through the same model**, changing only the
   volatility input: 20-day realised against the market's implied. The gap is the
   premium being harvested, in dollars. Anything under **$2.00** is discarded.
   *(This is our own model-derived measure, deliberately not the academic
   variance risk premium, which is defined and measured differently.)*
2. **Select.** The LLM picks one candidate from that shortlist. Its answer is
   validated against the list, so an invented contract is unexecutable, and the
   position size it suggests is discarded.
3. **Gate.** A deterministic risk module sizes the trade and can overrule the
   model at any confidence. **Eight hard gates**: 5% of equity per position, 25%
   across all, a 3% daily-loss halt, a 25-contract cap, plus liquidity, DTE,
   volatility and structural checks. Any one rejects the trade outright.
4. **Execute.** Orders go through **Alpaca's own MCP server** — the single write
   path in the system — as one atomic multi-leg order carrying a deterministic
   SHA-1 order ID, so a restart cannot open the same spread twice.
5. **Reconcile.** Every cycle re-reads positions and open orders **from Alpaca**,
   never from local state. The broker is the source of truth.

## How it uses Alpaca

Alpaca MCP server for all order placement; Alpaca options chain, Greeks and
quotes for screening; Alpaca positions and orders for reconciliation; Alpaca's
market clock to decide whether to trade at all. The process refuses to start
unless `ALPACA_PAPER_TRADE=true`.

## What the dashboard shows that most do not

Every rejection, by the reason that caused it. A typical cycle measures the
chains and declines around **1,500 spreads** — open interest below the liquidity
floor, premium under ten cents, delta outside the band, edge under $2.00 — each
reason counted and naming real contracts. Each position also carries a note the
model writes from that position's journal entry, using figures quoted from the
record; it is never allowed to calculate.

## Operating record

Unattended on GitHub Actions, started by a Vercel cron: **148 cycles, 749
candidates screened, 12 orders sent — 1.6% of what it looked at.** Currently 5
open spreads and 7 closed, **−$406 realised** on a $100,000 paper account.
**242 tests**, most of them on the gates.

## What I am not claiming

Quotes are indicative, not exchange best bid and offer. Skew is not modelled.
Twenty-day realised volatility is an estimator, not a forecast. Four correlated
tickers is not real diversification. The exit thresholds are unvalidated. A
contest week proves nothing about profitability — the research motivates the
idea; it does not validate this system.
