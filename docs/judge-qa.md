# Judge questions

Answers verified against the audited implementation, not the pitch. Where the
honest answer is "we don't know", it says so — a judge will find those anyway,
and finding them yourself is worth more than hiding them.

Every figure here is reproducible: `python -m pytest -q`,
`python -m scripts.run_screener`, `python scripts/show_journal.py`.

---

### 1. Why options at all?

Because options let you get paid for taking a **defined, quantifiable risk**
without predicting direction. A stock position needs the price to move your
way. A short option spread needs the price to *not* move past a strike — a
much weaker requirement, and one where the compensation is visible up front as
the credit received.

It also gives a measurable signal to trade on: the gap between what options
charge for volatility and what the underlying actually does.

### 2. Why credit spreads, rather than selling naked options?

**The loss is capped before the order is sent.** A naked short put on a $320
stock risks $32,000 per contract. The 310/305 spread risks $406, and that
number is known at submission.

It also makes the risk arithmetic exact: `max_loss = width × 100 − credit × 100`,
reconciled by a risk gate. You cannot build a position whose worst case you
can't state. The trade-off is real — capping the loss also caps the credit —
and we take it deliberately.

### 3. Why implied versus realised volatility?

Implied volatility is what option prices are charging for future movement.
Realised is what the underlying actually did. When implied sits above realised,
the seller is being paid more than recent history would justify.

That gap is the economic rationale for premium selling, and it is the only
thing this agent tries to capture. **It's a hypothesis with literature behind
it, not a guarantee** — implied vol is above realised most of the time
precisely because sometimes it isn't, and those are the expensive days.

### 4. What exactly is your "edge" figure? Is it the volatility risk premium?

**No, and we're careful not to claim that.**

`vrp_edge` is our own **model-derived, spread-level signal**. We take one
spread and compute its expected payoff twice under the same zero-drift
lognormal model with the same credit — once with 20-day realised volatility,
once with the short leg's implied volatility. The difference is the number.

The canonical academic VRP (Carr & Wu 2009) is defined on **variance swap rates
over a matched horizon** — a different quantity, measured differently. The
literature motivates *why* such a gap should persist. It does not certify this
estimator.

What the construction does guarantee: because both sides run the same function
on the same credit, the signal is **exactly zero when the two volatilities
agree**. That's pinned by a test.

### 5. Why isn't `ev_rn` always zero? Shouldn't no-arbitrage force it to?

Under exact risk-neutral pricing, yes. In our implementation it isn't, for
three stated reasons:

1. **Skew isn't modelled.** Both legs are priced at the *short* leg's implied
   volatility. Real vertical skew means the further-OTM long leg usually trades
   richer, so a single-vol model misprices it.
2. **The credit comes from mid quotes**, which aren't fair value — and on this
   account they're `indicative`, not true NBBO.
3. **No discounting.** Negligible at 2–14 DTE, but an omission.

So `ev_rn` reads as *"what this spread is worth under our model at the market's
own volatility"*. Its distance from zero is a rough gauge of how much skew and
quote noise the simplification absorbs. It is **not** a mispricing claim.

### 6. Why 20-day realised volatility?

It's a **screening input, not a forecast**. Twenty daily closes is long enough
to be more than noise, short enough to reflect the current regime.

Two honest caveats. The horizon isn't matched — we trade 2–14 DTE options
against a 20-day backward estimate, deliberately. And under a normal-iid
assumption the estimator carries roughly **16% relative standard error**; real
returns are fat-tailed and vol-clustered, so the true figure is worse.

That matters because a *low* realised-vol reading inflates the signal. Gating
on the implied-minus-realised gap rather than on expected value alone reduces
the resulting selection bias. It does not eliminate it.

### 7. Why 2–14 days to expiry?

**A contest-design parameter, and we label it as one.** It's chosen so
positions can open and close inside a one-week window.

The bounds have reasons: below 2 DTE, gamma makes the position violently
sensitive to small moves; beyond 14, capital is tied up longer than the
contest lasts and there's less theta decay per day held. No paper prescribes
these numbers.

### 8. Why filter on delta if delta isn't a probability?

Delta is a fine **moneyness coordinate** — it's a monotone, liquid,
broker-supplied measure of how far out of the money a strike is. Using it to
select a strike band is reasonable.

What's *not* reasonable is using it **as a probability**, which an earlier
version did. Under Black-Scholes, delta is N(d₁) while the risk-neutral
probability of finishing in the money is N(d₂) — they differ by roughly σ√T,
and the sign of the error **flips between calls and puts**, so substituting one
for the other biases the two sides of the book in opposite directions.

The 0.10–0.35 band is an engineering choice. Bakshi & Kapadia find delta-hedged
underperformance is *less* for away-from-the-money options, which motivates
avoiding the far tail — but the paper says nothing about 0.10, or 0.35, or
delta bands at all.

### 9. What if the model hallucinates a contract?

It cannot execute. The model returns an index into the shortlist plus the legs
it believes it chose; those legs are compared **as a set, character for
character**, against the real candidate. Any mismatch — an invented symbol, a
wrong strike, a flipped side — returns `no_trade` and journals both sets.

Tested for invented symbols, flipped sides, missing legs, out-of-range indices,
and garbage types — and specifically tested with the current provider, so the
guarantee isn't vendor-dependent.

### 10. What if it asks for too many contracts?

The field is **read and discarded**. `risk.size_position()` computes the size
as the minimum of four independent constraints: 5% of equity per position,
remaining portfolio headroom against a 25% cap, 50% of options buying power,
and a hard 25-contract cap.

You can watch this happen in the logs:

```
note: model suggested 1 contracts, risk sized 12 (risk wins)
```

The hard cap is deliberately independent of every percentage calculation, so a
bug in one of them still can't produce an enormous order.

### 11. What if there are no candidates?

Nothing trades, and the cycle records **why**. That's the designed outcome, not
a failure — on the screen in the deck, QQQ produced zero candidates because
its implied volatility sat below realised.

The gates that produce a zero: no spread cleared $2.00 of measured gap, or no
implied volatility was available (a signal we can't compute is one we won't
claim), or the trend filter blocked the side, or liquidity floors rejected the
legs.

### 12. What if the language model goes down?

The agent keeps trading. `deterministic_decide()` picks the top-scoring
candidate clearing fixed bars, and **every risk gate downstream is identical**.

Every failure path degrades rather than propagates: HTTP error, timeout, empty
response, unparseable output. The cause is written to the journal.

This isn't hypothetical — it happened twice in development, once from a
Cloudflare block and once from an empty model name. Both times the agent
traded correctly on arithmetic while the cause sat in `llm_error`. That's
also why `scripts/show_journal.py --check` exists: **a green run is not
evidence the model answered.**

### 13. What happens after a few losing trades?

Deliberately, **almost nothing** — until there's enough evidence to act on.

`adapt.py` refuses to react to fewer than **5 closed trades**. Past that:
three consecutive losses on one underlying disables it; more than 70% losers
in the delta ≥ 0.25 bucket narrows the band.

Every adjustment is then **clamped against the screener's own defaults**, so
adaptation can only ever restrict. It cannot increase size, loosen a limit, or
re-enable something it disabled. A small sample can make the agent more
cautious; it can never make it more aggressive.

This is a **constrained feedback policy, not machine learning**, and we don't
call it learning in the deck. Distinguishing a 60% win rate from 70% needs
hundreds of trades; a contest produces maybe ten to twenty.

### 14. Why should anyone trust your edge measurement?

Trust the *construction*, not the number.

- **Both valuations run one function**, so the difference isolates volatility.
  Equal volatilities give exactly zero — pinned by a test.
- **The expected payoff is exact**, not approximated. The closed form is
  checked against a brute-force numerical integral of the payoff across 54
  parameter combinations.
- **We found our own errors and published them.** Delta-as-probability, and a
  midpoint approximation wrong by up to $37 on wide spreads — exceeding the
  $2.00 gate in about 17% of a sampled candidate space, so it changed
  decisions.

What that buys is a signal that is **internally consistent and reproducible**.
It does not buy proof that the signal predicts profit. See the next answer.

### 15. What does this system *not* prove?

Stated plainly:

- **Not that the strategy is profitable.** A contest week produces perhaps
  10–20 trades. That cannot distinguish a 60% win rate from 70%.
- **Not that the edge measure predicts returns.** It's internally consistent
  and economically motivated. It has not been validated out-of-sample.
- **Not that the exit rules are right.** Take-profit at 50% of credit against a
  stop at 2× credit is a 1:4 reward-to-risk shape, needing a high hit rate.
  Whether the realised rate clears it is an open question — and note that the
  modelled 81.6% probability of profit is a **hold-to-expiry terminal**
  probability, which is a *different event* from reaching the take-profit
  before the stop. We do not compute the latter, and do not claim the two
  comparable.
- **Not that the thresholds are optimal.** Every number is an engineering
  choice; none is derived from the literature.
- **Not that four correlated ETFs and one stock is diversification.**

What it does demonstrate: an autonomous agent where the AI's authority is
bounded by construction, every decision is auditable, the maths is exact and
tested, and the failure modes degrade safely.

---

## Questions I'd expect from a finance specialist

**"Your model has zero drift — isn't that wrong under the real-world measure?"**
Yes, and deliberately. Zero drift means the forward equals spot: no rates, no
dividends, no equity risk premium. We are declining to take a directional view.
On the put side, which is most of the book, that's the conservative choice — a
positive drift would flatter put spreads. It's also why we stopped calling the
realised-vol valuation a "real-world probability": the drift isn't the
real-world drift and the volatility is an estimate.

**"Lognormal underestimates tails — doesn't that inflate your EV?"**
It does. `P(max loss)` is biased low, so every EV is biased high. Both
valuations share the bias so it partly cancels in the difference, but not
exactly, because they're evaluated at different volatilities.

**"Indicative quotes aren't NBBO. How much does that cost you?"**
Unknown, and it's the largest error source in the system — upstream of every
calculation. We concede 5% off the modelled mid on entry rather than chase a
price that may not exist, but that's a mitigation, not a measurement.

**"You size to 5% per position but cap the portfolio at 25%. With 5 positions
you're fully allocated on correlated names."**
Correct, and the concentration limits are the only thing standing between that
and a single directional bet: 5 concurrent positions maximum, 2 per underlying.
On a four-name correlated universe that's a real limitation, and it's on the
limits slide.
