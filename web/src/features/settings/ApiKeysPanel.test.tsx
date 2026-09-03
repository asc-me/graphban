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
  await user.type(screen.getByPlaceholderText(/key name/i), name);
  await user.click(screen.getByRole("button", { name: /Mint gate key|Mint agent key|Mint link key/ }));
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
    await mint("Agent key", "claude-code");

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
    await user.type(screen.getByPlaceholderText(/key name/i), "planner");
    await user.click(screen.getByRole("button", { name: /Mint agent key/ }));

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
    await user.type(screen.getByPlaceholderText(/key name/i), "k");
    await user.click(screen.getByRole("button", { name: /Mint agent key/ }));

    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][4]).toEqual([]);
  });

  it("says the Fleet admin tier is not a seat", async () => {
    // Same word, two objects: being in a fleet needs a seat; this tier is for the
    // supervisor or planner key. Reading it as a permission to join a wave would mint
    // the wrong object.
    view();
    const btn = await screen.findByRole("button", { name: "Fleet admin" });
    expect(btn.getAttribute("title") ?? "").toMatch(/seat/i);
  });

  it("says a tiered-out tool is still callable", async () => {
    // The one thing an operator must not misread. Reading a tier as a permission would mean
    // granting all four to every key "to be safe", which is tiering switched off.
    view();
    await screen.findByRole("button", { name: "Agent key" });
    expect(screen.getByText(/still/)).toBeInTheDocument();
    expect(screen.getByText(/callable/)).toBeInTheDocument();
  });

  it("does not offer tiers for a link key", async () => {
    // A link key calls no MCP tools at all, so a tier on it is a control that does
    // nothing — and a control that does nothing teaches the wrong model of what tiers are.
    const user = userEvent.setup();
    view();
    await user.click(await screen.findByRole("button", { name: "Link key" }));

    expect(screen.queryByRole("button", { name: "PRDs" })).toBeNull();
  });

  it("names the kind Link key, the same object Sync / Link mints", async () => {
    // API keys used to say "Sync credential" while Sync / Link said "link key".
    // Two names for one mint is the pairing hole this slice closes.
    view();
    expect(await screen.findByRole("button", { name: "Link key" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync credential" })).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Link key" }));
    expect(screen.getByRole("button", { name: /Mint link key/ })).toBeInTheDocument();
    // Cloud / Sync already names this picker "Link key target project".
    // "Sync target project" was the old name of the same object.
    expect(screen.getByLabelText("Link key target project")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sync target project")).not.toBeInTheDocument();
    // The name field sat next to "Mint link key" still saying "Key name".
    expect(screen.getByPlaceholderText("Link key name (e.g. laptop — acme-core)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Key name (e.g. laptop — acme-core)")).not.toBeInTheDocument();
  });

  it("names the agent mint Mint agent key, like the other two kinds", async () => {
    // Link and gate already name the object on the button. "Mint key" did not.
    view();
    expect(await screen.findByRole("button", { name: "Mint agent key" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Mint key$/ })).not.toBeInTheDocument();
  });

  it("names the agent and gate name fields, like Link key name", async () => {
    // #587 named the link key field. Agent and gate still said "Key name".
    view();
    expect(await screen.findByPlaceholderText("Agent key name (e.g. claude-code)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Key name (e.g. claude-code)")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Gate key" }));
    expect(screen.getByPlaceholderText("Gate key name (e.g. github-actions)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Key name (e.g. github-actions)")).not.toBeInTheDocument();
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

  it("labels a wave-tagged key so End wave sweeping it is visible on this list", async () => {
    keyList = [
      key({ id: "key_h", name: "hand-minted" }),
      key({ id: "key_w", name: "fleet worker", fleet_wave: "wave-1" }),
    ];
    view();
    expect(await screen.findByText(/wave-1 · swept by End wave/)).toBeInTheDocument();
    expect(screen.queryByText(/hand-minted/)).toBeInTheDocument();
    expect(screen.getByText("hand-minted").closest("div")?.textContent ?? "")
      .not.toMatch(/swept by End wave/);
  });

  it("points roles at Fleet, not at minting another key", async () => {
    view();
    const link = await screen.findByRole("link", { name: /seats on Fleet/i });
    expect(link).toHaveAttribute("href", "/fleet");
  });

  it("sends someone looking for seats to Fleet, not a second mint on this page", async () => {
    // The reverse of Fleet's "Looking for MCP?". A seat is not an API key; minting
    // another key here would be the two-surfaces bug wearing the other shirt.
    view();
    expect(await screen.findByRole("link", { name: /looking for seats\?/i }))
      .toHaveAttribute("href", "/fleet");
  });

  it("sends someone looking for LLM credentials to AI Providers, not a mint here", async () => {
    // The reverse of AI Providers' "Looking for API keys?". An Anthropic key is
    // not a Graphban API key; minting one here would be the two-surfaces bug.
    view();
    expect(await screen.findByRole("link", { name: /looking for llm credentials\?/i }))
      .toHaveAttribute("href", "/settings/deployment/providers");
  });

  it("says what the absence of a gate key COSTS", async () => {
    // An empty state reading "No gate keys" is true and useless. The reason the list is empty
    // is the reason the gate is running on its weak path.
    view();
    expect(await screen.findByText(/nothing can attest/i)).toBeInTheDocument();
  });
});

/**
 * The "minted with" detail. The permissions chosen at mint time (scopes + MCP tool tiers)
 * were never shown again after the one-time plaintext banner, which made a key's tool tiers
 * impossible to audit once it was in an agent's config. The detail expand surfaces them.
 */
describe("minted-with detail", () => {
  /** Open the chevron on the row for the key with the given name. */
  async function expand(name: string) {
    const user = userEvent.setup();
    await screen.findByText(name);
    await user.click(screen.getAllByTitle("Minted with")[0]);
  }

  it("is collapsed until the chevron is opened", async () => {
    // The row leads with name/prefix/project; the permissions are a disclosure, not wallpaper.
    keyList = [key({ id: "key_a", name: "claude-code", tool_tiers: ["prd"] })];
    view();
    await screen.findByText("claude-code");
    expect(screen.queryByTestId("minted-with")).toBeNull();
  });

  it("shows the scopes and tier labels an agent key was minted with", async () => {
    keyList = [key({ id: "key_a", name: "planner", scopes: ["read", "write"], tool_tiers: ["prd", "fleet"] })];
    view();
    await expand("planner");
    const block = await screen.findByTestId("minted-with");
    expect(block.textContent).toMatch(/read/);
    expect(block.textContent).toMatch(/write/);
    expect(block.textContent).toMatch(/PRDs/);
    expect(block.textContent).toMatch(/Fleet admin/);
  });

  it("names core explicitly when no extra tiers were granted", async () => {
    // Core-only is the DEFAULT, not a degraded state. An empty tools row would read as "we
    // forgot to say" rather than "this is the default". project_id is null so the Target row
    // ("Global…") cannot collide with the "core" chip being asserted.
    keyList = [key({ id: "key_a", name: "plain", project_id: null, tool_tiers: [] })];
    view();
    await expand("plain");
    const block = await screen.findByTestId("minted-with");
    expect(block.textContent).toMatch(/core/);
    expect(block.textContent).not.toMatch(/PRDs/);
  });

  it("does not advertise MCP tools for a link key — it calls none", async () => {
    // A link key pushes a code graph and never touches the MCP endpoint, so a tools row would
    // describe a capability it does not have. The block itself must still render (the scopes
    // and target are real), which is why the absence is checked ON the block, not the page.
    keyList = [key({ id: "key_l", name: "laptop", scopes: ["sync"] })];
    view();
    await expand("laptop");
    const block = await screen.findByTestId("minted-with");
    expect(block.textContent).not.toMatch(/MCP tools/);
    expect(block.textContent).toMatch(/sync/);
  });

  it("does not advertise MCP tools for a gate key — it attests, it does not call", async () => {
    keyList = [key({ id: "key_g", name: "ci", scopes: ["read", "write", "gate"] })];
    view();
    await expand("ci");
    const block = await screen.findByTestId("minted-with");
    expect(block.textContent).not.toMatch(/MCP tools/);
    expect(block.textContent).toMatch(/gate/);
  });
});

/**
 * The permissions ON the row. `MintedWith` carried scopes and tiers from the day it shipped,
 * and the registry was still reported as not showing which permissions each key has — the
 * only handle was a chevron whose label was a tooltip. What the operator asked ("I minted
 * with everything and see 34 tools") has to be answerable from the row itself.
 */
describe("permissions on the row", () => {
  it("shows scopes, tier labels, and the manifest size without opening anything", async () => {
    keyList = [key({ id: "key_a", name: "planner", scopes: ["read", "write"], tool_tiers: ["prd"], tool_count: 42 })];
    view();
    await screen.findByText("planner");
    expect(screen.queryByTestId("minted-with")).toBeNull();
    const row = screen.getByTestId("key-permissions");
    expect(row.textContent).toMatch(/read/);
    expect(row.textContent).toMatch(/write/);
    expect(row.textContent).toMatch(/PRDs/);
    expect(row.textContent).toMatch(/42 tools/);
  });

  it("shows the count for a core-only key — 34 is the answer, not an absence", async () => {
    keyList = [key({ id: "key_a", name: "plain", tool_tiers: [], tool_count: 34 })];
    view();
    await screen.findByText("plain");
    expect(screen.getByTestId("key-permissions").textContent).toMatch(/34 tools/);
  });

  it("omits the count rather than guessing when the server did not report one", async () => {
    // Summing tiers client-side would be a second copy of TOOL_TIERS. Older server: say nothing.
    keyList = [key({ id: "key_a", name: "old-server", tool_tiers: ["prd"] })];
    view();
    await screen.findByText("old-server");
    expect(screen.getByTestId("key-permissions").textContent).not.toMatch(/tools/);
  });

  it("puts no tools on a link key row — it calls none", async () => {
    keyList = [key({ id: "key_l", name: "laptop", scopes: ["sync"], tool_count: null })];
    view();
    await screen.findByText("laptop");
    const row = screen.getByTestId("key-permissions");
    expect(row.textContent).toMatch(/sync/);
    expect(row.textContent).not.toMatch(/tools/);
  });

  it("shows read, write and gate on a gate key row", async () => {
    keyList = [key({ id: "key_g", name: "ci", scopes: ["read", "write", "gate"], tool_count: 34 })];
    view();
    await screen.findByText("ci");
    const row = screen.getByTestId("key-permissions");
    expect(row.textContent).toMatch(/read/);
    expect(row.textContent).toMatch(/gate/);
    expect(row.textContent).not.toMatch(/tools/);
  });

  it("labels the disclosure — a tooltip is not a label", async () => {
    keyList = [key({ id: "key_a", name: "claude-code" })];
    view();
    await screen.findByText("claude-code");
    expect(screen.getByRole("button", { name: /minted with/i })).toBeTruthy();
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
    expect(body).toMatch(/seat/i);
    expect(keys.related?.some((r) => r.label === "Fleet")).toBe(true);
    expect(keys.related?.some((r) => r.label === "AI providers")).toBe(true);
    expect(docFor(settingsPath("project/providers")).title).toBe("AI providers");
    expect(docFor(settingsPath("deployment/providers")).title).toBe("AI providers");
    expect(docFor("/settings").title).toBe("Settings");
  });
});
