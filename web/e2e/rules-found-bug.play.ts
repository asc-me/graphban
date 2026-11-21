import { expect, test, type Page } from "@playwright/test";

import { COMPILE } from "./compile-wait";

/**
 * GRPH-1703: `build.found_bug` on the real Rules page, in a real browser (harness:
 * `e2e/rules-found-bug/`). Each state is asserted, then the inspector is captured to the
 * test's output dir; the copies committed for the PR are in `docs/design/grph-1703/`.
 */
const HARNESS = "http://127.0.0.1:5198/e2e/rules-found-bug/";

// The first navigation pays for the Vite compile (GRPH-1812). The cap has to outlast COMPILE.
test.describe.configure({ timeout: 180_000 });

async function open(page: Page, state: "on" | "off" | "absent") {
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.goto(`${HARNESS}?state=${state}`);
  const ins = page.getByTestId("rules-inspector");
  // The inspector is the landmark. A harness that never paints it fails here.
  await expect(ins).toBeVisible(COMPILE);
  return ins;
}

const shot = (page: Page, name: string) =>
  page.getByTestId("rules-inspector").screenshot({ path: test.info().outputPath(`${name}.png`) });

/**
 * GRPH-1816. The strip camera eases `transform` for 220ms, and ResizeObserver
 * starts that ease only after `data-map-pct` is already set. A 300ms sleep from
 * the attribute reads the in-flight box: around 220ms the glow is still ~2px
 * past the map (268.4 vs 266.75) under six workers, and the same check passes
 * alone once the ease has finished. The settled box is inside by ~4px.
 *
 * Wait until the painted transform matches the attribute. The glow assertion
 * below runs once, after that — this is not a retry of it. Five seconds is the
 * cap on that real ease plus the refit, not a budget the assertion spends.
 */
async function cameraSettled(page: Page, fitsBefore: string | null) {
  const result = await page.evaluate(async (fitsBefore) => {
    const canvas = document.querySelector<HTMLElement>('[data-testid="rules-canvas"]');
    const area = document.querySelector<HTMLElement>('[data-testid="rules-map-area"]');
    const svg = canvas?.querySelector("svg");
    const g = svg?.querySelector("g");
    if (!canvas || !area || !svg || !g) return "missing";
    const start = performance.now();
    let sizeWasWrong = false;
    let lastH = -1;
    let frames = 0;
    return await new Promise<string>((resolve) => {
      const tick = () => {
        frames += 1;
        const h = area.clientHeight;
        if (lastH !== -1 && h !== lastH) sizeWasWrong = true;
        lastH = h;
        const vb = svg.viewBox.baseVal;
        const sizeOk = Math.abs(vb.height - h) <= 1 && Math.abs(vb.width - area.clientWidth) <= 1;
        if (!sizeOk) sizeWasWrong = true;
        // The viewBox updates a frame before the refit commits its transform.
        // Accepting that frame measures the previous camera. A height that never
        // changed has no refit, so the pose it already has is the settled one.
        const refitOk = !sizeWasWrong || canvas.getAttribute("data-fits") !== fitsBefore;
        const running = g.getAnimations().some((a) => a.playState === "running");
        const attr = g.getAttribute("transform") ?? "";
        const parsed = /translate\(\s*([-\d.eE+]+)[,\s]+([-\d.eE+]+)\s*\)\s*scale\(\s*([-\d.eE+]+)\s*\)/.exec(attr);
        const ctm = g.getCTM();
        const poseOk = !!parsed && !!ctm
          && Math.abs(ctm.e - Number(parsed[1])) <= 0.5
          && Math.abs(ctm.f - Number(parsed[2])) <= 0.5
          && Math.abs(ctm.a - Number(parsed[3])) <= 0.002;
        if (frames >= 2 && sizeOk && refitOk && !running && poseOk) {
          resolve("settled");
          return;
        }
        if (performance.now() - start > 5_000) {
          resolve(`unsettled size=${sizeOk} refit=${refitOk} running=${running} pose=${poseOk} fits=${canvas.getAttribute("data-fits")}`);
          return;
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }, fitsBefore);
  expect(result, "camera ease").toBe("settled");
}

test("found-bug: on — the effective mode is shown", async ({ page }) => {
  const ins = await open(page, "on");
  await expect(ins.getByTestId("rules-found-bug-effective")).toHaveText("Effective mode: File a ticket.", COMPILE);
  await shot(page, "1-on");
});

test("found-bug: off — the default fix_in_build is in force", async ({ page }) => {
  const ins = await open(page, "off");
  await expect(ins.getByTestId("rules-found-bug-effective")).toHaveText("Off: the default, fix_in_build, is in force.", COMPILE);
  await shot(page, "2-off");
});

test("found-bug: absent from the pack — the default is in force, the rule is not hidden", async ({ page }) => {
  const ins = await open(page, "absent");
  await expect(ins.getByTestId("rules-found-bug-absent")).toContainText("the default, fix_in_build, is in force", COMPILE);
  await shot(page, "3-absent");
});

test("found-bug: the editor offers exactly the two modes", async ({ page }) => {
  // GRPH-1718: the stage opens as the docked editor, so the rule's form is already open.
  const ins = await open(page, "on");
  const group = ins.getByRole("radiogroup", { name: "What a builder does with a bug it finds" });
  await expect(group.getByRole("radio")).toHaveCount(2);
  await shot(page, "4-editor");
});

test("dock: layout C at 1440×900 and 800 wide — the map strip shows the stage, the columns stack, nothing scrolls sideways", async ({ page }) => {
  // GRPH-1725: the whole page at each width; the copies for the PR are attached as evidence.
  for (const [w, h] of [[1440, 900], [800, 900]] as const) {
    await page.setViewportSize({ width: w, height: h });
    await page.goto(`${HARNESS}?state=on`);
    await expect(page.getByTestId("rules-dock-form")).toBeVisible(COMPILE);
    await expect(page.getByTestId("rules-legend")).toHaveCount(0);
    if (w > 900) {
      // The strip draws the opened stage readable (at least .6 of its 208px card), not at the old 20%.
      const card = page.getByTestId("rules-stage-build");
      await expect(card).toHaveAttribute("data-selected", "true");
      await expect.poll(async () => (await card.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(0.6 * 208);
      await page.waitForTimeout(300); // let the camera's 220ms ease settle before the capture
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.screenshot({ path: test.info().outputPath(`5-dock-${w}.png`) });
  }
});

test("strip: at 1512×792 every stage card's content fits and the selected card's glow is whole, at HALF, TALL and FULL", async ({ page }) => {
  // GRPH-1728: the p74-walk strip, Build carrying the longest real tally. The glow is `0 0 0 3px`
  // plus `0 0 24px`, drawn at the map's zoom, so its reach on screen is 27 × zoom.
  await page.setViewportSize({ width: 1512, height: 792 });
  for (const [label, pct] of [["half", "46"], ["tall", "34"], ["full", "18"]] as const) {
    await page.goto(`${HARNESS}?state=on&flow=p74`);
    const height = page.getByTestId(`rules-dock-height-${label}`);
    await expect(height).toBeVisible(COMPILE);
    const fitsBefore = await page.getByTestId("rules-canvas").getAttribute("data-fits");
    await height.click();
    await expect(page.getByTestId("rules-map-area")).toHaveAttribute("data-map-pct", pct);
    await cameraSettled(page, fitsBefore);
    const m = await page.evaluate(() => {
      const area = document.querySelector('[data-testid="rules-map-area"]')!.getBoundingClientRect();
      const k = Number(document.querySelector('[data-testid="rules-canvas"]')!.getAttribute("data-zoom"));
      const cards = [...document.querySelectorAll<HTMLElement>('[role="option"][data-testid^="rules-stage-"]')].map((c) => ({
        id: c.dataset.testid, scroll: c.scrollHeight, client: c.clientHeight, selected: c.dataset.selected === "true",
        top: c.getBoundingClientRect().top, bottom: c.getBoundingClientRect().bottom,
      }));
      return { top: area.top, bottom: area.bottom, k, cards };
    });
    for (const c of m.cards) expect(c.scroll, `${label}: ${c.id} content overflows its card`).toBeLessThanOrEqual(c.client);
    const sel = m.cards.find((c) => c.selected)!;
    expect(sel.id).toBe("rules-stage-build");
    const glow = 27 * m.k;
    expect(sel.top - glow, `${label}: glow cut at the top`).toBeGreaterThanOrEqual(m.top);
    expect(sel.bottom + glow, `${label}: glow cut at the bottom`).toBeLessThanOrEqual(m.bottom);
    await expect(page.getByTestId("rules-dots")).toHaveAttribute("fill", "url(#rules-dots)");
    await page.screenshot({ path: test.info().outputPath(`6-strip-${label}-1512.png`) });
  }
});
