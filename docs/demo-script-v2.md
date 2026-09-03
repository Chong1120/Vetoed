# Demo video — narration script (v2, recut to the organiser's brief)

The brief: *"A short demo showing what you built, how the AI agent works like the
workflow, and how it uses Alpaca."*

Leads with the system, runs one continuous workflow from screening to execution,
and gives the dashboard more than half the running time. Deck is
`Vetoed-deck-video.pptx`, using slides 1, 2, 4, 5, 6 and 7 — **slide 3 is not
narrated in this cut**. Paste `docs/elevenlabs-v2.txt` into ElevenLabs.

Timing: deck 2:11, website 2:56, total 5:07 at 156 wpm. ElevenLabs usually runs
slightly faster - expect 4:45 to 5:05. Check the export before submitting.

---

[1 — DECK 1, title]
This is Vetoed: an autonomous options agent that trades defined risk credit spreads on Alpaca paper, unattended, all day. The A.I. inside it is the least trusted component. Here is the workflow, then I will show you it running.

<break time="0.7s" />

[2 — DECK 2, THE STRATEGY]
It sells credit spreads: sell one option, buy a cheaper one further out, keep the difference. The one it buys caps the loss, and both legs go as one order — so the worst case is known before the order exists.

<break time="0.7s" />

[3 — DECK 4, THE MEASUREMENT. Skip deck slide 3 in this cut.]
It only sells when the market is overpaying for movement, and it measures that directly: one spread, priced twice through the same model, changing only the volatility — what the stock has actually been doing, against what options are charging. Twenty eight dollars at realised, minus fourteen at implied. A forty two dollar gap, and anything under two dollars is thrown away.

To be precise, that is our own measure, not the academic variance risk premium.

<break time="0.8s" />

[4 — DECK 5, SEPARATION OF POWERS. The workflow slide.]
That is the workflow. A deterministic screener builds and measures every valid spread. The language model does one thing: pick one candidate from that shortlist. It cannot invent a contract — its answer is checked against the list — and the size it suggests is discarded. Deterministic risk code sets the size and can overrule it at any confidence.

Then execution, through Alpaca's own M.C.P. server: both legs as one atomic multi-leg order, with a deterministic order I.D. so a restart cannot open the same spread twice. It is the only write path in the system.

<break time="0.8s" />

[5 — DECK 6, RISK IS NOT ADVISORY]
Eight hard gates sit in front of all of it. Five percent of equity per position, twenty five percent across all, a three percent daily loss halts the session, twenty five contracts hard cap. Any one gate rejects the trade outright.

<break time="1.0s" />

[6 — WEBSITE. Switch to the browser. Everything from here is the live dashboard.]

[6a — Top of the page: KPI row, then the equity curve.]
Here it is running, market open. Every number explains itself.

Equity, a hundred and one thousand seven hundred and ninety three — "cash, plus what the positions are worth now". Cash is higher, because eight thousand eight hundred and sixty nine of premium is received but, as the label says, not yet earned. Capital at risk, twenty one point three percent against a twenty five percent cap. Five positions, "limit five concurrent".

The curve carries its own high-water mark, so a drawdown is the gap beneath the line.

<break time="0.7s" />

[6b — Volatility panel. Short.]
This is the edge per underlying — implied against realised, and what that gap is worth in premium right now. When implied falls below realised, the agent stops selling that name.

<break time="0.7s" />

[6c — Decisions, SCREENED OUT tab. Expand the breakdown, then the near-miss list.]
Now the part I most want you to see. Before a shortlist exists, the agent measures the whole option chain across four underlyings. On this cycle it measured seventy spreads in full and rejected one thousand five hundred and three.

Eight reasons, and every one opens. Nine hundred and nineteen for open interest below the liquidity floor — naming the contract: S.P.Y. six ninety call, open interest two, floor five hundred. Four hundred and five for premium under ten cents. Fifty seven for delta outside the band.

And the six it measured in full and still declined. A Q.Q.Q. spread, edge one dollar seventy four against two dollars required. It missed by twenty six cents, and the agent said no.

<break time="0.7s" />

[6d — Closed positions. The IWM row, AI reasoning open.]
Four closed, four profitable, two thousand and fifty four realised. Each row carries the rule, the result and the risk together.

I.W.M., sold the two ninety five call, bought the three hundred, twelve contracts. Collected eleven hundred and fifty eight, kept fifty four percent of it — twelve point nine percent of the four thousand eight hundred and forty two at risk.

Then press A.I. reasoning, and the agent writes the case from its own journal: expected value twenty four thirty nine at realised volatility, the market pricing it at six sixty one, an edge of seventeen dollars seventy seven. Every figure quoted from the record — it is not allowed to calculate.

<break time="0.7s" />

[6e — Open positions. The QQQ row and its payoff diagram.]
And the open book. Q.Q.Q., sold seven fourteen, bought seven nineteen, fourteen contracts. Collected nineteen hundred and forty six; most it can lose is five thousand and fifty four. Those add to exactly seven thousand — five dollars of width, times a hundred, times fourteen. Defined risk you can check on screen.

Take profit sits at nine hundred and seventy three, half the credit; the stop at minus three thousand eight hundred and ninety two, twice it — both set before the order existed. The payoff diagram shows where profit flattens and where the loss stops.

<break time="1.0s" />

[7 — DECK 7, WHAT I AM NOT CLAIMING]
The limits, plainly. Quotes are indicative, not exchange best bid and offer. Skew is not modelled. This is Alpaca paper trading throughout, and a contest week proves nothing about profitability.

Vetoed. An agent most useful when it says no.
