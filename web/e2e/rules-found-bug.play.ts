import { expect, test, type Page } from "@playwright/test";

/**
 * GRPH-1703: `build.found_bug` on the real Rules page, in a real browser (harness:
 * `e2e/rules-found-bug/`). Each state is asserted, then the inspector is captured to the
 * test's output dir; the copies committed for the PR are in `docs/design/grph-1703/`.
 */
const HARNESS = "http://127.0.0.1:5198/e2e/rules-found-bug/";

async function open(page: Page, state: "on" | "off" | "absent") {
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.goto(`${HARNESS}?state=${state}`);
  return page.getByTestId("rules-inspector");
}

const shot = (page: Page, name: string) =>
  page.getByTestId("rules-inspector").screenshot({ path: test.info().outputPath(`${name}.png`) });

test("found-bug: on — the effective mode is shown", async ({ page }) => {
  const ins = await open(page, "on");
  await expect(ins.getByTestId("rules-found-bug-effective")).toHaveText("Effective mode: File a ticket.");
  await shot(page, "1-on");
});

test("found-bug: off — the default fix_in_build is in force", async ({ page }) => {
  const ins = await open(page, "off");
  await expect(ins.getByTestId("rules-found-bug-effective")).toHaveText("Off: the default, fix_in_build, is in force.");
  await shot(page, "2-off");
});

test("found-bug: absent from the pack — the default is in force, the rule is not hidden", async ({ page }) => {
  const ins = await open(page, "absent");
  await expect(ins.getByTestId("rules-found-bug-absent")).toContainText("the default, fix_in_build, is in force");
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
    await expect(page.getByTestId("rules-dock-form")).toBeVisible();
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
