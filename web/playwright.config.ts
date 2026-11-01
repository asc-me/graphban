import { defineConfig } from "@playwright/test";

/**
 * Canvas perf bench and real-browser layout checks. Kept out of `pnpm test` (vitest) by the `.play.ts`
 * suffix. Playwright 1.x `webServer.url` waits until the page accepts
 * connections (https://playwright.dev/docs/test-webserver).
 *
 * The canvas bench times frames, so it runs as its own project on one worker
 * (`TestProject.workers`, Playwright 1.52+) and only after the layout project,
 * never beside it on a shared CI runner. The `list` reporter prints each test's
 * stdout, which is where the bench logs its measured numbers.
 */
export default defineConfig({
  testMatch: "e2e/**/*.play.ts",
  timeout: 120_000,
  reporter: "list",
  projects: [
    { name: "layout", testIgnore: "e2e/canvas-bench.play.ts" },
    {
      name: "canvas-bench",
      testMatch: "e2e/canvas-bench.play.ts",
      workers: 1,
      dependencies: ["layout"],
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:5199",
    viewport: { width: 1200, height: 800 },
  },
  webServer: [
    {
      command: "pnpm exec vite --config e2e/vite.config.ts --port 5199 --strictPort",
      url: "http://127.0.0.1:5199",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "pnpm exec vite --config e2e/layout.vite.config.ts --port 5198 --strictPort",
      url: "http://127.0.0.1:5198",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
