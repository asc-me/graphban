import { expect, test } from "@playwright/test";

/**
 * GRPH-1232: at 1280px the harness × function grid scrolls inside its own container and the
 * page does not scroll horizontally, even with every catalog leaf as a column and the cell
 * panel open. jsdom has no layout, so this is measured in a real browser with the real CSS.
 */
test("harness grid scrolls inside its container, not the page, at 1280px", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto("http://127.0.0.1:5198/e2e/layout/?view=harness-grid");

  // Families start collapsed at every width. The leaf strip is what a collapsed column shows.
  await expect(page.getByTestId("harness-grid-col")).toHaveCount(5);
  await expect(page.getByTestId("harness-leaf-strip").first()).toBeVisible();
  // Each family's button stays after expanding (it becomes collapse), so expand each one once:
  // the 5 family columns stay and their 25 functions slide in beside them.
  const collapsed = page.locator('[data-testid="harness-grid-family-expand"][aria-expanded="false"]');
  while ((await collapsed.count()) > 0) await collapsed.first().click();
  await expect(page.getByTestId("harness-grid-col")).toHaveCount(30);
  await page.getByTestId("harness-grid-cell").first().click();
  await expect(page.getByTestId("harness-cell-panel")).toBeVisible();
  // The panel slides in from the right; measure where it settles, not mid-slide.
  await page.getByTestId("harness-cell-panel").evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));

  const m = await page.evaluate(() => {
    const scroll = document.querySelector<HTMLElement>("[data-testid=harness-grid-scroll]")!;
    return {
      docScroll: document.documentElement.scrollWidth,
      inner: window.innerWidth,
      gridScroll: scroll.scrollWidth,
      gridClient: scroll.clientWidth,
    };
  });
  const panel = (await page.getByTestId("harness-cell-panel").boundingBox())!;

  await page.screenshot({ path: test.info().outputPath("harness-grid-1280.png") });
  test.info().annotations.push({
    type: "1280px",
    description: `page ${m.docScroll}/${m.inner} · grid ${m.gridScroll}/${m.gridClient} · panel ${panel.x}..${panel.x + panel.width}`,
  });

  expect(m.docScroll).toBeLessThanOrEqual(m.inner);
  expect(m.gridScroll).toBeGreaterThan(m.gridClient);
  expect(panel.x + panel.width).toBeLessThanOrEqual(m.inner);
});
