import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Layout harness only: real components and CSS, no app shell, no API. The page is
 * `/e2e/layout/`. Root stays `web/` because Tailwind v4 scans for class names from the
 * Vite root — rooted at `e2e/layout` it never sees `src/` and the panel renders unstyled.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  root: fileURLToPath(new URL("..", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { host: "127.0.0.1", port: 5198, strictPort: true },
});
