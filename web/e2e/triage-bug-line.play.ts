import { expect, test, type Page } from "@playwright/test";

import { COMPILE } from "./compile-wait";

/**
 * GRPH-1700: the bug pane on the real Triage page, in a real browser (harness:
 * `e2e/triage-bug-line/`). Each state is asserted, then the page is captured to the test's
 * output dir; the copies committed for the PR are in `docs/design/grph-1700/`.
 */
const HARNESS = "http://127.0.0.1:5198/e2e/triage-bug-line/";

// The first navigation pays for the Vite compile (GRPH-1812). The cap has to outlast COMPILE.
test.describe.configure({ timeout: 180_000 });

async function open(page: Page, state: "line" | "none") {
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto(`${HARNESS}?state=${state}`);
  // The bug heading is the landmark. A harness that never paints it fails here.
  await expect(page.getByRole("heading", { name: "Export to CSV drops the last row" })).toBeVisible(COMPILE);
}

const shot = (page: Page, name: string) =>
  page.screenshot({ path: test.info().outputPath(`${name}.png`) });

test("bug pane: line exists — accept is offered, with no PRD", async ({ page }) => {
  await open(page, "line");
  await expect(page.getByRole("button", { name: /Accept to Backlog/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Create the maintenance line/ })).toHaveCount(0);
  await expect(page.getByText(/will link on accept/)).toHaveCount(0);
  await shot(page, "1-line-exists");
});

test("bug pane: no line — Create the maintenance line replaces accept", async ({ page }) => {
  await open(page, "none");
  await expect(page.getByRole("button", { name: /Create the maintenance line/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Accept to/ })).toHaveCount(0);
  await shot(page, "2-no-line");
  await page.getByRole("button", { name: /Create the maintenance line/ }).click();
  await expect(page.getByTestId("triage-notice")).toContainText("still new");
  await expect(page.getByRole("button", { name: /Accept to Backlog/ })).toBeVisible();
  await shot(page, "2b-line-created");
});

test("bug pane: accepted and unready — the item is on the line and not yet claimable", async ({ page }) => {
  await open(page, "line");
  await page.getByRole("button", { name: /Accept to Backlog/ }).click();
  await expect(page.getByText(/Accepted → Backlog as CP-412 on Maintenance line/)).toBeVisible();
  await expect(page.getByText(/needs an acceptance clause and a touchpoint before a drain can claim it/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Open CP-412" })).toBeVisible();
  await shot(page, "3-accepted-unready");
});
