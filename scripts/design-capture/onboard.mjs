// Hosted first run for the seeded owner: create org "Ascme Labs" and project GRPH through the real UI.
import { chromium } from 'playwright';
const base = process.argv[2] ?? 'http://localhost:5198';
const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
await p.goto(base + '/'); await p.waitForSelector('input');
const ins = await p.$$('input'); await ins[0].fill('alex@ascme-labs.com'); await ins[1].fill('graphban'); await p.keyboard.press('Enter');
await p.waitForTimeout(2500);
await p.fill('input', 'Ascme Labs'); await p.click('button:has-text("Create organization")'); await p.waitForTimeout(3000);
const f = await p.$$('input'); await f[0].fill('Graphban'); await f[1].fill('GRPH');
await p.click('button:has-text("Create project")'); await p.waitForTimeout(3500);
console.log('onboarded ->', p.url()); await b.close();
