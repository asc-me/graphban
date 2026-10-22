import { expect, test } from "@playwright/test";

interface BenchResult {
  paintMs: number;
  panP95: number;
  hitMs: number;
  nodes: number;
}

type BenchFn = (name: "sparse" | "dense") => BenchResult;

/**
 * D9 budgets are Canvas 2D on `sparse` only. Dense is loaded so a paint that
 * throws is still a failure; its pan time is a WebGL budget (S10).
 *
 * The callbacks run in the page, so they only read `window.__bench`. A helper
 * closed over from this file is not defined there.
 */
test("sparse canvas stays inside the frame budget and dense paints", async ({ page }) => {
  await page.goto("/");
  await page.waitForFunction(
    () => typeof (window as unknown as { __bench?: BenchFn }).__bench === "function",
  );
  const sparse = await page.evaluate(() =>
    (window as unknown as { __bench: BenchFn }).__bench("sparse"),
  );
  expect(sparse.nodes).toBeGreaterThan(800);
  expect(sparse.paintMs).toBeLessThanOrEqual(1000);
  expect(sparse.panP95).toBeLessThanOrEqual(16);
  expect(sparse.hitMs).toBeLessThanOrEqual(1);
  test.info().annotations.push({
    type: "sparse",
    description: `paint ${sparse.paintMs.toFixed(1)}ms pan p95 ${sparse.panP95.toFixed(1)}ms hit ${sparse.hitMs.toFixed(3)}ms nodes ${sparse.nodes}`,
  });

  const dense = await page.evaluate(() =>
    (window as unknown as { __bench: BenchFn }).__bench("dense"),
  );
  expect(dense.nodes).toBeGreaterThan(800);
  expect(dense.paintMs).toBeGreaterThan(0);
  test.info().annotations.push({
    type: "dense",
    description: `paint ${dense.paintMs.toFixed(1)}ms pan p95 ${dense.panP95.toFixed(1)}ms hit ${dense.hitMs.toFixed(3)}ms`,
  });
});
