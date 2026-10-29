import { defineConfig } from "@playwright/test";

/**
 * Canvas perf bench and real-browser layout checks. Kept out of `pnpm test` (vitest) by the `.play.ts`
 * suffix. Playwright 1.x `webServer.url` waits until the page accepts
 * connections (https://playwright.dev/docs/test-webserver).
 */
export default defineConfig({
  testMatch: "e2e/**/*.play.ts",
  timeout: 120_000,
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
