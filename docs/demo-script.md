# Demo video script

Target length **3 minutes 15 seconds**. A 90-second cut is at the bottom if the
submission caps the length.

Slides: <https://claude.ai/code/artifact/33fbdd10-9ace-493f-a1c1-35f3ed49de54>
(arrow keys or click to advance, **F** for fullscreen)

Live dashboard: <https://chong1120.github.io/Vetoed/>

**Before recording**

- Open the deck in fullscreen (F) in one window.
- Open the dashboard in a second window, already scrolled to the top.
- Close notifications. Record at 1080p.
- Read at a normal pace. The script is written to be spoken, not read.

---

## 0:00 — 0:12 · Slide 1 (title)

> Most AI trading agents give a language model the keys and hope it behaves.
> This one does the opposite. It's called Vetoed, and the AI is the
> least-trusted component in it.

---

## 0:12 — 0:30 · Slide 2 (the problem)

> Here's the thing nobody wants to say out loud. If a model can pick the
> trade, size the position, and send the order, then one hallucinated strike
> is an unbounded loss. And you can't unit-test a language model.
>
> But you *can* unit-test the code that decides whether to obey it. So in
> Vetoed, the model's entire authority is picking an index out of a list that
> was already validated. That's it.

---

## 0:30 — 0:48 · Slide 3 (the strategy)

> The strategy is selling defined-risk credit spreads. Short one option, long
> another further out — the long leg caps the loss, so the worst case is known
> before the order is ever sent.
>
> It makes money because option buyers systematically overpay for protection.
> That gap between what the market prices and what actually happens is the
> volatility risk premium, and it's one of the better documented effects in
> the options literature.

---

## 0:48 — 1:02 · Slide 4 (why most tools measure nothing)

> Now here's where most retail tooling goes wrong. They rank trades by
> delta-derived expected value. But delta is the risk-neutral probability —
> and under risk-neutral pricing, every fairly-priced option trade has an
> expected value of exactly zero.
>
> That's a no-arbitrage identity. Not an opinion. So ranking on it is ranking
> quote noise.

---

## 1:02 — 1:20 · Slide 5 (the measurement)

> So Vetoed measures the premium directly. It prices the same spread twice
> through the same model, and changes only the volatility going in — once with
> what the market implies, once with what the stock actually did over twenty
> days.
>
> The difference is the premium, in dollars. I call it vrp_edge. It has to
> clear two dollars or the trade is thrown away, and the shortlist ranks on
> premium per dollar actually at risk.

---

## 1:20 — 1:42 · Slide 6 (the bug I caught) — *the most important slide*

> And I want to show you something, because this is the part I'm actually
> proud of.
>
> My first version computed those two numbers with two *different* formulas.
> They disagree even when the volatilities are identical. So the agent
> reported a two-dollar-seventy-five premium on an IWM trade where implied and
> realised were basically the same — meaning there was no premium at all.
>
> Eighty-six percent of that number was an artefact. I found it, fixed it, and
> wrote nine tests that fail if it ever comes back. The honest number is
> thirty-four cents — which is now below the gate, so the agent doesn't take
> that trade.

---

## 1:42 — 1:58 · Slide 7 (separation of powers)

> The architecture is separation of powers. A deterministic screener decides
> what's valid. Claude decides what's attractive. And a deterministic risk
> module decides what's allowed — and it can overrule the model at any
> confidence.
>
> The legs the model sends back get compared against the real candidate, so a
> hallucinated contract is unexecutable. It returns a position size and we
> throw it away. And if there's no API key at all, the agent keeps running on
> arithmetic.

---

## 1:58 — 2:12 · Slide 8 (risk)

> Eight hard gates, and any single veto kills the trade outright. There's no
> confidence threshold that buys an exception.
>
> It can never end up naked — that's enforced in three separate places, and
> both legs always move as one atomic order. Five percent of equity per
> position, a three percent daily loss stop, and the stop-loss fires at
> forty-four percent of max loss, not a hundred.

---

## 2:12 — 2:28 · Slide 9 (it refuses)

> Here's a real screen. Seventeen structurally valid spreads, six survived the
> edge gate.
>
> AAPL had the richest premium and took the top two slots. And QQQ — where
> implied volatility was *below* realised — produced zero candidates and sat
> the day out.
>
> That's the whole point. The agent isn't looking for trades. It's looking for
> paid risk.

---

## 2:28 — 2:52 · **SWITCH TO THE LIVE DASHBOARD**

*Screen-record <https://chong1120.github.io/Vetoed/>. Scroll slowly, top to
bottom. Pause about two seconds on each section as you name it.*

> Everything it does is auditable. This is live right now.
>
> The equity curve. The screening funnel — how many spreads cleared the edge
> gate, how many the risk module approved, how many actually reached the
> broker.
>
> Implied versus realised volatility for every underlying, so you can see
> which ones are worth trading and which aren't paying.
>
> And every decision it made, with the measured edge on each one — including
> every trade the risk gates vetoed. The refusals are logged as carefully as
> the fills.

---

## 2:52 — 3:05 · Slide 11 (limits)

> I'll be straight about the limits. My quotes are indicative, not true NBBO.
> Four correlated tickers isn't real diversification. And a one-week contest
> is statistical noise — ten or twenty trades cannot tell a sixty percent win
> rate from a seventy percent one.
>
> That's exactly why the adaptive logic refuses to react to fewer than five
> closed trades, and can only ever tighten a limit. Never loosen one.

---

## 3:05 — 3:15 · Slide 12 (close)

> Vetoed. Seventy-seven tests, MIT licensed, running on Alpaca paper trading.
>
> An agent that's most useful when it says no. Thanks for watching.

---

# 90-second cut

If the submission caps at 90 seconds, use slides **1, 5, 6, 9, dashboard, 12**
and this script:

> **[1]** Most AI trading agents hand a language model the keys. Vetoed does
> the opposite — the AI is the least-trusted component in it. It sells
> defined-risk options spreads to harvest the volatility risk premium.
>
> **[5]** It measures that premium directly: the same spread priced twice
> through one model, changing only the volatility — what the market implies
> versus what the stock actually did. The difference is the edge, in dollars.
> Clear two dollars or the trade is discarded.
>
> **[6]** And here's the part I'm proud of. My first version computed those two
> numbers with different formulas, so it reported a two-seventy-five premium
> on a trade that had none. Eighty-six percent was an artefact. I caught it,
> fixed it, and wrote nine tests so it can't come back.
>
> **[9]** On a live screen: seventeen valid spreads, six survived. QQQ, where
> implied vol was below realised, produced zero candidates and sat out. The
> agent isn't looking for trades. It's looking for paid risk.
>
> **[dashboard]** Every decision is auditable — the screening funnel, the
> volatility on each underlying, the measured edge, and every trade the eight
> risk gates vetoed.
>
> **[12]** Seventy-seven tests, MIT, Alpaca paper. An agent that's most useful
> when it says no.

---

# Delivery notes

- **Slide 6 is your differentiator.** Most submissions claim their thing
  works. You are showing a number your own system got wrong, how you caught
  it, and the tests that stop it recurring. Slow down there.
- Say "vee-arr-pee edge" or just "the edge" — never spell out `vrp_edge`.
- Don't apologise for the flat P&L. The limits slide handles it, and framing
  the agent as *deliberately* refusing trades is stronger than pretending a
  week of data means something.
- If the dashboard is sparse when you record, say so plainly: "this is a fresh
  account, so the journal is still filling up." Judges respond better to that
  than to a silence they have to interpret.
