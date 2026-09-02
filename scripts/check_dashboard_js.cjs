/* Actually RENDER the dashboard, against real exported data.
 *
 * A syntax check is not enough. The page parses fine with a function missing;
 * the error only appears when that line runs, refresh() swallows it, and the
 * panel renders empty with nothing in the UI to say why. A rewrite deleted
 * llmErrLine and the whole decision log vanished silently while every check
 * passed.
 *
 * Scanning the source for undefined calls does not work either: the calls that
 * matter live inside template literals, and stripping those to avoid matching
 * prose ("6 leg(s)") removes the call sites too.
 *
 * So run it. Stub just enough DOM, feed it site/data.json, and let a missing
 * function throw the way it would in a browser.
 *
 *   python scripts/export_static.py && node scripts/check_dashboard_js.cjs
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..");
const HTML = path.join(ROOT, "dashboard", "static", "index.html");
const DATA = path.join(ROOT, "site", "data.json");

const src = fs.readFileSync(HTML, "utf8");
const block = src.match(/<script>([\s\S]*?)<\/script>/);
if (!block) {
  console.error("no <script> block found");
  process.exit(1);
}
let js = block[1];

// The page kicks off a fetch on load. Drop that; we drive the renderers by hand.
js = js.replace(/\(async \(\) => \{[\s\S]*?\}\)\(\);\s*$/, "");

if (!fs.existsSync(DATA)) {
  console.error("no site/data.json - run: python scripts/export_static.py");
  process.exit(1);
}
const data = JSON.parse(fs.readFileSync(DATA, "utf8"));

// --- the smallest DOM that lets the renderers run -------------------------
function makeEl() {
  const el = {
    innerHTML: "", textContent: "", hidden: false, style: {}, dataset: {},
    lastChild: { textContent: "" },
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, scrollIntoView() {},
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  return el;
}
const els = {};
const sandbox = {
  document: {
    getElementById: (id) => (els[id] = els[id] || makeEl()),
    addEventListener() {},
    querySelectorAll() { return []; },
  },
  window: {},
  console,
  fetch: () => Promise.reject(new Error("no network in this check")),
  setInterval() {}, setTimeout() {},
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(js, ctx, { filename: "index.html<script>" });
} catch (e) {
  console.error("FAILED TO EVALUATE THE PAGE SCRIPT:");
  console.error("   " + e.message);
  process.exit(1);
}

// --- drive every renderer, the way refresh() does -------------------------
const spots = {};
const ctxRun = (data.runs || []).find(
  (r) => r.context_json && Object.keys(r.context_json).length);
if (ctxRun) {
  for (const [sym, v] of Object.entries(ctxRun.context_json)) {
    if (v && v.spot != null) spots[sym] = v.spot;
  }
}

const held = data.broker_positions || [];
const panels = [
  ["status",    () => ctx.status(data.summary, data.runs, null)],
  ["kpis",      () => ctx.kpis(data.summary, data.orders, held, null)],
  ["chart",     () => ctx.chart(data.equity)],
  ["funnel",    () => ctx.funnel(data.runs, data.decisions, data.orders)],
  ["vols",      () => ctx.vols(data.runs)],
  ["positions", () => ctx.positions(data.positions, held, null, spots)],
  ["closed",    () => ctx.closedPositions(data.closed_positions)],
  ["decisions", () => ctx.decisions(data.decisions, data.runs, data.orders, data.closed_positions)],
];

let failed = false;
for (const [name, run] of panels) {
  try {
    run();
  } catch (e) {
    failed = true;
    console.error("PANEL '" + name + "' THREW: " + e.message);
    console.error("   In a browser this renders an empty panel and says nothing.");
  }
}
if (failed) process.exit(1);

// A panel that renders nothing is the exact failure this exists to catch.
for (const id of ["positions", "decisions", "kpis"]) {
  const el = els[id];
  if (!el || !el.innerHTML || el.innerHTML.length < 40) {
    console.error("PANEL '" + id + "' RENDERED EMPTY");
    process.exit(1);
  }
}

// And every filter must survive being selected. DEC is declared with const,
// which does not attach to a VM context's global object, so reach it by
// evaluating inside the context rather than through the sandbox.
const DEC = vm.runInContext("DEC", ctx);
// Driven from DEC_FILTERS itself, not a copy of it. A hand-kept list silently
// stops covering a filter the moment one is added - which is exactly what
// happened when "Screened out" was introduced and the check went on
// reporting five filters passing.
const FILTERS = vm.runInContext("DEC_FILTERS", ctx).map(f => f[0]);
for (const f of FILTERS) {
  DEC.filter = f;
  try {
    ctx.render();
  } catch (e) {
    console.error("FILTER '" + f + "' THREW: " + e.message);
    process.exit(1);
  }
  if (!els.decisions.innerHTML) {
    console.error("FILTER '" + f + "' RENDERED EMPTY");
    process.exit(1);
  }
}

// The screened-out row is synthesised from a run's eliminated tally, so with
// no such run in the fixture its renderer never executes and a fault in it
// ships unseen. Drive it explicitly.
const probe = {
  id: "screen-probe", ts: "2026-09-02T14:00:00+00:00",
  _screen: { measured: 131, rejected: 1782,
    by_reason: [{reason: "oi_too_thin", label: "open interest below the liquidity floor", count: 1029,
                 examples: ["SPY 690 CALL 2DTE - open interest 93, floor 500"]},
                {reason: "premium_too_small", label: "premium too small to be worth the risk", count: 514},
                {reason: "edge_too_low", label: "measured edge below the $2.00 minimum", count: 1}],
    near_misses: [{underlying: "QQQ", kind: "call_credit", short_strike: 721,
                   long_strike: 722, dte: 6, vrp_edge: 1.42, required: 2.0}] },
};
DEC.rows = [probe]; DEC.filter = "screened";
try {
  ctx.render();
} catch (e) {
  console.error("SCREENED-OUT ROW THREW: " + e.message);
  process.exit(1);
}
for (const must of ["1,782", "1,029", "open interest below", "QQQ", "$1.42",
                    "SPY 690 CALL 2DTE - open interest 93, floor 500"]) {
  if (!els.decisions.innerHTML.includes(must)) {
    console.error("SCREENED-OUT ROW MISSING '" + must + "'");
    console.error(els.decisions.innerHTML.slice(0, 700));
    process.exit(1);
  }
}

// The reasoning panel only builds when someone presses the button, so
// nothing above this line has ever executed askAgent. Drive it directly,
// once with an answer and once with the endpoint refusing.
async function checkAgentPanel(reply, expect, label) {
  const chat = {hidden: true, dataset: {}, innerHTML: ""};
  const wrap = {querySelector: () => chat, classList: {toggle(){}}};
  const btn = {
    parentElement: wrap,
    dataset: {note: "SPY260904C00769000", q: "Why did you open this position?"},
    setAttribute(){},
  };
  ctx.fetch = () => Promise.resolve(reply);
  vm.runInContext("NOTES", ctx).clear();
  await ctx.askAgent(btn);
  if (chat.hidden) { console.error(label + ": panel stayed closed"); process.exit(1); }
  for (const must of expect) {
    if (!chat.innerHTML.includes(must)) {
      console.error(label + ": missing '" + must + "'");
      console.error(chat.innerHTML.slice(0, 400));
      process.exit(1);
    }
  }
  // Pressing again must close it, not re-ask.
  await ctx.askAgent(btn);
  if (!chat.hidden) { console.error(label + ": second press did not close"); process.exit(1); }
}

// The chart gained a crosshair, a high-water mark and range presets, none of
// which the panel loop above can see - it only asserts the panel rendered
// something. Check the parts are actually there, and that a range preset
// changes what is plotted rather than silently doing nothing.
{
  const svg = els.chart.innerHTML;
  for (const must of ["hwm-line", "cross-line", "chart-hit", "grid-line"]) {
    if (!svg.includes(must)) {
      console.error("CHART MISSING '" + must + "'");
      process.exit(1);
    }
  }
  const C = vm.runInContext("CHART", ctx);
  const full = C.geom ? C.geom.rows.length : 0;
  C.range = "1d";
  ctx.drawChart();
  const day = C.geom ? C.geom.rows.length : 0;
  // Only assert narrowing when the fixture actually spans more than a day -
  // otherwise "24H" legitimately equals "All" and the check would be a lie.
  // Equality is the failure worth catching: a range function that quietly
  // returns everything looks exactly like one that works.
  const span = Date.parse(ctx.tsOf(data.equity[data.equity.length - 1]))
             - Date.parse(ctx.tsOf(data.equity[0]));

  // The broker's history uses epoch `t`, the journal an ISO `ts`. A chart that
  // reads only one loses its axis dates and its ranges against the other, and
  // renders perfectly while doing so - so drive it with the broker's shape too.
  const epoch = data.equity.map(r => ({
    t: Math.floor(Date.parse(r.ts) / 1000), equity: r.equity,
  }));
  ctx.chart(epoch, true);
  const esvg = els.chart.innerHTML;
  if (/>\s*(Invalid|NaN|undefined)/.test(esvg) || !/<text class="axis-text" x="62"/.test(esvg)) {
    console.error("CHART lost its dates on an epoch-timestamped series");
    process.exit(1);
  }
  const C2 = vm.runInContext("CHART", ctx);
  C2.range = "1d"; ctx.drawChart();
  const eday = C2.geom ? C2.geom.rows.length : 0;
  if (span > 864e5 && eday >= epoch.length) {
    console.error("CHART RANGE ignored epoch timestamps: all=" + epoch.length
                  + " 1d=" + eday);
    process.exit(1);
  }
  C2.range = "all";
  ctx.chart(data.equity, false);

  // A curve that disagrees with the account must never reach the chart.
  // This is the $202,226 case: Alpaca returned base + equity, the page drew
  // it, and told the reader the account had returned 102%.
  {
    const journal = data.equity;
    const doubled = journal.map(r => ({ t: Math.floor(Date.parse(r.ts) / 1000),
                                        equity: r.equity + 100000 }));
    const bad = ctx.pickSeries({ equity: 101952, equity_series: doubled }, journal);
    if (bad !== journal) {
      console.error("PAGE ACCEPTED an equity curve that contradicts the account");
      process.exit(1);
    }
    const good = journal.map(r => ({ t: Math.floor(Date.parse(r.ts) / 1000),
                                     equity: r.equity }));
    const okv = journal[journal.length - 1].equity;
    if (ctx.pickSeries({ equity: okv, equity_series: good }, journal) !== good) {
      console.error("PAGE REJECTED a broker curve that agrees with the account");
      process.exit(1);
    }
  }

  // The very first point cannot have made money: it IS the starting balance.
  // Measuring profit from the first point IN VIEW rather than from inception
  // made the tooltip announce a six-figure gain at the moment the account
  // opened, and made the figure change whenever a range preset was pressed.
  {
    const G = vm.runInContext("CHART", ctx).geom;
    if (!G || G.origin == null) {
      console.error("CHART has no inception baseline");
      process.exit(1);
    }
    const firstProfit = G.vals[0] - G.origin;
    if (Math.abs(firstProfit) > 0.005) {
      console.error("CHART first point claims a profit of " + firstProfit);
      process.exit(1);
    }
    // ...and it must not move when the range does.
    const o = G.origin;
    const C3 = vm.runInContext("CHART", ctx);
    C3.range = "1d"; ctx.drawChart();
    if (vm.runInContext("CHART", ctx).geom.origin !== o) {
      console.error("CHART baseline moved with the range preset");
      process.exit(1);
    }
    C3.range = "all"; ctx.drawChart();
  }
  if (!full || !day) {
    console.error("CHART RANGE produced nothing: all=" + full + " 1d=" + day);
    process.exit(1);
  }
  if (span > 864e5 && day >= full) {
    console.error("CHART RANGE did not narrow: all=" + full + " 1d=" + day
                  + " over " + (span / 864e5).toFixed(1) + " days of data");
    process.exit(1);
  }
  C.range = "all";
  ctx.drawChart();
}

// The VM has no CSS, so it cannot catch a panel that opens and never closes.
// This is the static half: anything toggled with .hidden in the script needs
// an author-level [hidden] rule, because a class setting display beats the
// browser's own [hidden] styling and the attribute stops doing anything.
if (/\.hidden\s*=/.test(src) && !/\[hidden\]\s*\{[^}]*display:\s*none/.test(src)) {
  console.error("ELEMENTS ARE TOGGLED WITH .hidden BUT NO [hidden] CSS RULE EXISTS");
  console.error("  a class setting display will override the browser default");
  process.exit(1);
}

// A pointer crossing a row fires enter, then down, then click - three asks
// for one note. They must share a single request.
async function checkNoteDedupe() {
  let calls = 0;
  ctx.fetch = () => { calls++; return Promise.resolve(
    {ok: true, json: () => Promise.resolve({explanation: "cached answer"})}); };
  vm.runInContext("NOTES", ctx).clear();
  vm.runInContext("NOTE_INFLIGHT", ctx).clear();
  const leg = "SPY260904C00769000";
  const all = await Promise.all([ctx.noteFor(leg), ctx.noteFor(leg), ctx.noteFor(leg)]);
  if (calls !== 1) {
    console.error("NOTE DEDUPE: " + calls + " requests for one note, expected 1");
    process.exit(1);
  }
  if (await ctx.noteFor(leg) !== "cached answer" || calls !== 1) {
    console.error("NOTE DEDUPE: cached answer not reused");
    process.exit(1);
  }
  if (all.some(x => x !== "cached answer")) {
    console.error("NOTE DEDUPE: concurrent callers got different answers");
    process.exit(1);
  }
}

(async () => {
  await checkNoteDedupe();
  await checkAgentPanel(
    {ok: true, json: () => Promise.resolve({explanation: "I measured the edge at $25.96 per spread."})},
    ["Why did you open this position?", "$25.96", "ai-msg bot"], "agent panel");
  await checkAgentPanel(
    {ok: false, json: () => Promise.resolve(null)},
    ["can't reach my reasoning"], "agent panel (endpoint down)");

  console.log("  dashboard: all 7 panels render, all " + FILTERS.length +
            " filters render, screened-out row renders, " +
            "AI reasoning panel opens, one request per note, chart ranges narrow (" + data.decisions.length +
            " decisions, " + data.positions.length + " spreads)");
})();
