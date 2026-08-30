# Demo video — narration script

Written for text-to-speech. Section 1 is the block to paste straight into
ElevenLabs; everything after it is for assembling the video afterwards.

Slides: <https://claude.ai/code/artifact/33fbdd10-9ace-493f-a1c1-35f3ed49de54>
Dashboard: <https://chong1120.github.io/Vetoed/>

**Written for the machine, not the page.** Acronyms are spelled with periods
so they are read letter by letter, `AAPL` is written as "Apple" because that
is how it is said aloud, numbers are words, and em dashes are replaced with
full stops because they make most engines rush the pause. Do not "tidy" these
back up — the awkwardness on the page is what makes it sound right.

---

## 1. Paste this into ElevenLabs

Runs about **4 minutes** at a normal narration pace (619 words plus the
break tags). If the submission caps the length, use the ninety second cut
in section 2 instead.

```
Most A.I. trading agents give a language model the keys, and hope it behaves. This one does the opposite. It's called Vetoed. And the A.I. is the least trusted component in it.

<break time="0.8s" />

If a model can pick the trade, size the position, and send the order, then one hallucinated strike is an unbounded loss. You cannot unit test a language model. But you can unit test the code that decides whether to obey it.

So here, the model's entire authority is picking an index out of a list that was already validated.

<break time="0.8s" />

The strategy is selling defined risk credit spreads. The long leg caps the loss, so the worst case is known before the order is ever sent.

It works because option buyers systematically overpay for protection. That gap is the volatility risk premium. One of the better documented effects in the options literature.

<break time="0.8s" />

Now, most retail tooling ranks trades by delta derived expected value. But under risk neutral pricing, every fairly priced option trade has an expected value of exactly zero. That's a no arbitrage identity. Not an opinion. So ranking on it is ranking quote noise.

<break time="0.8s" />

So Vetoed measures the premium directly. It prices the same spread twice through the same model, changing only the volatility. Once with what the market implies. Once with what the stock actually did over twenty days.

The difference is the premium, in dollars. Clear two dollars, or the trade is discarded.

<break time="1.0s" />

And here's the part I'm actually proud of.

My first version computed those two numbers using two different formulas. They disagree even when the volatilities are identical. So the agent reported a two dollar seventy five premium on an I.W.M. trade, where implied and realised were basically the same. Meaning no premium existed at all.

Eighty six percent of that number was an artefact. I found it, I fixed it, and I wrote nine tests that fail if it ever comes back. The honest number was thirty four cents. Below the gate. So the agent doesn't take that trade.

<break time="1.0s" />

The architecture is separation of powers. A deterministic screener decides what's valid. Claude decides what's attractive. And a deterministic risk module decides what's allowed, and can overrule the model at any confidence.

The legs it sends back are checked against the real candidate, so a hallucinated contract is unexecutable. It returns a position size, and we throw it away.

<break time="0.8s" />

Eight hard gates. Any single veto kills the trade outright. It can never end up naked. Both legs always move as one atomic order. Five percent of equity per position. A three percent daily loss stop. And the stop fires at forty four percent of max loss. Not a hundred.

<break time="0.8s" />

Here's a real screen. Seventeen valid spreads. Six survived the edge gate.

Apple had the richest premium, and took the top two slots. And Q.Q.Q., where implied volatility was below realised, produced zero candidates and sat the day out.

That's the whole point. The agent isn't looking for trades. It's looking for paid risk.

<break time="1.0s" />

Everything it does is auditable. And this is live right now. The equity curve. The screening funnel. Implied versus realised volatility for every underlying. And every decision it made, with the measured edge on each one. Including every trade the risk gates vetoed.

<break time="1.0s" />

I'll be straight about the limits. My quotes are indicative, not true N.B.B.O. Four correlated tickers isn't real diversification. And a one week contest is statistical noise.

That's exactly why the adaptive logic refuses to react to fewer than five closed trades. And can only ever tighten a limit. Never loosen one.

<break time="1.0s" />

Vetoed. Seventy seven tests. M.I.T. licensed. Running on Alpaca paper trading.

An agent that's most useful when it says no.

Thanks for watching.
```

---

## 2. Ninety second cut

If the submission caps the length. Uses slides 1, 5, 6, 9, the dashboard, then 12.

```
Most A.I. trading agents hand a language model the keys. Vetoed does the opposite. The A.I. is the least trusted component in it. It sells defined risk options spreads to harvest the volatility risk premium.

<break time="0.7s" />

It measures that premium directly. The same spread, priced twice through one model, changing only the volatility. What the market implies, versus what the stock actually did. The difference is the edge, in dollars. Clear two dollars, or the trade is discarded.

<break time="0.7s" />

And here's the part I'm proud of. My first version computed those two numbers with different formulas. So it reported a two dollar seventy five premium on a trade that had none at all. Eighty six percent of it was an artefact. I caught it, fixed it, and wrote nine tests so it can't come back.

<break time="0.7s" />

On a live screen. Seventeen valid spreads. Six survived. Q.Q.Q., where implied volatility was below realised, produced zero candidates and sat out. The agent isn't looking for trades. It's looking for paid risk.

<break time="0.7s" />

Every decision is auditable. The screening funnel. The volatility on each underlying. The measured edge. And every trade the eight risk gates vetoed.

<break time="0.7s" />

Seventy seven tests. M.I.T. licensed. Alpaca paper trading. An agent that's most useful when it says no.
```

---

## 3. ElevenLabs settings

| Setting | Value | Why |
|---|---|---|
| Model | **Eleven Multilingual v2** | Most stable for long single-take narration. |
| Stability | **50–60%** | Lower values drift in tone across four minutes. |
| Similarity | **75%** | |
| Style exaggeration | **0–15%** | This is explanatory, not dramatic. High style oversells it. |
| Speaker boost | On | |

Pick a calm, mid-range voice — the tone is a confident engineer explaining
their work, not a movie trailer. Generate the whole thing in **one take** so
the tone does not shift between paragraphs.

If `<break>` tags are not supported by the voice or model you pick, delete
them. The paragraph breaks alone will carry the pacing.

**Listen back for these**, which are the words most likely to come out wrong:

- "I.W.M." and "Q.Q.Q." should be letters, not words.
- "artefact" — if it sounds odd, change it to "an error in the model".
- "no arbitrage" should sound like one idea, not two.
- "delta derived" should not sound like "delta, derived".

---

## 4. Slide timings

Generate the audio first, then line the slides up to it. These are the cut
points; adjust to whatever your actual audio does.

| From | Slide | Content |
|---|---|---|
| 0:00 | 1 | Title |
| 0:12 | 2 | The problem |
| 0:36 | 3 | The strategy |
| 0:57 | 4 | Why delta EV is empty |
| 1:14 | 5 | The measurement |
| 1:34 | 6 | **The bug I caught** |
| 2:13 | 7 | Separation of powers |
| 2:36 | 8 | Risk gates |
| 2:56 | 9 | 17 valid, 6 survived |
| 3:18 | — | **Cut to the live dashboard**, scroll slowly |
| 3:35 | 11 | Limits |
| 3:56 | 12 | Close |

Derived from the word count of each block above at 158 words per minute, plus
the break tags. Your generated audio will differ by a few seconds either way —
line the slides up to what you actually get rather than to this table.

The deck has an autoplay mode: press **P** and it advances on these timings by
itself, with a countdown, and a banner telling you when to switch to the
dashboard and when to come back. Press **F** for fullscreen first.

---

## 5. Assembling it

1. Generate the narration in ElevenLabs, download the MP3.
2. Screen-record the deck **silently**: fullscreen with **F**, autoplay with
   **P**, and alt-tab to the dashboard when the banner appears.
   Windows records with **Win + Alt + R**, saving to `Videos\Captures`.
3. Combine the two in **Clipchamp** (built into Windows 11) — drop the video
   on the timeline, drop the MP3 underneath, mute the video track, and nudge
   the clip so the slide changes land on the right sentences.
4. Export at 1080p.

Recording the deck silently first is deliberate: it means a fluffed slide
change costs one more screen recording, not a whole new narration take.

---

## 6. Delivery notes

- **The bug segment is the differentiator.** Most submissions claim their
  system works. You are showing a number your own system got wrong, how you
  caught it, and the tests that stop it recurring. No submission that did not
  actually find something can say that. Give it room.
- **Do not apologise for a flat P&L.** The limits section handles it, and an
  agent that deliberately refuses unpaid risk is a stronger story than a week
  of noise dressed up as a track record.
- **If the dashboard is sparse when you record it**, say so in your own voice
  over that section: "this is a fresh account, so the journal is still filling
  up". An unexplained empty chart is worse than an explained one.
