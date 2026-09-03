# Demo video — narration script

Matches the 12-slide deck exactly. Written for text-to-speech, so the spelling
is deliberately odd in places — see §3.

Slides: <https://claude.ai/code/artifact/33fbdd10-9ace-493f-a1c1-35f3ed49de54>
Dashboard: <https://chong1120.github.io/Vetoed/>

Runs about **6 minutes 53 seconds** at a normal narration pace. A 90-second cut
is in §2.

---

## 1. Paste this into ElevenLabs

```
[1 — DECK slide 1]
Most A.I. trading agents give a language model the keys, and hope it behaves. This one does the opposite.

It's called Vetoed. It trades options on Alpaca paper, and the A.I. is the least trusted component in it. On a normal cycle it measures about fifteen hundred spreads and refuses almost every one of them.

<break time="0.8s" />

[2 — DECK slide 2]
The problem is authority. If a model can pick the trade, decide the size, and send the order, then one hallucinated contract is a real loss. And you cannot unit test a language model.

But you can unit test the code that decides whether to obey it.

So the model's entire authority is this: pick one item from a list it did not write.

<break time="0.8s" />

[3 — DECK slide 3]
The strategy is selling defined risk credit spreads. Two contracts, always together. We sell one option and buy a cheaper one further out, collect the difference as premium, and the one we bought caps the loss.

The second leg is the important part. The worst case is known before the order is ever sent.

<break time="0.8s" />

[4 — DECK slide 4]
Now, why volatility. Implied volatility is how much movement option prices are charging for. Realised volatility is how much the stock has actually been moving.

On this screen, Apple's options were pricing about twenty four percent while the stock had been moving about nineteen. A ratio of one point two six — priced for more movement than has been happening.

That gap is what a premium seller is trying to collect.

<break time="1.0s" />

[5 — DECK slide 5]
So Vetoed measures that gap directly. It takes one spread and prices it twice, through the same model, with the same payoff. The only thing that changes is which volatility goes in.

At realised volatility that spread was worth about twenty eight dollars. At the market's implied volatility, minus fourteen. The difference, about forty two dollars, is the signal.

I want to be precise about what that is. It's our own model derived measure, motivated by volatility risk premium research. It is not the academic variance risk premium, which is a different thing measured a different way.

<break time="1.0s" />

[6 — DECK slide 6. The strongest slide. Do not rush it.]
And here's the part I'm actually proud of. I caught my own agent lying.

There was an I.W.M. trade where implied and realised volatility were almost identical, so there was essentially no gap to collect. The agent reported a healthy edge anyway.

Two defects. One side of the calculation used delta as if it were a probability, and it isn't quite. And the payoff between the two strikes was approximated at its midpoint, which a lognormal distribution does not respect.

Both changed real trade decisions, not just displayed numbers. On that trade the honest figure is thirty four cents, which is below the gate, so the agent doesn't take it.

I found them, fixed them, and the test suite went from seventy seven to two hundred and thirty six.

<break time="1.0s" />

[7 — DECK slide 7]
The architecture is separation of powers. A deterministic screener decides what is structurally valid. The model decides what looks attractive. A deterministic risk module decides what is allowed, and can overrule the model at any confidence.

Its contracts are checked against the shortlist, so an invented one is unexecutable. It returns a size, and we throw that away.

Everything that reaches the broker goes through Alpaca's own M.C.P. server — the single write path in the system. Every order carries a deterministic identifier, so a restart cannot open the same spread twice.

<break time="0.8s" />

[8 — DECK slide 8, then cut to slide 10 at "It learns from its own journal"]
Eight hard gates, and any single one rejects the trade outright. Five percent of equity in any one position, twenty five percent across all of them, a three percent daily loss halts the session, and a hard cap of twenty five contracts whatever the maths says.

It learns from its own journal, but only toward restriction — three losses in a row bans an underlying — and never on fewer than five closed trades. It can learn, but it cannot learn its way around the risk controls.

<break time="1.0s" />

[9 — LIVE DASHBOARD. Switch to the browser here. Deck slides 9 and 11 are
NOT used: slide 9 is what the demo shows better, and slide 11 is a static
summary of the thing you are about to show running. Skip Volatility in the
dashboard too - slides 4 and 5 already covered implied versus realised.]

[9a — Top of page: the KPI row, then the equity curve.]
This is it running, live. And the thing I want you to notice is that every number explains itself.

Equity isn't just a figure — it says "cash, plus what the positions are worth now". Capital at risk reads twenty one point four percent of equity, with "cap twenty five percent" printed beside it. Premium collected says "received up front", because it is not earned until the spread is closed. Open positions says "limit five concurrent". Nobody has to be told what any of these mean.

The curve carries a high-water mark, so a fall from a peak is the gap beneath the line.

<break time="0.7s" />

[9b — Decisions, Screened out tab. THE differentiator. Expand one breakdown and let it sit on screen.]
Now the part I most want you to see. Before a shortlist even exists, the agent measures the whole option chain across four underlyings — and on this cycle it rejected about fifteen hundred spreads.

Eight reasons, and you can open the breakdown. Every rejection is counted by the reason that stopped it, naming real contracts: open interest under five hundred, premium under ten cents, delta outside nought point one to nought point three five, measured edge below two dollars.

Those four numbers are the strategy. This is not an agent looking for a reason to trade. It spends almost all of its time declining.

<break time="0.7s" />

[9c — Closed positions. Press one AI reasoning panel and let it type.]
Four closed, four profitable, two thousand and fifty four dollars realised.

And look at how a closed trade reports itself. Take profit, plus six hundred and twenty four of eleven hundred and fifty eight maximum. Kept fifty four percent of premium. Twelve point nine percent of four thousand eight hundred and forty two dollars risked. The rule, the result, and the risk it was measured against, in one row.

Then press A.I. reasoning, and the agent writes the technical case from its own journal entry: what it measured, and what it did about it. Every figure is quoted from the record. It is not allowed to calculate.

<break time="0.7s" />

[9d — Open positions. Brief, then back to the deck.]
Five spreads are open, each showing its take profit and stop levels in dollars before either is hit. Real exposure, live — and every one cleared eight gates before it existed.

<break time="1.0s" />

[10 — DECK slide 12, the closing slide]
I'll be straight about the limits. My quotes are indicative, not true exchange best bid and offer. Skew isn't modelled. Twenty day realised volatility is an estimator, not a forecast. Four correlated tickers isn't real diversification. The exit thresholds are unvalidated.

This is Alpaca paper trading throughout — it refuses to start against a live account. And a contest week proves nothing about profitability. The research motivates the idea; it does not validate this system.

Vetoed. An agent that's most useful when it says no.

Thanks for watching.

## 2. Ninety second cut

Slides **1, 5, 6, 9, dashboard, 12**.

```
Most A.I. trading agents hand a language model the keys. Vetoed does the opposite. The A.I. is the least trusted component in it. It sells defined risk options spreads, where a second contract caps the loss before the order is sent.

<break time="0.7s" />

It measures its edge directly. One spread, priced twice through the same model, changing only the volatility. What the market is charging, versus what the stock has actually been doing. The difference is the signal. Clear two dollars, or the trade is discarded.

<break time="0.7s" />

And here's the part I'm proud of. I caught my own agent lying. On a trade where there was no gap to collect, it reported a healthy edge anyway. Two defects in the maths, both of which changed real decisions. I found them, fixed them, and the test suite went from seventy seven to two hundred and seven.

<break time="0.7s" />

On a live screen: twenty valid spreads, seven survived. Q.Q.Q., where implied volatility sat below realised, produced zero. The agent isn't looking for trades. It's looking for paid risk.

<break time="0.7s" />

Every decision is auditable, including every refusal. It runs unattended on a schedule, and it can learn from its own history, but only by becoming more restrictive.

<break time="0.7s" />

Vetoed. An agent that's most useful when it says no.
```

---

## 3. Why the spelling looks wrong

Written for the machine, not the page. Acronyms carry periods so they are read
letter by letter; `AAPL` is written **Apple** because that is how it is said
aloud; every figure is words; em dashes are gone because most engines rush the
pause. **Do not tidy these up** — the awkwardness is what makes it sound right.

**ElevenLabs settings:** Eleven Multilingual v2, stability 50–60%, similarity
75%, style 0–15%. Pick a calm mid-range voice and generate in **one take** so
the tone doesn't drift.

**Listen back for:** "I.W.M." and "Q.Q.Q." as letters; "lognormal" as one word;
"delta" not swallowed.

---

## 4. Slide timings

| From | Slide | Content |
|---|---|---|
| 0:00 | 1 | Hook |
| 0:15 | 2 | The problem |
| 0:47 | 3 | Credit spreads, both kinds |
| 1:20 | 4 | Implied vs realised |
| 1:53 | 5 | The measurement |
| 2:35 | 6 | **The bug I caught** |
| 3:27 | 7 | Separation of powers |
| 3:59 | 8 | Risk |
| 4:23 | 9 | It says no |
| 4:47 | 10 | Learning on a leash |
| 5:20 | — | **LIVE DASHBOARD** — equity, screened out, closed + AI reasoning, open (~1:40) |
| 7:00 | 12 | Limits and close |

Derived from word counts at 158 wpm plus the break tags. Your generated audio
will differ by a few seconds — line the slides up to what you actually get.

The deck autoplays on these timings: press **F** for fullscreen, then **P**.
A banner tells you when to switch to the dashboard and when to come back.

---

## 5. Recording it

1. Generate the narration, download the MP3.
2. Screen-record the deck **silently** — **F**, then **P**, and alt-tab to the
   dashboard when the banner appears. Windows records with **Win + Alt + R**.
3. Combine in **Clipchamp**: drop the video on the timeline, the MP3 under it,
   mute the video track, nudge until the slide changes land on the sentences.
4. Export 1080p.

Recording silently first is deliberate: a fluffed slide change then costs one
more screen capture, not another narration.

---

## 6. Delivery notes

- **Slide 6 is the differentiator.** Every submission claims their system
  works. You are showing a number your own system got wrong, how you caught
  it, and the tests that stop it returning. Slow down there.
- **Slide 5 has one sentence you must not paraphrase away** — the one saying
  this is *our* measure and not the academic definition. It is the difference
  between a defensible claim and an overclaim a finance judge will catch.
- **Don't apologise for flat P&L.** An agent that deliberately refuses unpaid
  risk is a better story than a week of noise presented as a track record.
- **If the dashboard is sparse when you record**, say so in your own voice over
  that section. An unexplained empty chart is worse than an explained one.
