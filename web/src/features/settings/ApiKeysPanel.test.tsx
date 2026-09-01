/**
 * Minting a `gate` key from the UI (GRPH-580).
 *
 * The scope shipped in GRPH-541, completion was made to depend on it in GRPH-543, and a CI
 * adapter was built for it in GRPH-551 — and until this change the only way to create one was
 * curl with a JWT. The item's framing is the point: a capability nobody can create is a
 * capability nobody uses, and the completion gate then runs permanently under the weak path,
 * where a reviewer's `sign_off` is the only thing that can ever attest. That looks identical
 * to a working gate.
 *
 * **Two of the three claims here fail silently, which is why they are tested rather than
 * eyeballed.** A picker button is visible in a screenshot; these are not:
 *
 * - the scopes that reach the API. `["gate"]` alone mints a key the server accepts — `gate` is
 *   in `API_KEY_SCOPES` — and which then 403s on the first real attestation, because
 *   `attest_ci.py` attests via `update_item` and `mcp_server` refuses a mutating tool without
 *   `write`. The key would be created, stored as a CI secret, and fail once, in CI, months
 *   later. `fleet.mint` carries read+write+gate for this exact reason.
 * - what the operator is told to DO with it. Routing a gate key to `McpInstall` — the old
 *   behaviour for anything non-sync — hands a completion-attesting credential to the agent
 *   doing the work, which is the arrangement the scope exists to prevent.
 *
 * The third (a gate key listed as an agent key) is merely visible-but-wrong: the old
 * partition was `sync` vs everything-else, so the scope that matters would not be shown.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ApiKeysPanel } from "@/features/settings/SettingsView";
import { ProjectProvider } from "@/features/ProjectContext";
import { settingsPath } from "@/lib/routes";
import type { ApiKey, Project } from "@/lib/types";

const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "key_a", name: "a key", prefix: "gb_abc", scopes: ["read", "write"],
  project_id: "core", last_used: null, created_at: "2026-01-01T00:00:00Z",
  expires_at: null, revoked: false, ...over,
});

const proj = (id: string): Project => ({
  id, tag: id.toUpperCase().slice(0, 4), name: id, accent: "#c6f24e",
  visibility: "private", description: "", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
  credential_id: null, model_override: "",
});

let keyList: ApiKey[] = [];

// Declared WITH parameters so `mock.calls[0][3]` is typed — a no-arg `vi.fn` gives an empty
// tuple, and indexing it passes the test while failing the typecheck.
const createApiKey = vi.fn(
  async (name: string, _projectId: string | null, _days?: number | null, _scopes?: string[],
         _tiers?: string[]) => ({
    ...key({ id: "key_new", name }), plaintext: "gb_live_plaintext",
  }),
);

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [proj("core")]),
    // A SNAPSHOT, not the shared array — returning the live one puts it into react-query's
    // cache, where mutating it in place means the cache can never be stale.
    apiKeys: vi.fn(async () => [...keyList]),
    createApiKey: (...a: Parameters<typeof createApiKey>) => createApiKey(...a),
    revokeApiKey: vi.fn(async () => undefined),
    syncLinks: vi.fn(async () => []),
  },
}));

function view() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <ApiKeysPanel />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Pick the kind, name it, create it. */
async function mint(kind: string, name = "github-actions") {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: kind }));
  await user.type(screen.getByPlaceholderText(/Key name/), name);
  await user.click(screen.getByRole("button", { name: /Mint gate key|Create key|Mint credential/ }));
}

beforeEach(() => {
  keyList = [];
  createApiKey.mockClear();
});

describe("minting a gate key", () => {
  it("offers the kind at all", async () => {
    view();
    expect(await screen.findByRole("button", { name: "Gate key" })).toBeInTheDocument();
  });

  it("says what the scope is FOR, not just its name", async () => {
    // The item's acceptance. A picker entry reading "Gate key" and nothing else leaves an
    // operator to guess, and the wrong guess — giving it to the building agent — is the one
    // arrangement that defeats the gate while looking correct.
    view();
    const btn = await screen.findByRole("button", { name: "Gate key" });
    const desc = btn.getAttribute("title") ?? "";
    expect(desc).toMatch(/attests|attest/i);
    expect(desc).toMatch(/done/i);
    expect(desc).toMatch(/never for the agent doing the work/i);
  });

  it("sends scopes that can actually attest, not `gate` alone", async () => {
    // THE ONE THAT MATTERS. `["gate"]` is accepted by the schema and dead in practice.
    view();
    await mint("Gate key");

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    const scopes = createApiKey.mock.calls[0][3];
    expect(scopes).toContain("gate");
    expect(scopes).toContain("write"); // else `update_item` 403s at attest time
  });

  it("pins the key to a project — a global gate key attests everywhere", async () => {
    // THE BOUNCE. `key_gate_ids` falls back to every writable project when
    // project_id is null, so a leaked CI secret would complete work across all of
    // them. Sync already refused to be global; the UI still offered the checkbox
    // for gate and never asserted createApiKey's projectId.
    view();
    await mint("Gate key");

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    const projectId = createApiKey.mock.calls[0][1];
    expect(projectId).toBe("core");
    expect(screen.queryByText(/make it global/i)).toBeNull();
    expect(screen.getByText(/exactly one project/i)).toBeInTheDocument();
  });

  it("tells the operator to store it as a CI secret, not in an MCP config", async () => {
    view();
    await mint("Gate key");

    expect(await screen.findByText(/GRAPHBAN_GATE_KEY/)).toBeInTheDocument();
    // The MCP install block names clients to paste a stanza into. Its absence is the claim:
    // a gate key in an agent's MCP config is the failure this scope exists to prevent.
    expect(screen.queryByRole("button", { name: /Claude Code|Cursor/ })).toBeNull();
  });

  it("still mints an ordinary agent key with no scopes override", async () => {
    // The regression guard. `undefined` means the backend default `["read","write"]`; sending
    // `["read","write","gate"]` to every key would hand the gate to every agent, which is the
    // same defect as the MCP-config one and easier to introduce.
    view();
    await mint("Agent key", "ci-agent");

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][3]).toBeUndefined();
  });
});

describe("tool tiers (GRPH-571)", () => {
  it("mints with no tiers by default", async () => {
    // The default IS core-only, and it is not a degraded state. If the picker defaulted to
    // everything selected, tiering would be a no-op for every key made in the UI — which is
    // the same shape as the backend bug where `None` tiers meant "all tiers".
    view();
    await mint("Agent key", "plain");

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][4]).toEqual([]);
  });

  it("sends the tiers that were picked", async () => {
    const user = userEvent.setup();
    view();
    await user.click(await screen.findByRole("button", { name: "Agent key" }));
    await user.click(screen.getByRole("button", { name: "PRDs" }));
    await user.click(screen.getByRole("button", { name: "Fleet admin" }));
    await user.type(screen.getByPlaceholderText(/Key name/), "planner");
    await user.click(screen.getByRole("button", { name: /Create key/ }));

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][4]).toEqual(["prd", "fleet"]);
  });

  it("toggles a tier off again", async () => {
    // A picker that only ever adds looks identical to a working one until someone changes
    // their mind.
    const user = userEvent.setup();
    view();
    await user.click(await screen.findByRole("button", { name: "Agent key" }));
    await user.click(screen.getByRole("button", { name: "PRDs" }));
    await user.click(screen.getByRole("button", { name: "PRDs" }));
    await user.type(screen.getByPlaceholderText(/Key name/), "k");
    await user.click(screen.getByRole("button", { name: /Create key/ }));

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][4]).toEqual([]);
  });

  it("says a tiered-out tool is still callable", async () => {
    // The one thing an operator must not misread. Reading a tier as a permission would mean
    // granting all four to every key "to be safe", which is tiering switched off.
    view();
    await screen.findByRole("button", { name: "Agent key" });
    expect(screen.getByText(/still/)).toBeInTheDocument();
    expect(screen.getByText(/callable/)).toBeInTheDocument();
  });

  it("does not offer tiers for a sync credential", async () => {
    // A sync credential calls no MCP tools at all, so a tier on it is a control that does
    // nothing — and a control that does nothing teaches the wrong model of what tiers are.
    const user = userEvent.setup();
    view();
    await user.click(await screen.findByRole("button", { name: "Sync credential" }));

    expect(screen.queryByRole("button", { name: "PRDs" })).toBeNull();
  });
});

describe("listing", () => {
  it("shows a gate key under its own heading rather than as an agent key", async () => {
    keyList = [key({ id: "key_g", name: "github-actions", scopes: ["read", "write", "gate"] })];
    view();

    // Wait for the KEY, not for the heading. The headings render before the query resolves,
    // so `findByText("Gate keys")` returns while both lists are still empty — the first
    // version of this test asserted the key was absent from the agent list at a moment when
    // it was absent from every list, and survived the mutation that puts it in the wrong one.
    const rows = await screen.findAllByText("github-actions");
    expect(rows).toHaveLength(1); // in exactly one group, not rendered under both
    expect(screen.getByText("Gate keys").parentElement!).toContainElement(rows[0]);
  });

  it("says what the absence of a gate key COSTS", async () => {
    // An empty state reading "No gate keys" is true and useless. The reason the list is empty
    // is the reason the gate is running on its weak path.
    view();
    expect(await screen.findByText(/nothing can attest/i)).toBeInTheDocument();
  });
});

describe("docs overlay", () => {
  it("matches api-keys before the /settings catch-all", () => {
    // THE CALL. /settings/project/api-keys starts with /settings, so without this
    // branch the overlay is AI Providers. The page is keys, MCP rights, and
    // advertisement — not LLM credentials.
    const keys = docFor(settingsPath("project/api-keys"));
    expect(keys.title).toBe("API keys");
    expect(keys.badge).toBe("API KEYS");
    expect(keys.sections[0].h).not.toMatch(/AI Providers/);
    const body = keys.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/advertis/i);
    expect(body).toMatch(/callable/i);
    expect(body).toMatch(/gate/i);
    expect(body).toMatch(/scope/i);
    expect(docFor(settingsPath("project/providers")).title).toBe("AI providers");
    expect(docFor(settingsPath("deployment/providers")).title).toBe("AI providers");
    expect(docFor("/settings").title).toBe("Settings");
  });
});
