#!/usr/bin/env node
/**
 * Colour-collision regression check for docs/index.html.
 *
 *   node scripts/check_colours.js          # table + pass/fail
 *   node scripts/check_colours.js --quiet  # exit code only
 *
 * Loads the real MODES/buildRamp out of index.html (no duplicated constants), rebuilds
 * every ramp, and compares every mode and every metro line against every other using
 * CIEDE2000 over the FULL ramps — all 24x24 sample pairs, not just base colours.
 * Comparing base colours alone hides the failure that actually shows up on screen: one
 * mode's fast end landing on another's slow end.
 *
 * Threshold logic
 * ---------------
 * metro Orange vs metro Yellow sits at ~10.8 and CANNOT be improved: both are STM's
 * official hexes. That sets the floor for the whole scheme, so the bar is "nothing we
 * control may be worse than STM's own closest pair".
 *
 * Pairs where BOTH colours are externally given (the four metro lines and REM's
 * #73A400) are reported but not enforced — their hues are brand constraints, so a
 * violation there is fixed with lightness/chroma, never by moving a hue.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const MIN_DE = 10.8;                 // = the immovable metro Orange/Yellow floor
const BASEMAP = [14, 16, 19];        // CARTO dark-matter background
const MIN_BASEMAP_DE = 12;           // below this a mode disappears into the map

const htmlPath = path.join(__dirname, "..", "docs", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).sort((a, b) => b.length - a.length)[0];

// Evaluate the page's script with just enough DOM to reach the colour system.
const stub = () => new Proxy({
  style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  children: [], textContent: "", innerHTML: "", value: 0, max: 0, className: "",
  addEventListener() {}, appendChild() {}, querySelectorAll: () => [], querySelector: () => null,
}, { get: (t, k) => (k in t ? t[k] : stub()), set: (t, k, v) => { t[k] = v; return true; } });

const sandbox = {
  document: { getElementById: stub, createElement: stub, addEventListener() {}, body: stub(), querySelectorAll: () => [] },
  window: { addEventListener() {} }, console: { warn() {}, log() {} },
  // hostname matters: docs/index.html branches on location.hostname to decide whether
  // it is running on GitHub Pages. Stub it as a local host so the colour system is
  // evaluated on the same path the local app takes.
  location: { search: "", hostname: "localhost", href: "http://localhost/" },
  fetch: () => new Promise(() => {}), performance: { now: () => 0 }, requestAnimationFrame: () => {},
  deck: { TripsLayer: function () {}, ScatterplotLayer: function () {}, MapboxOverlay: function () {} },
  maplibregl: { Map: function () { return { on() {}, addControl() {} }; }, AttributionControl: function () {} },
  URLSearchParams, Math, JSON, Object, Array, Number, String, Date, Float32Array,
  parseInt, parseFloat, isNaN, Promise, Error, setTimeout,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(js + "\n;globalThis.__X={MODES,LUT_STEPS,buildRamp,hexHue};", sandbox, { timeout: 10000 });
const { MODES, LUT_STEPS } = sandbox.__X;

/* ---------- CIELAB + CIEDE2000 ---------- */
const lin = c => { c /= 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
function lab([R, G, B]) {
  const r = lin(R), g = lin(G), b = lin(B);
  const x = (0.4124564*r + 0.3575761*g + 0.1804375*b) / 0.95047;
  const y = (0.2126729*r + 0.7151522*g + 0.0721750*b);
  const z = (0.0193339*r + 0.1191920*g + 0.9503041*b) / 1.08883;
  const f = t => (t > 216/24389 ? Math.cbrt(t) : (841/108) * t + 4/29);
  const fx = f(x), fy = f(y), fz = f(z);
  return [116*fy - 16, 500*(fx - fy), 200*(fy - fz)];
}
function deltaE00(l1, l2) {
  const [L1, a1, b1] = l1, [L2, a2, b2] = l2;
  const avgL = (L1 + L2) / 2;
  const C1 = Math.hypot(a1, b1), C2 = Math.hypot(a2, b2), avgC = (C1 + C2) / 2;
  const G = 0.5 * (1 - Math.sqrt(Math.pow(avgC, 7) / (Math.pow(avgC, 7) + Math.pow(25, 7))) || 0);
  const a1p = a1 * (1 + G), a2p = a2 * (1 + G);
  const C1p = Math.hypot(a1p, b1), C2p = Math.hypot(a2p, b2), avgCp = (C1p + C2p) / 2;
  const h1p = (Math.atan2(b1, a1p) * 180 / Math.PI + 360) % 360;
  const h2p = (Math.atan2(b2, a2p) * 180 / Math.PI + 360) % 360;
  let dhp = 0;
  if (C1p * C2p !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360; else if (dhp < -180) dhp += 360;
  }
  const dLp = L2 - L1, dCp = C2p - C1p;
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin(dhp * Math.PI / 360);
  let avghp;
  if (C1p * C2p === 0) avghp = h1p + h2p;
  else if (Math.abs(h1p - h2p) <= 180) avghp = (h1p + h2p) / 2;
  else avghp = (h1p + h2p < 360) ? (h1p + h2p + 360) / 2 : (h1p + h2p - 360) / 2;
  const T = 1 - 0.17*Math.cos((avghp-30)*Math.PI/180) + 0.24*Math.cos(2*avghp*Math.PI/180)
              + 0.32*Math.cos((3*avghp+6)*Math.PI/180) - 0.20*Math.cos((4*avghp-63)*Math.PI/180);
  const Sl = 1 + (0.015 * Math.pow(avgL-50,2)) / Math.sqrt(20 + Math.pow(avgL-50,2));
  const Sc = 1 + 0.045 * avgCp, Sh = 1 + 0.015 * avgCp * T;
  const Rt = -Math.sin(2 * (30*Math.exp(-Math.pow((avghp-275)/25,2))) * Math.PI/180)
             * 2 * Math.sqrt(Math.pow(avgCp,7)/(Math.pow(avgCp,7)+Math.pow(25,7)) || 0);
  return Math.sqrt(Math.pow(dLp/Sl,2) + Math.pow(dCp/Sc,2) + Math.pow(dHp/Sh,2)
                   + Rt*(dCp/Sc)*(dHp/Sh));
}
const minDE = (A, B) => {
  let m = Infinity;
  for (const x of A) for (const y of B) { const d = deltaE00(x, y); if (d < m) m = d; }
  return m;
};

/* ---------- collect every rendered ramp ---------- */
const entries = [];   // {name, labs, official}
for (const [key, m] of Object.entries(MODES)) {
  if (m.ramps) {
    for (const [lineId, ramp] of Object.entries(m.ramps)) {
      entries.push({ name: `${m.label} ${lineId}`, labs: ramp.map(lab), official: true });
    }
  } else if (m.ramp) {
    entries.push({ name: m.label, labs: m.ramp.map(lab), official: Boolean(m.official) });
  }
}

const quiet = process.argv.includes("--quiet");
const bmLab = lab(BASEMAP);
const pairs = [];
for (let i = 0; i < entries.length; i++)
  for (let j = i + 1; j < entries.length; j++)
    pairs.push({ d: minDE(entries[i].labs, entries[j].labs), a: entries[i], b: entries[j] });
pairs.sort((x, y) => x.d - y.d);

let failed = 0;
if (!quiet) {
  const w = Math.max(...entries.map(e => e.name.length));
  console.log(`\nCIEDE2000, minimum over full ${LUT_STEPS}-step ramps (all sample pairs)\n`);
  console.log(" ".repeat(w + 2) + entries.map(e => e.name.slice(0, 6).padStart(8)).join(""));
  for (const A of entries) {
    let row = "  " + A.name.padEnd(w);
    for (const B of entries) {
      row += A === B ? "—".padStart(8)
        : (pairs.find(p => (p.a === A && p.b === B) || (p.a === B && p.b === A)).d).toFixed(1).padStart(8);
    }
    console.log(row);
  }
  console.log("\nWeakest pairs:");
  for (const p of pairs.slice(0, 6)) {
    const bothOfficial = p.a.official && p.b.official;
    const tag = p.d >= MIN_DE ? "ok"
      : bothOfficial ? "exempt — both official, fix with lightness/chroma" : "FAIL";
    console.log(`  ${p.d.toFixed(1).padStart(5)}  ${p.a.name} vs ${p.b.name}  [${tag}]`);
  }
  console.log("\nVisibility against the dark basemap (slow end = worst case):");
  for (const e of entries) {
    const d = Math.min(...e.labs.map(l => deltaE00(l, bmLab)));
    const tag = d >= MIN_BASEMAP_DE ? "" : "  <-- TOO DARK";
    console.log(`  ${e.name.padEnd(w)} ${d.toFixed(1).padStart(6)}${tag}`);
  }
}

for (const p of pairs) {
  if (p.d < MIN_DE && !(p.a.official && p.b.official)) {
    console.error(`FAIL: ${p.a.name} vs ${p.b.name} = ΔE ${p.d.toFixed(2)} (min ${MIN_DE})`);
    failed++;
  }
}
for (const e of entries) {
  const d = Math.min(...e.labs.map(l => deltaE00(l, bmLab)));
  if (d < MIN_BASEMAP_DE) {
    console.error(`FAIL: ${e.name} is ΔE ${d.toFixed(2)} from the basemap (min ${MIN_BASEMAP_DE})`);
    failed++;
  }
}
console.log(failed ? `\n❌ ${failed} colour check(s) failed` : `\n✅ all colour checks passed (min controllable ΔE >= ${MIN_DE})`);
process.exit(failed ? 1 : 0);
