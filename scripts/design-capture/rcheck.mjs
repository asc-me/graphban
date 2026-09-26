// Render-check every built card from file:// and fail on a blank frame or a failed request.
// Run from $DC_WORK (reads written.json, writes rc/*.png).
import { chromium } from 'playwright'; import fs from 'node:fs';
const list = JSON.parse(fs.readFileSync('written.json')); fs.mkdirSync('rc', {recursive:true});
const b = await chromium.launch(); const p = await b.newPage({viewport:{width:1520,height:1000}});
const report = [];
for (const w of list) {
  const errs = []; p.removeAllListeners('requestfailed'); p.on('requestfailed', r => errs.push(r.url().slice(0,80)));
  await p.goto('file://' + process.cwd() + '/bundle/' + w + '/index.html', {waitUntil:'load'}); await p.waitForTimeout(700);
  const info = await p.evaluate(() => ({ h: document.body.scrollHeight, frames: document.querySelectorAll('.ds-frame').length,
     blankFrames: [...document.querySelectorAll('.ds-frame')].filter(f => (f.innerText||'').trim().length < 5).length }));
  await p.screenshot({ path: `rc/${w.replace(/\//g,'_')}.png`, fullPage: true });
  report.push({ w, ...info, failed: errs.filter(u => !u.startsWith('data:')).slice(0,3) });
}
console.log(JSON.stringify(report, null, 0).replace(/},/g, '},\n'));
await b.close();
const bad = report.filter(x => x.blankFrames || x.failed.length);
if (bad.length) { console.error('render check failed:', bad.map(x => x.w).join(', ')); process.exit(1); }
