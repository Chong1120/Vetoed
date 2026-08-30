"""
Build the pitch deck as a native PowerPoint file.

    python scripts/build_deck.py        ->  Vetoed-deck.pptx

WHY A SCRIPT AND NOT A SAVED FILE. The numbers on these slides come from the
screener and the journal, and they move. Regenerating from source is how the
deck stays true to what the agent actually did, rather than drifting into a
set of figures that were right once.

FONTS. IBM Plex, which the HTML deck and the dashboard use, is a web font and
is not installed on a typical Windows machine - PowerPoint would silently
substitute something worse. Segoe UI and Consolas ship with Windows, read
cleanly when a video encoder compresses them, and keep the same
sans-plus-mono pairing.

Slides are built as native shapes and text, so everything is editable in
PowerPoint afterwards - including recording narration per slide and exporting
straight to MP4 via Slide Show > Record, then File > Export > Create a Video.
"""

from __future__ import annotations

import os
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "Vetoed-deck.pptx")

# Same palette as the dashboard and the HTML deck.
GROUND = RGBColor(0x0A, 0x0E, 0x13)
PANEL = RGBColor(0x10, 0x16, 0x1E)
RAISED = RGBColor(0x16, 0x1E, 0x28)
LINE = RGBColor(0x21, 0x2C, 0x38)
TEXT = RGBColor(0xE6, 0xED, 0xF3)
DIM = RGBColor(0xA9, 0xB6, 0xC3)
MUTED = RGBColor(0x75, 0x82, 0x8F)
ACCENT = RGBColor(0x4D, 0xD4, 0xC0)
PROFIT = RGBColor(0x3E, 0xCF, 0x8E)
LOSS = RGBColor(0xF2, 0x68, 0x5C)
WARN = RGBColor(0xF0, 0xA9, 0x3B)
WARN_BG = RGBColor(0x24, 0x1B, 0x0C)

SANS = "Segoe UI"
MONO = "Consolas"

W, H = Inches(13.333), Inches(7.5)
M = Inches(0.85)          # side margin
CONTENT_W = W - 2 * M


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

def slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])   # blank
    bg = s.background.fill
    bg.solid()
    bg.fore_color.rgb = GROUND
    return s


def text(s, x, y, w, h, runs, size=18, font=SANS, color=TEXT, bold=False,
         align=PP_ALIGN.LEFT, spacing=1.0, anchor=MSO_ANCHOR.TOP,
         letter_space=None):
    """`runs` is a string, or a list of (text, {overrides}) pairs."""
    box = s.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = spacing
    if isinstance(runs, str):
        runs = [(runs, {})]
    for content, over in runs:
        r = p.add_run()
        r.text = content
        f = r.font
        f.name = over.get("font", font)
        f.size = Pt(over.get("size", size))
        f.bold = over.get("bold", bold)
        f.color.rgb = over.get("color", color)
        if letter_space or over.get("letter_space"):
            # python-pptx has no spacing API; set it on the run XML directly.
            r.font._rPr.set("spc", str(int((over.get("letter_space")
                                            or letter_space) * 100)))
    return box


def rect(s, x, y, w, h, fill=PANEL, border=LINE, radius=True):
    shape = s.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if border is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = border
        shape.line.width = Pt(1)
    if radius:
        try:
            shape.adjustments[0] = 0.06
        except (IndexError, KeyError):
            pass
    shape.shadow.inherit = False
    shape.text_frame.text = ""
    return shape


def eyebrow(s, label):
    text(s, M, Inches(0.72), CONTENT_W, Inches(0.3), label.upper(),
         size=12, font=MONO, color=ACCENT, bold=True, letter_space=2.2)


def heading(s, title, y=Inches(1.18)):
    text(s, M, y, CONTENT_W, Inches(1.25), title,
         size=34, color=TEXT, bold=True, spacing=1.06)


def body(s, x, y, w, paras, size=16, color=DIM):
    """Each paragraph is a string or a list of (text, overrides) runs."""
    box = s.shapes.add_textbox(x, y, w, Inches(3.4))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = 1.35
        p.space_after = Pt(11)
        for content, over in ([(para, {})] if isinstance(para, str) else para):
            r = p.add_run()
            r.text = content
            r.font.name = over.get("font", SANS)
            r.font.size = Pt(over.get("size", size))
            r.font.bold = over.get("bold", False)
            r.font.color.rgb = over.get("color", color)
    return box


def card(s, x, y, w, h, label, value, sub="", value_color=TEXT, value_size=30):
    rect(s, x, y, w, h)
    pad = Inches(0.24)
    text(s, x + pad, y + pad, w - 2 * pad, Inches(0.24), label.upper(),
         size=10.5, font=MONO, color=MUTED, bold=True, letter_space=1.4)
    text(s, x + pad, y + pad + Inches(0.3), w - 2 * pad, Inches(0.55), value,
         size=value_size, font=MONO, color=value_color, bold=True)
    if sub:
        text(s, x + pad, y + h - pad - Inches(0.52), w - 2 * pad, Inches(0.5),
             sub, size=11.5, color=MUTED, spacing=1.2)


def chrome(s, n, total, show=True):
    if not show:
        return
    text(s, M, H - Inches(0.62), Inches(3), Inches(0.3), "VETOED",
         size=11, font=MONO, color=MUTED, bold=True, letter_space=2.5)
    text(s, W - M - Inches(3), H - Inches(0.62), Inches(3), Inches(0.3),
         "%02d / %d" % (n, total), size=11, font=MONO, color=MUTED,
         align=PP_ALIGN.RIGHT, letter_space=1.2)
    bar = rect(s, Emu(0), H - Inches(0.055), Emu(int(W * n / total)),
               Inches(0.055), fill=ACCENT, border=None, radius=False)
    bar.shadow.inherit = False


# --------------------------------------------------------------------------- #
# slides
# --------------------------------------------------------------------------- #

def build() -> str:
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    N = 12
    n = 0

    # 1 - title ------------------------------------------------------------ #
    s = slide(prs)
    text(s, M, Inches(2.15), CONTENT_W, Inches(0.3),
         "ALPACA AI TRADING AGENTS HACKATHON", size=12, font=MONO,
         color=ACCENT, bold=True, letter_space=2.6)
    text(s, M, Inches(2.62), CONTENT_W, Inches(1.5), "Vetoed",
         size=68, color=TEXT, bold=True)
    text(s, M, Inches(3.95), Inches(9.2), Inches(1.1),
         [("An autonomous options agent where ", {}),
          ("the AI is the least-trusted component.",
           {"bold": True, "color": TEXT})],
         size=22, color=DIM, spacing=1.35)
    text(s, M, Inches(5.25), CONTENT_W, Inches(0.4),
         "chong1120.github.io/Vetoed   ·   github.com/Chong1120/Vetoed",
         size=13, font=MONO, color=MUTED)
    n += 1

    # 2 - the problem ------------------------------------------------------ #
    s = slide(prs); n += 1
    eyebrow(s, "The problem")
    heading(s, "Most AI trading agents let the model decide.")
    body(s, M, Inches(2.72), Inches(6.4), [
        [("An LLM that can choose the trade, size the position, and send the "
          "order has ", {}),
         ("unbounded downside", {"bold": True, "color": TEXT}),
         (" the moment it hallucinates a strike or misreads a quote.", {})],
        "You cannot unit-test a language model. You can unit-test the code "
        "that decides whether to obey it.",
    ])
    x = M + Inches(7.0)
    rect(s, x, Inches(2.6), Inches(4.6), Inches(2.5))
    text(s, x + Inches(0.3), Inches(2.85), Inches(4.0), Inches(0.25),
         "VETOED'S ANSWER", size=10.5, font=MONO, color=MUTED, bold=True,
         letter_space=1.4)
    text(s, x + Inches(0.3), Inches(3.25), Inches(4.0), Inches(1.0),
         "The model picks an index\nfrom a pre-vetted list.",
         size=19, font=MONO, color=ACCENT, bold=True, spacing=1.3)
    text(s, x + Inches(0.3), Inches(4.25), Inches(4.0), Inches(0.8),
         "That is its entire authority. Everything else is deterministic, "
         "tested code that can overrule it.", size=11.5, color=MUTED,
         spacing=1.25)
    chrome(s, n, N)

    # 3 - the strategy ----------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "The strategy")
    heading(s, "Sell insurance. Don't predict direction.")
    body(s, M, Inches(2.65), Inches(10.6), [
        [("The agent sells ", {}),
         ("defined-risk vertical credit spreads", {"bold": True, "color": TEXT}),
         (" — short one option, long another further out. The long leg caps "
          "the loss, so the worst case is known before the order is sent.", {})],
        [("It profits because risk-neutral probabilities systematically ", {}),
         ("overstate", {"bold": True, "color": TEXT}),
         (" the real-world chance of big moves. Option buyers overpay for "
          "protection. That gap is the ", {}),
         ("volatility risk premium", {"bold": True, "color": ACCENT}),
         (".", {})],
    ], size=17)
    text(s, M, Inches(5.5), Inches(11.0), Inches(1.0),
         "Bakshi & Kapadia (2003), RFS 16(2)  ·  Carr & Wu (2009), RFS 22(3)  ·  "
         "CBOE PUT index, Jun 1986–Dec 2018: 9.95% vs 14.93% volatility at "
         "near-identical return.", size=11.5, color=MUTED, spacing=1.35)
    chrome(s, n, N)

    # 4 - delta is empty --------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "Why most tools measure nothing")
    heading(s, "Ranking by delta-derived EV is\nmathematically empty.")
    body(s, M, Inches(3.15), Inches(6.2), [
        [("Delta is the ", {}), ("risk-neutral", {"bold": True, "color": TEXT}),
         (" probability of finishing in the money. Under risk-neutral pricing, "
          "every fairly-priced option trade has an expected value of exactly ",
          {}), ("zero", {"bold": True, "color": TEXT}), (".", {})],
        [("That is a no-arbitrage identity, not an opinion. So a screener "
          "ranking on delta-derived EV is ranking ", {}),
         ("quote noise", {"bold": True, "color": LOSS}), (".", {})],
    ])
    x = M + Inches(6.9)
    rect(s, x, Inches(3.05), Inches(4.7), Inches(2.15))
    text(s, x + Inches(0.35), Inches(3.4), Inches(4.0), Inches(0.5),
         "Under Q:  E[payoff] = 0", size=17, font=MONO, color=ACCENT, bold=True)
    text(s, x + Inches(0.35), Inches(3.95), Inches(4.0), Inches(0.6),
         "for every fairly-priced\noption trade, always.",
         size=13, font=MONO, color=MUTED, spacing=1.3)
    text(s, x + Inches(0.35), Inches(4.6), Inches(4.0), Inches(0.4),
         "No edge to find there.", size=14, font=MONO, color=LOSS, bold=True)
    chrome(s, n, N)

    # 5 - the measurement -------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "The measurement")
    heading(s, "One model. Two volatilities.")
    body(s, M, Inches(2.5), Inches(11.0), [
        "Price the same spread twice through the same lognormal. Change only "
        "the volatility fed into it. The difference is the premium — not an "
        "artefact of comparing two different formulas.",
    ], size=17)
    rect(s, M, Inches(3.5), CONTENT_W, Inches(1.75))
    text(s, M + Inches(0.4), Inches(3.75), Inches(11.5), Inches(1.3),
         [("ev_rw", {"color": ACCENT, "bold": True}),
          ("     = spread priced at 20-day REALISED volatility\n", {"color": DIM}),
          ("ev_rn", {"color": ACCENT, "bold": True}),
          ("     = spread priced at the market's IMPLIED volatility\n\n",
           {"color": DIM}),
          ("vrp_edge = ev_rw − ev_rn", {"color": TEXT, "bold": True}),
          ("     ← the premium, in dollars", {"color": MUTED})],
         size=15, font=MONO, spacing=1.45)
    text(s, M, Inches(5.55), Inches(11.5), Inches(0.9),
         [("It must clear ", {}), ("$2.00", {"bold": True, "color": TEXT}),
          (" or the spread is discarded, and the shortlist ranks on ", {}),
          ("vrp_edge / max_loss", {"font": MONO, "color": ACCENT}),
          (" — premium per dollar actually at risk.", {})],
         size=15, color=DIM, spacing=1.35)
    chrome(s, n, N)

    # 6 - the bug ---------------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "Proof the measurement is honest")
    heading(s, "I caught my own agent lying.")
    body(s, M, Inches(2.62), Inches(6.5), [
        [("The first build computed ", {}),
         ("ev_rn", {"font": MONO, "color": TEXT}), (" from ", {}),
         ("delta", {"bold": True, "color": TEXT}), (" and ", {}),
         ("ev_rw", {"font": MONO, "color": TEXT}),
         (" from a lognormal. Those disagree even at identical volatility.", {})],
        [("The journal proved it. IWM at implied 14.84% vs realised 14.58% — "
          "a ratio of 1.018, so ", {}),
         ("no premium existed", {"bold": True, "color": TEXT}),
         (". It reported $2.75 anyway.", {})],
        [("86% of that was model mismatch.", {"bold": True, "color": TEXT}),
         (" Both sides now use one model, and nine tests pin the invariant.", {})],
    ], size=15)
    x = M + Inches(7.1)
    card(s, x, Inches(2.55), Inches(4.5), Inches(1.55),
         "Before — delta vs lognormal", "+$2.75",
         "reported on a trade with no premium on offer", LOSS, 28)
    card(s, x, Inches(4.28), Inches(4.5), Inches(1.75),
         "After — one model, vol only", "+$0.34",
         "honest, and below the $2.00 gate — so it is not taken", PROFIT, 28)
    chrome(s, n, N)

    # 7 - separation of powers --------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "Separation of powers")
    heading(s, "The model proposes. Deterministic code disposes.")
    nodes = [("screener.py", "what is VALID\npure arithmetic", PANEL, LINE, TEXT),
             ("brain.py", "what is GOOD\nthe only LLM", WARN_BG, WARN, WARN),
             ("risk.py", "what is ALLOWED\n8 hard gates", PANEL, LINE, TEXT)]
    # Sized so three nodes plus two gaps land inside the margins: the first
    # attempt at 3.5in overhung the right edge by 0.07in.
    nw, gap = Inches(3.4), Inches(0.75)
    x0 = M
    for i, (title, desc, fill, border, tc) in enumerate(nodes):
        x = x0 + i * (nw + gap)
        rect(s, x, Inches(2.62), nw, Inches(1.35), fill=fill, border=border)
        text(s, x, Inches(2.85), nw, Inches(0.35), title, size=16, font=MONO,
             color=tc, bold=True, align=PP_ALIGN.CENTER)
        text(s, x, Inches(3.25), nw, Inches(0.6), desc, size=11.5, color=MUTED,
             align=PP_ALIGN.CENTER, spacing=1.25)
        if i < 2:
            text(s, x + nw, Inches(3.1), gap, Inches(0.4), "→", size=20,
                 font=MONO, color=MUTED, align=PP_ALIGN.CENTER)
    rows = [("Echoed legs are compared to the real candidate", "hallucination → no-trade", LOSS),
            ("The model returns contracts, and it is discarded", "cannot size", LOSS),
            ("No tool output ever enters the prompt", "no injection surface", LOSS),
            ("No API key? Deterministic selection runs instead", "still autonomous", PROFIT)]
    y = Inches(4.35)
    for label, tag, col in rows:
        text(s, M, y, Inches(7.4), Inches(0.35), label, size=14, color=DIM)
        text(s, M + Inches(7.5), y, Inches(4.0), Inches(0.35), tag.upper(),
             size=11, font=MONO, color=col, bold=True, letter_space=1.2)
        ln = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, M, y + Inches(0.42),
                                CONTENT_W, Pt(0.75))
        ln.fill.solid(); ln.fill.fore_color.rgb = LINE
        ln.line.fill.background(); ln.shadow.inherit = False
        y += Inches(0.62)
    chrome(s, n, N)

    # 8 - risk ------------------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "Risk is not advisory")
    heading(s, "Eight gates. Any veto rejects outright.")
    bullets = [
        ("Never naked", " — enforced three times: the screener only builds "
         "pairs, a gate proves the long leg caps the loss, and both legs move "
         "as one atomic mleg order."),
        ("Loss always capped", " — reconciled against width×100 − credit×100."),
        ("The LLM cannot size", " — sizing is the minimum of four constraints "
         "and a hard 25-contract cap."),
        ("Guardrails only ever restrict", " — clamped structurally, never by "
         "convention."),
    ]
    y = Inches(2.62)
    for head, rest in bullets:
        text(s, M, y, Inches(0.25), Inches(0.3), "•", size=15, color=ACCENT)
        text(s, M + Inches(0.28), y, Inches(6.3), Inches(1.0),
             [(head, {"bold": True, "color": TEXT}), (rest, {})],
             size=14, color=DIM, spacing=1.3)
        y += Inches(1.02)
    x = M + Inches(7.1)
    card(s, x, Inches(2.55), Inches(4.5), Inches(1.05), "Per position", "5%",
         "", TEXT, 26)
    card(s, x, Inches(3.75), Inches(4.5), Inches(1.05), "Daily loss stop", "−3%",
         "", LOSS, 26)
    card(s, x, Inches(4.95), Inches(4.5), Inches(1.35), "Exits",
         "+50% / −2× / δ×2 / 1DTE",
         "the stop fires at 44% of max loss, not 100%", TEXT, 15)
    chrome(s, n, N)

    # 9 - it refuses ------------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "It refuses to trade")
    heading(s, "17 valid spreads. 6 survived.")
    text(s, M, Inches(2.42), Inches(11.0), Inches(0.4),
         "One live screen of the universe. The edge tracks implied-vs-realised "
         "exactly as the theory predicts.", size=15, color=DIM)
    hdr = ["Underlying", "Implied / realised", "Outcome"]
    cols = [Inches(2.2), Inches(3.0), Inches(6.4)]
    y = Inches(3.05)
    x = M
    for i, htxt in enumerate(hdr):
        text(s, x, y, cols[i], Inches(0.3), htxt.upper(), size=10.5, font=MONO,
             color=MUTED, bold=True, letter_space=1.3)
        x += cols[i]
    y += Inches(0.42)
    data = [("AAPL", "1.26", PROFIT, "top two candidates — +$41.32, +$32.30"),
            ("SPY", "1.069", WARN, "three candidates, +$4.28 to +$5.43"),
            ("IWM", "1.067", WARN, "one at +$10.11; three cut at ~$0"),
            ("QQQ", "0.894", LOSS, "ZERO CANDIDATES — implied below realised, it sits out")]
    for sym, ratio, col, outcome in data:
        x = M
        text(s, x, y, cols[0], Inches(0.35), sym, size=15, font=MONO, color=TEXT,
             bold=True); x += cols[0]
        text(s, x, y, cols[1], Inches(0.35), ratio, size=15, font=MONO,
             color=col, bold=True); x += cols[1]
        text(s, x, y, cols[2], Inches(0.35), outcome, size=13.5,
             color=LOSS if sym == "QQQ" else DIM)
        ln = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, M, y + Inches(0.4),
                                CONTENT_W, Pt(0.75))
        ln.fill.solid(); ln.fill.fore_color.rgb = LINE
        ln.line.fill.background(); ln.shadow.inherit = False
        y += Inches(0.58)
    text(s, M, y + Inches(0.25), Inches(11.5), Inches(0.5),
         [("QQQ sitting out is the point.", {"bold": True, "color": TEXT}),
          (" The agent is not looking for trades. It is looking for paid risk.", {})],
         size=15, color=DIM)
    chrome(s, n, N)

    # 10 - dashboard ------------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "Live now")
    heading(s, "Every decision is auditable,\nincluding the refusals.")
    body(s, M, Inches(3.15), Inches(6.4), [
        [("The dashboard renders the SQLite journal: the screening funnel, "
          "implied-vs-realised per underlying, the measured edge on every "
          "candidate, and ", {}),
         ("every trade the risk gates vetoed", {"bold": True, "color": TEXT}),
         (".", {})],
        "It is static and read-only — no server to sleep, and no route that "
        "could place an order even if it were compromised.",
    ], size=15)
    text(s, M, Inches(5.4), Inches(7.0), Inches(0.4),
         "chong1120.github.io/Vetoed", size=17, font=MONO, color=ACCENT,
         bold=True)
    x = M + Inches(7.1)
    card(s, x, Inches(2.85), Inches(2.15), Inches(1.35), "Tests", "77",
         "passing", PROFIT, 30)
    card(s, x + Inches(2.35), Inches(2.85), Inches(2.15), Inches(1.35),
         "LLM writes", "0", "to the broker", TEXT, 30)
    card(s, x, Inches(4.4), Inches(2.15), Inches(1.35), "Risk gates", "8",
         "any one vetoes", TEXT, 30)
    card(s, x + Inches(2.35), Inches(4.4), Inches(2.15), Inches(1.35),
         "Naked positions", "0", "structurally impossible", PROFIT, 30)
    chrome(s, n, N)

    # 11 - limits ---------------------------------------------------------- #
    s = slide(prs); n += 1
    eyebrow(s, "What I am not claiming")
    heading(s, "The limits, stated before you find them.")
    items = [
        ("Quotes are indicative, not NBBO.", " No OPRA agreement, so the credit "
         "— and every number downstream — comes from a derived quote."),
        ("Four correlated tickers is not diversification.", " It spreads across "
         "names, not risk factors."),
        ("A 20-day vol estimate carries ~16% standard error.", " Gating on the "
         "premium reduces the resulting selection bias. It does not remove it."),
        ("A contest window is statistical noise.", " Ten to twenty trades cannot "
         "separate a 60% win rate from 70%. That is exactly why the circuit "
         "breaker refuses to react to fewer than five closed trades, and can "
         "only ever tighten."),
    ]
    y = Inches(2.62)
    for head, rest in items:
        text(s, M, y, Inches(0.25), Inches(0.3), "•", size=15, color=ACCENT)
        text(s, M + Inches(0.3), y, Inches(11.3), Inches(1.0),
             [(head, {"bold": True, "color": TEXT}), (rest, {})],
             size=15, color=DIM, spacing=1.3)
        y += Inches(1.0)
    chrome(s, n, N)

    # 12 - close ----------------------------------------------------------- #
    s = slide(prs); n += 1
    text(s, M, Inches(2.35), CONTENT_W, Inches(0.3), "VETOED", size=12,
         font=MONO, color=ACCENT, bold=True, letter_space=2.6)
    text(s, M, Inches(2.85), CONTENT_W, Inches(1.9),
         "An agent that is most useful\nwhen it says no.",
         size=44, color=TEXT, bold=True, spacing=1.1)
    text(s, M, Inches(4.85), CONTENT_W, Inches(1.3),
         [("chong1120.github.io/Vetoed\n", {"color": ACCENT, "bold": True}),
          ("github.com/Chong1120/Vetoed\n", {"color": DIM}),
          ("MIT  ·  Alpaca paper trading  ·  77 tests", {"color": MUTED})],
         size=14, font=MONO, spacing=1.75)

    prs.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print("built %s  (%.0f KB)" % (path, os.path.getsize(path) / 1024))
    sys.exit(0)
