import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";

/** Bench page only. Root is `e2e/bench` so it does not boot the app shell. */
export default defineConfig({
  root: fileURLToPath(new URL("./bench", import.meta.url)),
  resolve: {
    alias: { "@": fileURLToPath(new URL("../src", import.meta.url)) },
  },
  server: { host: "127.0.0.1", port: 5199, strictPort: true },
});
