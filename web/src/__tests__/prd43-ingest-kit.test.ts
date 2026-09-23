import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { DEFAULT_CONFIG, fromParams, toParams } from "@/features/feedback/config";
import { inlineSnippet, launcherSnippet } from "@/features/feedback/snippets";

/**
 * GRPH-904 bounce: the Kit copy is the load-bearing CALL, not mint_ingest_token.
 * Drop `it=` from toParams, or stop passing config.ingestToken into submit, and
 * these fail. Effort 3 requires a sabotage receipt with tests_failed ≥ 1.
 */

const here = dirname(fileURLToPath(import.meta.url));
const webSrc = join(here, "..");

function src(rel: string): string {
  return readFileSync(join(webSrc, rel), "utf8");
}

const TOKEN = "gbfb_test_token_rotate_me";

describe("PRD-43 D1 Kit copy (CALL sabotage)", () => {
  it("puts the ingest token on the embed URL the snippet copies", () => {
    const cfg = { ...DEFAULT_CONFIG, ingestToken: TOKEN };
    const qs = toParams(cfg);
    expect(qs).toContain(`it=${TOKEN}`);
    const embedUrl = `https://cloud.graphban.dev/embed/feedback?${qs}`;
    expect(inlineSnippet(embedUrl)).toContain(`it=${TOKEN}`);
    expect(launcherSnippet(embedUrl, cfg)).toContain(`it=${TOKEN}`);
    // Round-trip: the iframe reads the same token back.
    expect(fromParams(new URLSearchParams(qs)).ingestToken).toBe(TOKEN);
  });

  it("omits it= when no token is pasted — legacy embed URLs stay token-free", () => {
    const qs = toParams({ ...DEFAULT_CONFIG, ingestToken: "" });
    expect(qs).not.toContain("it=");
  });

  it("FeedbackKitView copies via toParams(cfg) and has an ingest-token field", () => {
    const kit = src("features/feedback/FeedbackKitView.tsx");
    expect(kit).toContain("toParams(cfg)");
    expect(kit).toContain('value={cfg.ingestToken}');
    expect(kit).toContain("Ingest token");
  });

  it("widget submit sends the Kit token as Bearer, not a share-token query", () => {
    const widget = src("features/feedback/FeedbackWidget.tsx");
    expect(widget).toContain("config.ingestToken");
    expect(widget).toContain("publicApi.submit");
    const publicApi = src("lib/publicApi.ts");
    expect(publicApi).toContain("Authorization");
    expect(publicApi).toContain("Bearer");
    expect(publicApi).toContain("ingestToken");
  });

  it("Settings mint/rotate is the door the Kit paste comes from", () => {
    const settings = src("features/settings/SettingsView.tsx");
    expect(settings).toContain("useMintIngestToken");
    expect(settings).toContain("mintIngestToken.mutate");
    const api = src("lib/api.ts");
    expect(api).toContain("mintIngestToken");
    expect(api).toContain("/public/ingest-token");
  });
});
