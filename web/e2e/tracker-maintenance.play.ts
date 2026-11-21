import { expect, test, type Page } from "@playwright/test";

import { COMPILE } from "./compile-wait";

/**
 * GRPH-1701: the Maintenance filter on the real tracker page, in a real browser (harness:
 * `e2e/tracker-maintenance/`). Each state is asserted, then captured to the test's output dir;
 * the copies committed for the PR are in `docs/design/grph-1701/`.
 */
const HARNESS = "http://127.0.0.1:5198/e2e/tracker-maintenance/";

// The first navigation pays for the Vite compile (GRPH-1812). The cap has to outlast COMPILE.
test.describe.configure({ timeout: 180_000 });

async function openFiltered(page: Page, line: "none" | "empty" | "populated", layout: "list" | "board" = "list") {
  await page.setViewportSize({ width: 1400, height: 760 });
  await page.goto(`${HARNESS}?line=${line}&layout=${layout}`);
  // "2 unscoped" is the landmark. A harness that never paints it fails here.
  await expect(page.getByTestId("unscoped-count")).toHaveText("2 unscoped", COMPILE);
  await page.getByRole("button", { name: /^Maintenance filter/ }).click();
}

// The chips fade with `transition-colors`: finish it, and move the pointer off the chip, so the shot shows the settled state.
const shot = async (page: Page, name: string) => {
  await page.mouse.move(0, 0);
  await page.screenshot({ path: test.info().outputPath(`${name}.png`), animations: "disabled" });
};

test("maintenance: no line — its own sentence and create", async ({ page }) => {
  await openFiltered(page, "none");
  await expect(page.getByText("This project has no maintenance line.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create the maintenance line" })).toBeVisible();
  await shot(page, "1-no-line");
});

test("maintenance: empty line — a different sentence, unscoped count beside it", async ({ page }) => {
  await openFiltered(page, "empty");
  await expect(page.getByText("The maintenance line has no items.")).toBeVisible();
  await expect(page.getByText("This project has no maintenance line.")).toHaveCount(0);
  await shot(page, "2-empty-line");
});

test("maintenance: populated — line items only, with FIX/SECURITY/needs/blocks", async ({ page }) => {
  await openFiltered(page, "populated");
  await expect(page.getByText("Session token not rotated on password reset")).toBeVisible();
  await expect(page.getByText("Invoice PDF export")).toHaveCount(0);
  await expect(page.getByTestId("badge-security")).toHaveCount(1);
  await expect(page.getByTestId("line-needs")).toHaveText("needs acceptance · needs a file");
  await expect(page.getByTestId("badge-blocks")).toHaveText("blocks CP-41");
  await shot(page, "3-populated-list");
});

test("maintenance: populated board", async ({ page }) => {
  await openFiltered(page, "populated", "board");
  await expect(page.getByTestId("badge-security")).toHaveCount(1);
  await expect(page.getByTestId("line-needs")).toHaveText("needs acceptance · needs a file");
  await shot(page, "4-populated-board");
});

test("blocked by a fix: the PRD item reads BLOCKED by the fix's key", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 760 });
  await page.goto(`${HARNESS}?line=populated&layout=list`);
  await expect(page.getByTestId("badge-blocked-by")).toHaveText("BLOCKED by CP-52", COMPILE);
  await shot(page, "5-blocked-by-fix");
});

test("unscoped: a small list, one action per row (GRPH-1744)", async ({ page }) => {
  await openFiltered(page, "populated");
  await page.getByTestId("unscoped-count").click();
  const pop = page.getByRole("dialog", { name: "Unscoped fixes" });
  await expect(pop).toBeVisible();
  await expect(pop.getByText("Fixes with no PRD and no line. They are not in the Maintenance filter.")).toBeVisible();
  await expect(pop.getByRole("button", { name: "Put on the maintenance line" })).toHaveCount(2);
  await shot(page, "6-unscoped-popover");
});
