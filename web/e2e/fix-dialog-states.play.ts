import { expect, test, type Page } from "@playwright/test";

/**
 * GRPH-1699: each Fix Dialog state, in a real browser against the real dialog and CSS
 * (harness: `e2e/fix-dialog/`). Every state is asserted, then captured to the test's
 * output dir; the copies committed for the PR are in `docs/design/grph-1699/`.
 */
const HARNESS = "http://127.0.0.1:5198/e2e/fix-dialog/";

async function openFix(page: Page, lineExists: boolean) {
  await page.setViewportSize({ width: 900, height: 1000 });
  await page.goto(HARNESS + (lineExists ? "?line=1" : ""));
  await page.getByRole("button", { name: /New item/ }).click();
  await page.getByPlaceholder("Title").fill("Webhook retries drop the signature header");
  await page.getByLabel("Kind").selectOption("fix");
}

const shot = (page: Page, name: string) =>
  page.getByRole("dialog").screenshot({ path: test.info().outputPath(`${name}.png`) });

test("fix dialog: blank — two scopes, nothing chosen, cannot submit", async ({ page }) => {
  await openFix(page, true);
  await expect(page.getByRole("radio")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "Create item" })).toBeDisabled();
  await shot(page, "1-blank");
});

test("fix dialog: on the line", async ({ page }) => {
  await openFix(page, true);
  await page.getByLabel("On the maintenance line").check();
  await expect(page.getByText(/Filed on Maintenance/)).toBeVisible();
  await page.getByLabel("Acceptance").fill("Retried webhooks carry the original signature header\nA test replays a retry and verifies it");
  await page.getByLabel("Touchpoints").fill("backend/app/webhooks/*");
  await expect(page.getByText(/will not be claimable/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create item" })).toBeEnabled();
  await shot(page, "2-line");
});

test("fix dialog: blocking a PRD", async ({ page }) => {
  await openFix(page, true);
  await page.getByLabel("Blocking a PRD").check();
  await page.getByLabel("PRD", { exact: true }).selectOption("CP-P4");
  await expect(page.getByRole("button", { name: "Create item" })).toBeDisabled();
  await page.getByLabel("Blocked item").selectOption("CP-42");
  await page.getByLabel(/Severity: Security/).check();
  await page.getByLabel("Acceptance").fill("Proration survives a retried webhook");
  await page.getByLabel("Touchpoints").fill("backend/app/billing/*");
  await expect(page.getByRole("button", { name: "Create item" })).toBeEnabled();
  await shot(page, "3-blocking");
});

test("fix dialog: no line — create it in place, then file on it", async ({ page }) => {
  await openFix(page, false);
  await page.getByLabel("On the maintenance line").check();
  const make = page.getByRole("button", { name: "Create the maintenance line" });
  await expect(make).toBeVisible();
  await expect(page.getByRole("button", { name: "Create item" })).toBeDisabled();
  await shot(page, "4-no-line");
  await make.click();
  await expect(page.getByText(/Filed on Maintenance/)).toBeVisible();
  await shot(page, "4b-no-line-created");
});

test("fix dialog: line, unready — then the server refusal, verbatim, dialog open", async ({ page }) => {
  await openFix(page, true);
  await page.getByLabel("On the maintenance line").check();
  await expect(page.getByText(/will not be claimable; it lands as unready/)).toBeVisible();
  await shot(page, "5-line-unready");
  await page.getByRole("button", { name: "Create item" }).click();
  await expect(page.getByRole("alert")).toHaveText(/^a fix filed on the line needs acceptance clauses/);
  await expect(page.getByRole("dialog")).toBeVisible();
  await shot(page, "6-refusal");
});
