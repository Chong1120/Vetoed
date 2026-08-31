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
[1]
Most A.I. trading agents give a language model the keys, and hope it behaves. This one does the opposite. It's called Vetoed, and the A.I. is the least trusted component in it.

<break time="0.8s" />

[2]
Here's the problem. If a model can pick the trade, decide the size, and send the order, then one hallucinated contract is a real loss. And you cannot unit test a language model.

But you can unit test the code that decides whether to obey it.

So in Vetoed, the model's entire authority is this: pick one item from a list it did not write. It cannot build a trade, choose a size, or send an order.

<break time="0.8s" />

[3]
The strategy is selling defined risk credit spreads. That means two option contracts, always together.

In a put credit spread, we sell one put and buy a cheaper one further away. We collect the difference as premium, and the one we bought caps the loss. A call credit spread is the mirror image, for when we think a stock won't rise far.

The important part is the second leg. The worst case is known before the order is ever sent.

<break time="0.8s" />

[4]
Now, why volatility. There are two numbers that matter.

Implied volatility is how much movement option prices are charging for. Realised volatility is how much the stock has actually been moving lately.

On this screen, Apple's options were pricing about twenty four percent, while the stock had actually been moving about nineteen. A ratio of one point two six. Options priced for more movement than has been happening.

That gap is what a premium seller is trying to collect.

<break time="1.0s" />

[5]
So Vetoed measures that gap directly. It takes one spread and prices it twice, through the same model, with the same payoff. The only thing that changes is which volatility goes in.

Priced at realised volatility, that spread was worth about twenty eight dollars. Priced at the market's implied volatility, minus fourteen. The difference, about forty two dollars, is the signal.

I want to be precise about what that is. It's our own model derived measure, motivated by volatility risk premium research. It is not the academic definition of the variance risk premium, which is a different thing measured a different way.

<break time="1.0s" />

[6]
And here's the part I'm actually proud of. I caught my own agent lying.

There was an I.W.M. trade where implied and realised volatility were almost identical. So there was essentially no gap to collect. The agent reported a healthy edge anyway.

Two defects. One side of the calculation used delta as if it were a probability, and it isn't quite. And the payoff between the two strikes was approximated at its midpoint, which a lognormal distribution does not respect.

Both of those changed real trade decisions, not just displayed numbers. On that trade, the honest figure is thirty four cents, which is below the gate, so the agent doesn't take it.

I found them, fixed them, and the test suite went from seventy seven to two hundred and seven.

<break time="1.0s" />

[7]
The architecture is separation of powers. A deterministic screener decides what is structurally valid. A language model decides what looks attractive. And a deterministic risk module decides what is allowed, and can overrule the model at any confidence.

The contracts the model sends back are checked against the shortlist, so an invented one is unexecutable. It returns a position size, and we throw that away. And if the model is unavailable, the agent keeps trading on arithmetic.

<break time="0.8s" />

[8]
Eight hard gates, and any single one rejects the trade outright.

Five percent of equity at risk in any one position. A three percent daily loss halts the whole session. A hard cap of twenty five contracts, whatever the maths says. And zero naked positions, which is structurally impossible here because both legs move as one order.

<break time="0.8s" />

[9]
Here's a real screen. Twenty structurally valid spreads. Seven survived.

Apple had the richest premium and kept both of its candidates. And Q.Q.Q., where implied volatility was actually below realised, produced zero. The agent looked at it and passed.

That's the whole point. The agent isn't looking for trades. It's looking for paid risk.

<break time="1.0s" />

[10]
It also learns from its own history, but on a very short leash.

Every decision is journalled. Three losses in a row on one underlying, and it stops trading that underlying. A losing delta band gets narrowed.

But every adjustment is clamped against the defaults. It cannot increase size, it cannot loosen a hard limit, and it will not react to fewer than five closed trades.

It can learn, but it can't learn its way around the risk controls.

<break time="1.0s" />

[11a — screenshot 1: equity, funnel, volatility]
And this runs unattended. A schedule fires one full cycle roughly every thirty minutes during the US session, with nothing of mine switched on. Every cycle reconciles against the broker first, so a restart can't duplicate a position.

This is the dashboard. Read the funnel downward. Eight spreads cleared the edge gate. One was selected. One survived all eight risk gates. And zero were sent, because the market was closed. Every row says dry run, and I've left it that way.

<break time="0.5s" />

[11b — screenshot 2: the decisions table]
This is every decision it has made, including the ones it refused.

Top row, the language model choosing. Apple, the three ten put credit spread, forty two dollars of measured edge, approved at twelve contracts.

The row underneath is the one I'd point a judge at. The model call failed, a four twenty two, logged right there in the open. The agent didn't stop and it didn't guess. It fell back to arithmetic and reached the same spread. That's the fallback, firing for real.

<break time="1.0s" />

[12]
I'll be straight about the limits. My quotes are indicative, not true exchange best bid and offer. Skew isn't modelled. Twenty day realised volatility is an estimator, not a forecast. Four correlated tickers isn't real diversification. The exit thresholds are unvalidated.

And a contest week proves nothing about profitability. The research motivates the idea. It does not validate this system.

Vetoed. An agent that's most useful when it says no.

Thanks for watching.
```

---

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
| 5:20 | — | **Screenshot 1**, then **screenshot 2** (33s + 34s) |
| 6:27 | 12 | Limits and close |

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
