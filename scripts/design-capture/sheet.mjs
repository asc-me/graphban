// Contact sheet of capture screenshots for a quick eyeball: node sheet.mjs <dir> <out.png> [name-regex]
import { chromium } from 'playwright'; import fs from 'node:fs'; import path from 'node:path';
const [,, dir, outp, filter] = process.argv;
const files = fs.readdirSync(dir).filter(f => f.endsWith('.png') && (!filter || new RegExp(filter).test(f))).sort();
const html = `<body style="margin:0;background:#333;display:grid;grid-template-columns:repeat(3,720px);gap:6px;font:14px sans-serif;color:#fff">${files.map(f=>`<div><div>${f}</div><img width=720 src="data:image/png;base64,${fs.readFileSync(path.join(dir,f)).toString('base64')}"></div>`).join('')}</body>`;
const b = await chromium.launch(); const p = await b.newPage({viewport:{width:2172,height:600}});
await p.setContent(html); await p.screenshot({path: outp, fullPage: true}); await b.close();
