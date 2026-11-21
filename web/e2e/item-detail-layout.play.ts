import { expect, test } from "@playwright/test";

import { COMPILE } from "./compile-wait";

/**
 * GRPH-1172: the Assistant model selector in the item detail panel ran past the panel's
 * right edge, because its widest <option> set its intrinsic width. jsdom has no layout,
 * so this is measured in a real browser against the real panel and CSS.
 */
// The first navigation pays for the Vite compile (GRPH-1812). The cap has to outlast COMPILE.
test.describe.configure({ timeout: 180_000 });

for (const width of [1280, 1600]) {
  test(`assistant model selector stays inside the item detail panel at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("http://127.0.0.1:5198/e2e/layout/");

    const panel = page.getByRole("dialog");
    const select = panel.locator("select").filter({ has: page.locator("option", { hasText: "qwen2.5-coder" }) });
    // Five options is the landmark. A harness that never paints them fails here.
    await expect(select.locator("option")).toHaveCount(5, COMPILE);
    await select.scrollIntoViewIfNeeded();

    const p = (await panel.boundingBox())!;
    const s = (await select.boundingBox())!;
    const box = (await select.locator("xpath=ancestor::div[contains(@class,'h-[420px]')]").boundingBox())!;

    await page.screenshot({ path: test.info().outputPath(`item-detail-${width}.png`) });
    test.info().annotations.push({
      type: `${width}px`,
      description: `panel ${p.x}..${p.x + p.width} · assistant box ${box.x}..${box.x + box.width} · select ${s.x}..${s.x + s.width}`,
    });

    expect(s.x).toBeGreaterThanOrEqual(box.x);
    expect(s.x + s.width).toBeLessThanOrEqual(box.x + box.width);
    expect(s.x + s.width).toBeLessThanOrEqual(p.x + p.width);
  });
}
