import { expect, test } from "@playwright/test";

interface BenchResult {
  paintMs: number;
  panP95: number;
  hitMs: number;
  nodes: number;
  edges: number;
}

type BenchFn = (name: "sparse" | "dense") => BenchResult;

/** Prints the numbers before the budget checks, so a failing run shows its margin too. */
function report(label: string, r: BenchResult): void {
  const line = `${label}: paint ${r.paintMs.toFixed(1)}ms pan p95 ${r.panP95.toFixed(1)}ms hit ${r.hitMs.toFixed(3)}ms nodes ${r.nodes} edges ${r.edges}`;
  console.log(`[bench] ${line}`);
  test.info().annotations.push({ type: "bench", description: line });
}

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
  report("canvas2d sparse", sparse);
  expect(sparse.nodes).toBeGreaterThan(800);
  expect(sparse.paintMs).toBeLessThanOrEqual(1000);
  expect(sparse.panP95).toBeLessThanOrEqual(16);
  expect(sparse.hitMs).toBeLessThanOrEqual(1);

  const dense = await page.evaluate(() =>
    (window as unknown as { __bench: BenchFn }).__bench("dense"),
  );
  report("canvas2d dense", dense);
  expect(dense.nodes).toBeGreaterThan(800);
  expect(dense.paintMs).toBeGreaterThan(0);
});

/**
 * The unit suite's canvas mock never has a real bitmap, and the bench above
 * passes dpr 1. This is the 2× case: the node has to land on the device pixel
 * that the CSS hull would name, not in the top-left quarter.
 */
test("2× bitmap draws a node on its CSS pixel", async ({ page }) => {
  await page.goto("/");
  await page.waitForFunction(
    () => typeof (window as unknown as { __dprProbe?: () => { correct: number; quarter: number; bitmap: number } }).__dprProbe === "function",
  );
  const probe = await page.evaluate(() =>
    (window as unknown as { __dprProbe: () => { correct: number; quarter: number; bitmap: number } }).__dprProbe(),
  );
  expect(probe.bitmap).toBe(800);
  expect(probe.correct).toBeGreaterThan(200);
  expect(probe.quarter).toBe(0);
});

/**
 * S10 budgets. Hit-test is the layout grid. Pan/zoom is the WebGL rasteriser:
 * `dense` is measured at 2.6×, which is individual edges under the cap.
 */
test("webgl paints both fixtures inside the frame budget", async ({ page }) => {
  await page.goto("/");
  await page.waitForFunction(
    () => typeof (window as unknown as { __bench?: BenchFn }).__bench === "function",
  );
  const sparse = await page.evaluate(() =>
    (window as unknown as { __bench: (name: "sparse" | "dense", opts?: { renderer?: string }) => BenchResult }).__bench(
      "sparse",
      { renderer: "webgl" },
    ),
  );
  report("webgl sparse", sparse);
  expect(sparse.nodes).toBeGreaterThan(800);
  expect(sparse.paintMs).toBeLessThanOrEqual(1000);
  expect(sparse.panP95).toBeLessThanOrEqual(16);
  expect(sparse.hitMs).toBeLessThanOrEqual(1);

  const dense = await page.evaluate(() =>
    (
      window as unknown as {
        __bench: (name: "sparse" | "dense", opts?: { renderer?: string; zoom?: number }) => BenchResult;
      }
    ).__bench("dense", { renderer: "webgl", zoom: 2.6 }),
  );
  report("webgl dense 2.6x", dense);
  expect(dense.nodes).toBeGreaterThan(800);
  expect(dense.edges).toBeGreaterThan(100);
  expect(dense.paintMs).toBeLessThanOrEqual(1000);
  expect(dense.panP95).toBeLessThanOrEqual(16);
  expect(dense.hitMs).toBeLessThanOrEqual(1);
});

test("sparse pixel diff stays close between canvas 2d and webgl", async ({ page }) => {
  await page.goto("/");
  await page.waitForFunction(
    () => typeof (window as unknown as { __pixelDiff?: () => { blockMae: number; centerN: number; centerHit: number; ink2d: number; inkGl: number; cloudMae: number } }).__pixelDiff === "function",
  );
  const diff = await page.evaluate(() =>
    (window as unknown as { __pixelDiff: () => { blockMae: number; centerN: number; centerHit: number; ink2d: number; inkGl: number; zoom: number; cloudMae: number; cloudInk2d: number; cloudInkGl: number } }).__pixelDiff(),
  );
  expect(diff.centerN).toBeGreaterThan(20);
  expect(diff.centerHit / diff.centerN).toBeGreaterThanOrEqual(0.75);
  expect(diff.blockMae).toBeLessThanOrEqual(28);
  expect(diff.ink2d).toBeGreaterThan(100);
  expect(diff.inkGl).toBeGreaterThan(100);
  const ratio = diff.inkGl / diff.ink2d;
  expect(ratio).toBeGreaterThan(0.45);
  expect(ratio).toBeLessThan(2.2);
  expect(diff.cloudMae).toBeLessThanOrEqual(28);
  // A soft falloff crosses alpha>20 at different radii on the two rasters, so
  // the ink ratio is not the agreement check. Ink only proves each backend
  // painted the cloud; blockMae is what says the pictures match.
  expect(diff.cloudInk2d).toBeGreaterThan(100);
  expect(diff.cloudInkGl).toBeGreaterThan(100);
  test.info().annotations.push({
    type: "pixel",
    description: `mae ${diff.blockMae.toFixed(2)} centres ${diff.centerHit}/${diff.centerN} ink ${diff.ink2d}/${diff.inkGl} zoom ${diff.zoom.toFixed(2)} cloud mae ${diff.cloudMae.toFixed(2)} ink ${diff.cloudInk2d}/${diff.cloudInkGl}`,
  });
});
