// Rendered contrast audit. Measures what the browser actually paints, not what the tokens
// imply: every control border and every text node is compared against the background really
// behind it, composited through transparent ancestors.
//
//   node scripts/design-capture/contrast-audit.mjs [baseUrl] [email] [password]
//   (defaults: http://localhost:5199 and the seed owner)
//
// Exits non-zero on any violation. Two floors, and the difference between them is the whole
// point of the --color-line / --color-control split:
//   * control borders  >= 3:1   (WCAG 1.4.11 — the border identifies the control)
//   * text             >= 4.5:1 (1.4.3; 3:1 for large text)
// Structural hairlines are decorative and deliberately ~1.2:1. They are not audited, and
// raising them to the control floor is what outlined every panel in the app.
//
// Token-pair assertions in web/src/__tests__/design-tokens.test.ts cannot see nesting: a
// hairline on a card inside a lighter surface is a pairing no static test enumerates. This
// walks the real tree instead. It found three controls still on hairlines that a source scan
// reported as fully converted, because their classes came from a cva variant map.
import { chromium } from "playwright";

const BASE = process.argv[2] || "http://localhost:5199";
const EMAIL = process.argv[3] || "alex@ascme-labs.com";
const PASSWORD = process.argv[4] || "graphban";

const ROUTES = [
  "/home", "/tracker", "/triage", "/requests", "/prds", "/roadmap", "/memory", "/lessons",
  "/activity", "/live", "/harness", "/usage", "/fleet", "/outposts", "/mcp-tools",
  "/settings/project", "/settings/box", "/profile",
];

const AUDIT = () => {
  const px = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s);
    if (!m) return null;
    const a = m[1].split(",").map((x) => parseFloat(x));
    return { r: a[0], g: a[1], b: a[2], a: a.length > 3 ? a[3] : 1 };
  };
  const L = (c) => {
    const f = (x) => { const s = x / 255; return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const CR = (x, y) => { const a = L(x), b = L(y); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
  });
  // The painted background: composite every translucent ancestor until one is opaque.
  const bgOf = (el) => {
    let n = el, acc = null;
    while (n && n.nodeType === 1) {
      const c = px(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) { acc = acc ? over(acc, c) : c; if (acc.a >= 0.999) return acc; }
      n = n.parentElement;
    }
    return acc || { r: 10, g: 11, b: 13, a: 1 };
  };
  const CONTROLS = new Set(["BUTTON", "INPUT", "TEXTAREA", "SELECT"]);
  const out = { controls: [], text: [] };
  for (const el of document.querySelectorAll("*")) {
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;

    const width = ["Top", "Right", "Bottom", "Left"]
      .reduce((s, k) => s + parseFloat(cs["border" + k + "Width"] || 0), 0);
    if (width > 0 && CONTROLS.has(el.tagName)) {
      const bc = px(cs.borderTopColor);
      if (bc && bc.a > 0.05) {
        const bg = bgOf(el.parentElement || el);
        const ratio = CR(over(bc, bg), bg);
        if (ratio < 3) {
          out.controls.push({
            tag: el.tagName, color: cs.borderTopColor, ratio: +ratio.toFixed(2),
            cls: (el.className || "").toString().slice(0, 110),
            text: (el.textContent || "").trim().slice(0, 40),
          });
        }
      }
    }

    // Only elements that own a text node directly — otherwise every ancestor is re-counted.
    if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const col = px(cs.color);
    if (!col || col.a < 0.05) continue;
    const size = parseFloat(cs.fontSize);
    const weight = parseInt(cs.fontWeight) || 400;
    const need = size >= 24 || (size >= 18.66 && weight >= 700) ? 3 : 4.5;
    const bg = bgOf(el);
    const ratio = CR(over(col, bg), bg);
    if (ratio < need) {
      out.text.push({
        color: cs.color, size, weight, need, ratio: +ratio.toFixed(2),
        cls: (el.className || "").toString().slice(0, 110),
        text: (el.textContent || "").trim().slice(0, 45),
      });
    }
  }
  return out;
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const login = await ctx.newPage();
await login.goto(BASE + "/");
await login.waitForSelector("input", { timeout: 30000 });
const fields = await login.$$("input");
await fields[0].fill(EMAIL);
await fields[1].fill(PASSWORD);
await login.keyboard.press("Enter");
await login.waitForTimeout(3000);
await login.close();

const controls = new Map(), text = new Map();
let visited = 0;
for (const route of ROUTES) {
  const page = await ctx.newPage();
  try {
    await page.goto(BASE + route, { waitUntil: "domcontentloaded", timeout: 25000 });
    await page.waitForTimeout(1500);
    const res = await page.evaluate(AUDIT);
    visited++;
    for (const c of res.controls) {
      const k = `${c.color}|${c.tag}|${c.ratio}`;
      if (!controls.has(k)) controls.set(k, { ...c, routes: [] });
      controls.get(k).routes.push(route);
    }
    for (const t of res.text) {
      const k = `${t.color}|${t.size}|${t.ratio}`;
      if (!text.has(k)) text.set(k, { ...t, routes: [] });
      text.get(k).routes.push(route);
    }
  } catch (e) {
    console.log(`  route failed: ${route} — ${e.message.split("\n")[0]}`);
  }
  await page.close();
}
await browser.close();

// A run that reached nothing must not read as a pass. Absence of findings is only evidence
// when something was actually looked at.
if (visited < ROUTES.length / 2) {
  console.error(`\nFAIL: only ${visited}/${ROUTES.length} routes loaded — audit is not evidence.`);
  process.exit(2);
}
console.log(`\naudited ${visited}/${ROUTES.length} routes`);
console.log("\n=== control borders below 3:1 ===");
if (!controls.size) console.log("  none");
for (const v of [...controls.values()].sort((a, b) => a.ratio - b.ratio)) {
  console.log(`  ${v.ratio}  <${v.tag}> ${v.color}  "${v.text}"  [${v.routes.length} routes]`);
  console.log(`        ${v.cls}`);
}
console.log("\n=== text below its floor ===");
if (!text.size) console.log("  none");
for (const v of [...text.values()].sort((a, b) => a.ratio - b.ratio)) {
  console.log(`  ${v.ratio} (needs ${v.need})  ${v.color} ${v.size}px/${v.weight}  "${v.text}"  [${v.routes.length} routes]`);
}
const total = controls.size + text.size;
console.log(`\n${total} violation kind(s)`);
process.exit(total ? 1 : 0);
