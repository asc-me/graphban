// Capture routes of the running web app as self-contained HTML (see README.md).
// usage: node capture.mjs <jobs.json> <outdir>
// A job: {name, path, variant: populated|empty|loading|error, clicks?, keys?, extract?: {label: selector}}.
// loading/error hold open or 500 every /api call except the ones the shell needs to boot (KEEP) — note
// `me(\/|\?|$)`: a bare `/api/me` prefix also matches /api/memory and silently un-fails those pages.
import { chromium } from 'playwright';
import fs from 'node:fs';
const [,, jobsFile, out] = process.argv;
const cfg = JSON.parse(fs.readFileSync(jobsFile, 'utf8'));
const BASE = cfg.base;
fs.mkdirSync(out, { recursive: true });
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

async function login(p) {
  await p.goto(BASE + '/');
  await p.waitForSelector('input');
  const ins = await p.$$('input');
  await ins[0].fill(cfg.email); await ins[1].fill(cfg.password);
  await p.keyboard.press('Enter');
  await p.waitForTimeout(2000);
}
const p0 = await ctx.newPage();
if (cfg.email) await login(p0);
await p0.close();

const KEEP = /\/api\/(auth|config|me(\/|\?|$)|projects\/?($|\?)|platform\/me|orgs\/?$|health)/;
for (const job of cfg.jobs) {
  const p = await ctx.newPage();
  if (job.variant === 'loading' || job.variant === 'error') {
    await p.route('**/api/**', async (r) => {
      const u = r.request().url();
      if (KEEP.test(u) || (job.keep && new RegExp(job.keep).test(u))) return r.continue();
      if (job.variant === 'error') return r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Internal Server Error"}' });
      // loading: never answer
    });
  }
  if (job.localStorage) await p.addInitScript(ls => { for (const [k,v] of Object.entries(ls)) localStorage.setItem(k, v); }, job.localStorage);
  try {
    await p.goto(BASE + job.path, { waitUntil: job.variant === 'loading' ? 'domcontentloaded' : 'networkidle', timeout: 20000 });
  } catch (e) { console.log('nav warn', job.name, e.message.split('\n')[0]); }
  await p.waitForTimeout(job.wait ?? (job.variant === 'loading' ? 1200 : 1800));
  if (job.keys) for (const k of job.keys) { await p.keyboard.press(k); await p.waitForTimeout(900); }
  if (job.clicks) for (const c of job.clicks) { try { await p.click(c, { timeout: 4000 }); await p.waitForTimeout(900); } catch (e) { console.log('click fail', job.name, c); } }
  if (job.extract) { const ex = {}; for (const [k, sel] of Object.entries(job.extract)) { ex[k] = await p.$$eval(sel, els => els.slice(0, 6).map(e => e.outerHTML)).catch(() => []); } fs.writeFileSync(`${out}/${job.name}.extract.json`, JSON.stringify(ex)); }
  // canvases (galaxy, graphs) -> <img>, so the card needs no script
  const canvases = await p.$$('canvas');
  for (const c of canvases) {
    try { const buf = await c.screenshot(); await c.evaluate((el, src) => { const img = document.createElement('img'); img.src = src; img.setAttribute('style', el.getAttribute('style') || ''); img.className = el.className; img.width = el.clientWidth; img.height = el.clientHeight; el.dataset.dsImg = src; el.replaceWith(img); }, 'data:image/png;base64,' + buf.toString('base64')); } catch {}
  }
  await p.screenshot({ path: `${out}/${job.name}.png` });
  const res = await p.evaluate(() => {
    let css = '';
    for (const ss of document.styleSheets) { try { for (const r of ss.cssRules) { if (r.constructor.name === 'CSSFontFaceRule') continue; css += r.cssText + '\n'; } } catch {} }
    document.querySelectorAll('input,textarea').forEach(i => { if (i.type === 'checkbox' || i.type === 'radio') { if (i.checked) i.setAttribute('checked', ''); } else if (i.value) i.setAttribute('value', i.value); if (i.tagName === 'TEXTAREA') i.textContent = i.value; });
    document.querySelectorAll('select').forEach(s => { [...s.options].forEach(o => o.selected ? o.setAttribute('selected', '') : o.removeAttribute('selected')); });
    const body = document.body.cloneNode(true);
    body.querySelectorAll('script,noscript,iframe').forEach(n => n.remove());
    return { css, body: body.innerHTML, title: document.title, htmlClass: document.documentElement.className, url: location.pathname };
  });
  fs.writeFileSync(`${out}/${job.name}.json`, JSON.stringify(res));
  console.log('ok', job.name, res.url, (res.body.length/1024|0) + 'KB');
  await p.close();
}
await b.close();
