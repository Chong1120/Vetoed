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
  ["decisions", () => ctx.decisions(data.decisions, data.runs, data.orders)],
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
for (const f of ["all", "traded", "vetoed", "passed", "dry"]) {
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

console.log("  dashboard: all 7 panels render, all 5 filters render (" +
            data.decisions.length + " decisions, " + data.positions.length + " spreads)");
