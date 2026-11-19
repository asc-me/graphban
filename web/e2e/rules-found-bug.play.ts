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
