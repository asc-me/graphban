/**
 * Settings → API keys (GRPH-580 gate keys, GRPH-1310 the 2026-11-04 redraw).
 *
 * **Several claims here fail silently, which is why they are tested rather than eyeballed:**
 *
 * - the scopes that reach the API. `["gate"]` alone mints a key the server accepts and which
 *   then 403s on the first real attestation, because `attest_ci.py` attests via `update_item`
 *   and `mcp_server` refuses a mutating tool without `write`.
 * - what the operator is told to DO with a gate key. Routing it to the MCP install hands a
 *   completion-attesting credential to the agent doing the work.
 * - which row action a row gets. Rotating a `gban setup` key mints a spare live key; rotating a
 *   link key forges a new deployment identity. Only the rotate route may replace a key, and
 *   never as a create plus a DELETE from the browser.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ApiKeysPanel } from "@/features/settings/ApiKeysPanel";
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
let listFails: string | null = null;

const revokeApiKey = vi.fn(async (_id: string) => undefined);
const rotateApiKey = vi.fn(async (_id: string, _days: number | null) => ({
  ...key({ id: "key_rot", name: "rotated", prefix: "gb_new" }), plaintext: "gb_sk_rotated_plain",
}));
// Declared WITH parameters so `mock.calls[0][3]` is typed.
const createApiKey = vi.fn(
  async (name: string, _projectId: string | null, _days?: number | null, _scopes?: string[],
         _tiers?: string[], _origin?: string) => ({
    ...key({ id: "key_new", name }), plaintext: "gb_live_plaintext",
  }),
);
const listKeys = vi.fn(async () => {
  if (listFails) throw new Error(JSON.stringify({ detail: listFails }));
  return [...keyList];
});

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [proj("core"), proj("other")]),
    // A SNAPSHOT, not the shared array — the live one in react-query's cache could never be stale.
    apiKeys: () => listKeys(),
    createApiKey: (...a: Parameters<typeof createApiKey>) => createApiKey(...a),
    rotateApiKey: (...a: Parameters<typeof rotateApiKey>) => rotateApiKey(...a),
    revokeApiKey: (...a: Parameters<typeof revokeApiKey>) => revokeApiKey(...a),
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

/** Open the Keys tab. */
async function keysTab() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: /Keys/ }));
  return user;
}

/** Open a kind's form on Set up, pick its project where one is needed, name it, mint. */
async function mint(kind: "Agent key" | "Link key" | "Gate key", name = "github-actions", project = "core") {
  const user = userEvent.setup();
  const mintBtn = { "Agent key": "Mint agent key", "Link key": "Mint link key", "Gate key": "Mint gate key" }[kind];
  // The card's toggle and the form's submit share a label; the toggle is the aria-expanded one.
  await user.click(await screen.findByRole("button", { name: mintBtn, expanded: false }));
  await user.type(screen.getByLabelText(new RegExp(`${kind} name`, "i")), name);
  const pin = screen.queryByRole("combobox", { name: /target project|Pinned project/i });
  if (pin && project) await user.selectOptions(pin, project);
  const submit = screen.getAllByRole("button", { name: mintBtn }).find((b) => !b.hasAttribute("aria-expanded"))!;
  await user.click(submit);
  return user;
}

beforeEach(() => {
  keyList = [];
  listFails = null;
  createApiKey.mockClear();
  rotateApiKey.mockClear();
  revokeApiKey.mockReset();
  revokeApiKey.mockResolvedValue(undefined);
});

describe("tabs", () => {
  it("opens on Set up with the gban block and three collapsed mint cards", async () => {
    view();
    expect(await screen.findByRole("tab", { name: "Set up", selected: true })).toBeInTheDocument();
    expect(screen.getByText("gban login")).toBeInTheDocument();
    expect(screen.getByText("gban setup --auto")).toBeInTheDocument();
    expect(screen.getByText(/Not this page either/i)).toBeInTheDocument();
    for (const n of ["Mint agent key", "Mint link key", "Mint gate key"]) {
      expect(screen.getByRole("button", { name: n })).toHaveAttribute("aria-expanded", "false");
    }
    expect(screen.getByText("INTEGRATION")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /Integration docs/ })[0])
      .toHaveAttribute("href", "https://github.com/asc-me/graphban/tree/main/docs");
  });

  it("counts only keys not marked revoked on the Keys tab", async () => {
    keyList = [key({ id: "a", name: "live" }), key({ id: "b", name: "dead", revoked: true })];
    view();
    await waitFor(() => expect(screen.getByTestId("keys-count")).toHaveTextContent("1"));
  });

  it("keeps the two header links where they went", async () => {
    view();
    expect(await screen.findByRole("link", { name: "Looking for seats?" })).toHaveAttribute("href", "/fleet");
    expect(screen.getByRole("link", { name: "Looking for LLM credentials?" }))
      .toHaveAttribute("href", settingsPath("deployment/providers"));
  });
});

describe("visual pass (design audit 2026-11-04)", () => {
  it("underlines the selected tab with the accent and no other", async () => {
    view();
    const setup = await screen.findByRole("tab", { name: "Set up" });
    const keysT = screen.getByRole("tab", { name: /Keys/ });
    expect(setup.className).toMatch(/border-accent/);
    expect(keysT.className).not.toMatch(/border-accent/);
  });

  it("drops the subtitle line", async () => {
    view();
    await screen.findByRole("tab", { name: "Set up" });
    expect(screen.queryByText(/An API key is who the process is/)).toBeNull();
  });

  it("holds the gban title and the notes in one card", async () => {
    view();
    const title = await screen.findByText(/Set up a project with/);
    const notes = screen.getByText(/Not this page either/i);
    expect(title.closest("[data-testid='gban-card']")).not.toBeNull();
    expect(title.closest("[data-testid='gban-card']")).toBe(notes.closest("[data-testid='gban-card']"));
  });

  it("draws each closed Mint key button in the default lime variant", async () => {
    view();
    await screen.findByRole("tab", { name: "Set up" });
    for (const n of ["Mint agent key", "Mint link key", "Mint gate key"]) {
      expect(screen.getByRole("button", { name: n }).className).toContain("bg-accent");
    }
  });

  it("labels the agent form in sentence case, not uppercase mono", async () => {
    view();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mint agent key" }));
    for (const t of ["Name", "Scope", "Key expiry"]) {
      expect(screen.getByText(t).className).not.toMatch(/uppercase/);
    }
  });

  it("writes a group heading in sentence case and a row expiry as a short date", async () => {
    keyList = [key({ id: "x", name: "soon", expires_at: "2099-11-10T12:00:00Z" })];
    view();
    await keysTab();
    const h = await screen.findByRole("heading", { name: "Agent keys" });
    expect(h.className).not.toMatch(/uppercase/);
    expect(await screen.findByText(/expires Nov 10, 2099/i)).toBeInTheDocument();
  });
});

describe("minting", () => {
  it("posts origin web and no scopes override for an agent key", async () => {
    view();
    await mint("Agent key", "claude-code");
    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    const call = createApiKey.mock.calls[0];
    expect(call[3]).toBeUndefined(); // the backend default; never hand the gate to every agent
    expect(call[5]).toBe("web");
    expect(call[1]).toBe("core");
    expect(await screen.findByText(/Copy now — shown once/i)).toBeInTheDocument();
    expect(screen.getByText("gb_live_plaintext")).toBeInTheDocument();
    expect(screen.getByText(/Connect an agent/i)).toBeInTheDocument();
  });

  it("refuses a pinned agent mint without a project, in the form", async () => {
    view();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mint agent key", expanded: false }));
    await user.type(screen.getByLabelText(/Agent key name/i), "x");
    await user.click(screen.getAllByRole("button", { name: "Mint agent key" }).find((b) => !b.hasAttribute("aria-expanded"))!);
    expect(await screen.findByText(/Pick a project/i)).toBeInTheDocument();
    expect(createApiKey).not.toHaveBeenCalled();
  });

  it("a global agent key shows the AGENTS.md snippet and the fleet-drain warning", async () => {
    view();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mint agent key", expanded: false }));
    await user.click(screen.getByRole("button", { name: "Global" }));
    expect(screen.getByText(/can't supervise a fleet drain — the server refuses its records/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Agent key name/i), "g");
    await user.click(screen.getAllByRole("button", { name: "Mint agent key" }).find((b) => !b.hasAttribute("aria-expanded"))!);
    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][1]).toBeNull();
    expect(await screen.findByLabelText("Copy AGENTS.md project")).toBeInTheDocument();
  });

  it("sends tiers only for an agent key, and toggles them off again", async () => {
    view();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mint agent key", expanded: false }));
    const fleet = screen.getByRole("button", { name: /Fleet admin/ });
    await user.click(fleet);
    await user.click(fleet);
    await user.click(screen.getByRole("button", { name: /PRDs/ }));
    await user.type(screen.getByLabelText(/Agent key name/i), "t");
    await user.selectOptions(screen.getByRole("combobox", { name: "Pinned project" }), "core");
    await user.click(screen.getAllByRole("button", { name: "Mint agent key" }).find((b) => !b.hasAttribute("aria-expanded"))!);
    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    expect(createApiKey.mock.calls[0][4]).toEqual(["prd"]);
  });

  it("does not offer tiers on the link form", async () => {
    view();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mint link key", expanded: false }));
    expect(screen.queryByRole("button", { name: /Fleet admin/ })).toBeNull();
  });

  it("a gate mint sends read+write+gate, pinned, and shows the CI table not the MCP picker", async () => {
    view();
    await mint("Gate key");
    await waitFor(() => expect(createApiKey).toHaveBeenCalled());
    const call = createApiKey.mock.calls[0];
    expect(call[3]).toEqual(expect.arrayContaining(["read", "write", "gate"]));
    expect(call[1]).toBe("core");
    expect(call[5]).toBe("web");
    const table = await screen.findByTestId("gate-ci-table");
    expect(within(table).getByLabelText("URL your CI runner reaches")).toHaveValue(window.location.origin);
    expect(within(table).getByText("GRAPHBAN_URL")).toBeInTheDocument();
    expect(within(table).getByText("GRAPHBAN_GATE_KEY")).toBeInTheDocument();
    expect(within(table).getByText(/gh variable set GRAPHBAN_URL/)).toBeInTheDocument();
    expect(within(table).getByText(/gh secret set GRAPHBAN_GATE_KEY/)).toBeInTheDocument();
    expect(within(table).getByText(/127\.0\.0\.1:8080/)).toBeInTheDocument();
    expect(within(table).getByText(/graphban\.local:8080/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Claude Code|Cursor/ })).toBeNull();
  });

  it("shows a server refusal verbatim", async () => {
    createApiKey.mockRejectedValueOnce(new Error(JSON.stringify({ detail: "a 'gate' credential must target one project" })));
    view();
    await mint("Gate key");
    expect(await screen.findByText(/The server refused the mint: a 'gate' credential must target one project/)).toBeInTheDocument();
  });
});

describe("Keys tab", () => {
  it("shows three groups with counts, and NONE for an empty one", async () => {
    keyList = [
      key({ id: "a1", name: "agent-one" }),
      key({ id: "a2", name: "agent-two" }),
      key({ id: "g1", name: "gate-one", scopes: ["read", "write", "gate"] }),
    ];
    view();
    await keysTab();
    await screen.findByText("agent-one");
    expect(screen.getByRole("region", { name: "Agent keys" })).toHaveTextContent(/Agent keys\s*2/);
    expect(screen.getByRole("region", { name: "Gate keys" })).toHaveTextContent(/Gate keys\s*1/);
    const link = screen.getByRole("region", { name: "Link keys" });
    expect(within(link).getByText("None")).toBeInTheDocument();
    expect(link).toHaveTextContent(/No deployment is linked/);
    // a gate key is in its own group, not also listed as an agent key
    expect(screen.getAllByText("gate-one")).toHaveLength(1);
    expect(within(screen.getByRole("region", { name: "Gate keys" })).getByText("gate-one")).toBeInTheDocument();
  });

  it("shows loading, then NOT LOADED with the reason, and Retry refetches", async () => {
    listFails = "database is down";
    view();
    await keysTab();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Not loaded/i);
    expect(alert).toHaveTextContent("database is down");
    listFails = null;
    keyList = [key({ id: "z", name: "back-again" })];
    const before = listKeys.mock.calls.length;
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("back-again")).toBeInTheDocument();
    expect(listKeys.mock.calls.length).toBeGreaterThan(before);
  });

  it("renders a loading status while the list is in flight", async () => {
    let release: (v: ApiKey[]) => void = () => undefined;
    listKeys.mockImplementationOnce(() => new Promise<ApiKey[]>((r) => { release = r; }));
    view();
    await keysTab();
    expect(await screen.findByRole("status")).toBeInTheDocument();
    release([]);
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("labels a wave key, an origin, a never-used key, and an expired one", async () => {
    keyList = [
      key({ id: "w", name: "fleet worker", fleet_wave: "wave-1" }),
      key({ id: "s", name: "p delegation", origin: "gban_setup", last_used: "2026-01-01T00:00:00Z" }),
      key({ id: "m", name: "swamp gate", scopes: ["read", "write", "gate"], origin: "gban_swamp_setup" }),
      key({ id: "e", name: "old", expires_at: "2020-01-01T00:00:00Z" }),
    ];
    view();
    await keysTab();
    expect(await screen.findByText(/wave-1 · swept by End wave/)).toBeInTheDocument();
    expect(screen.getByText("via gban setup")).toBeInTheDocument();
    expect(screen.getByText("via gban swamp setup")).toBeInTheDocument();
    expect(screen.getByText("expired")).toBeInTheDocument();
    expect(screen.getAllByText("never used").length).toBeGreaterThan(0);
  });

  it("offers each row only the actions its origin allows", async () => {
    keyList = [
      key({ id: "web", name: "web-agent", origin: "web" }),
      key({ id: "wave", name: "wave-agent", fleet_wave: "w" }),
      key({ id: "lnk", name: "link-one", scopes: ["sync"] }),
      key({ id: "gs", name: "setup-agent", origin: "gban_setup" }),
      key({ id: "sw", name: "swamp-gate", scopes: ["read", "write", "gate"], origin: "gban_swamp_setup" }),
    ];
    view();
    await keysTab();
    await screen.findByText("web-agent");
    const rowOf = (n: string) => screen.getByText(n).closest("div")!.parentElement!;
    const labels = (n: string) => within(rowOf(n)).getAllByRole("button")
      .map((b) => b.textContent).filter((t) => ["Rotate", "Revoke", "Unlink and relink…", "Revoke and rerun…"].includes(t ?? ""));
    expect(labels("web-agent")).toEqual(["Rotate", "Revoke"]);
    expect(labels("wave-agent")).toEqual(["Revoke"]);
    expect(labels("link-one")).toEqual(["Unlink and relink…", "Revoke"]);
    expect(labels("setup-agent")).toEqual(["Revoke and rerun…", "Revoke"]);
    expect(labels("swamp-gate")).toEqual(["Revoke and rerun…", "Revoke"]);
  });

  it("greys a revoked row, tags it, and gives it no actions", async () => {
    keyList = [key({ id: "r", name: "dead-key", revoked: true }), key({ id: "l", name: "live-key" })];
    view();
    await keysTab();
    const dead = (await screen.findByText("dead-key")).closest("div")!.parentElement!;
    expect(dead.className).toMatch(/opacity-50/);
    expect(within(dead).getByText("revoked")).toBeInTheDocument();
    expect(within(dead).queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(within(dead).queryByRole("button", { name: "Rotate" })).toBeNull();
    const live = screen.getByText("live-key").closest("div")!.parentElement!;
    expect(live.className).not.toMatch(/opacity-50/);
  });

  it("keeps permissions in the Minted with disclosure, with the server's tool count", async () => {
    keyList = [key({ id: "a", name: "claude-code", tool_tiers: ["prd"], tool_count: 34 })];
    view();
    const user = await keysTab();
    await screen.findByText("claude-code");
    expect(screen.queryByTestId("minted-with")).toBeNull();
    await user.click(screen.getByTitle("Minted with"));
    const panel = screen.getByTestId("minted-with");
    expect(panel).toHaveTextContent("core");
    expect(panel).toHaveTextContent("PRDs");
    expect(panel).toHaveTextContent("34 tools");
    expect(panel).toHaveTextContent("Pinned to core");
  });

  it("shows CI SECRETS on a gate row with no copyable key value", async () => {
    keyList = [key({ id: "g", name: "gh-actions", prefix: "gb_sk_zz", scopes: ["read", "write", "gate"] })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "CI secrets" }));
    const table = screen.getByTestId("gate-ci-table");
    expect(within(table).getByText(/the key starting gb_sk_zz/)).toBeInTheDocument();
    expect(within(table).queryByLabelText("Copy GRAPHBAN_GATE_KEY value")).toBeNull();
    expect(within(table).getByLabelText("Copy GRAPHBAN_URL value")).toBeInTheDocument();
  });
});

describe("row dialogs", () => {
  it("Rotate calls the rotate route once — no create, no DELETE — and shows the plaintext once", async () => {
    keyList = [key({ id: "k1", name: "web-agent", origin: "web", expires_at: "2099-01-01T00:00:00Z" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("New key expiry")).toHaveValue("90");
    expect(within(dialog).getByText(/update the MCP config of every client/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    await waitFor(() => expect(rotateApiKey).toHaveBeenCalledTimes(1));
    expect(rotateApiKey).toHaveBeenCalledWith("k1", 90);
    expect(createApiKey).not.toHaveBeenCalled();
    expect(revokeApiKey).not.toHaveBeenCalled();
    expect(await screen.findByText(/New key — shown once/i)).toBeInTheDocument();
    expect(screen.getByText("gb_sk_rotated_plain")).toBeInTheDocument();
    expect(screen.getByText(/claude mcp add/)).toBeInTheDocument();
  });

  it("Rotate on a gate row ends in the CI table", async () => {
    keyList = [key({ id: "g", name: "ci", scopes: ["read", "write", "gate"], origin: "web" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Rotate key" }));
    expect(await screen.findByTestId("gate-ci-table")).toBeInTheDocument();
    expect(rotateApiKey).toHaveBeenCalledWith("g", null);
  });

  it("a rotate failure stays inside the dialog", async () => {
    rotateApiKey.mockRejectedValueOnce(new Error(JSON.stringify({ detail: "key is already revoked" })));
    keyList = [key({ id: "k1", name: "web-agent", origin: "web" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Rotate key" }));
    expect(await within(screen.getByRole("dialog")).findByText("key is already revoked")).toBeInTheDocument();
  });

  it("Revoke and rerun sends only DELETE, then names gban setup", async () => {
    keyList = [key({ id: "k1", name: "p delegation", origin: "gban_setup" })];
    view();
    const user = await keysTab();
    expect(await screen.findByText("p delegation")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rotate" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Revoke and rerun…" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(revokeApiKey).toHaveBeenCalledWith("k1"));
    expect(rotateApiKey).not.toHaveBeenCalled();
    expect(createApiKey).not.toHaveBeenCalled();
    const done = await screen.findByRole("dialog", { name: /revoked/ });
    expect(within(done).getByText("gban setup")).toBeInTheDocument();
  });

  it("a gban swamp setup row names --replace", async () => {
    keyList = [key({ id: "k1", name: "swamp gate", scopes: ["read", "write", "gate"], origin: "gban_swamp_setup" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Revoke and rerun…" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revoke key" }));
    const done = await screen.findByRole("dialog", { name: /revoked/ });
    expect(within(done).getByText("gban swamp setup --replace")).toBeInTheDocument();
  });

  it("a link row offers Unlink and relink, not Rotate; confirming revokes then opens the link form", async () => {
    keyList = [key({ id: "l", name: "laptop", scopes: ["sync"] })];
    view();
    const user = await keysTab();
    expect(await screen.findByText("laptop")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rotate" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Unlink and relink…" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Unlink and relink" }));
    await waitFor(() => expect(revokeApiKey).toHaveBeenCalledWith("l"));
    expect(await screen.findByTestId("mint-form-sync")).toBeInTheDocument();
  });

  it("Revoke names the consequence for the kind and keeps an error inside the dialog", async () => {
    revokeApiKey.mockRejectedValueOnce(new Error(JSON.stringify({ detail: "boom" })));
    keyList = [key({ id: "k1", name: "web-agent" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Every MCP client using this key stops on its next call/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    expect(await within(dialog).findByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("a successful Revoke closes the dialog", async () => {
    keyList = [key({ id: "k1", name: "web-agent" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(revokeApiKey).toHaveBeenCalledWith("k1");
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


describe("design-audit gaps (GRPH-1313)", () => {
  const dialog = () => screen.getByRole("dialog");

  it("rotate dialog lists where the old value lives", async () => {
    keyList = [
      key({ id: "ag", name: "agent-k", origin: "web" }),
      key({ id: "gt", name: "gate-k", origin: "web", scopes: ["read", "write", "gate"] }),
    ];
    view();
    const user = await keysTab();
    const rotates = await screen.findAllByRole("button", { name: "Rotate" });
    await user.click(rotates[0]);
    const note = within(dialog()).getByText(/Update/).parentElement!;
    expect(note).toHaveTextContent("~/.claude.json");
    expect(note).toHaveTextContent("~/.codex/config.toml");
    await user.click(within(dialog()).getByRole("button", { name: "Cancel" }));
    await user.click((await screen.findAllByRole("button", { name: "Rotate" }))[1]);
    expect(within(dialog()).getByText(/Update/).parentElement!).toHaveTextContent("GRAPHBAN_GATE_KEY");
  });

  it("rotate dialog shows Now and the New expiry preview", async () => {
    keyList = [key({ id: "ne", name: "no-exp", origin: "web" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(dialog()).toHaveTextContent(/Now\s*No expiry/);
    expect(dialog()).toHaveTextContent("New expiry: none");
    await user.selectOptions(within(dialog()).getByRole("combobox"), "30");
    const d = new Date(Date.now() + 30 * 86400000).toLocaleDateString();
    expect(dialog()).toHaveTextContent(`New expiry: ${d}`);
  });

  it("shows the current expiry date in Now", async () => {
    keyList = [key({ id: "ex", origin: "web", expires_at: "2099-01-01T00:00:00Z" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(dialog()).toHaveTextContent(`Now ${new Date("2099-01-01T00:00:00Z").toLocaleDateString()}`);
  });

  it("destructive confirms are danger; rotate is not", async () => {
    keyList = [
      key({ id: "r1", name: "plain", origin: "web" }),
      key({ id: "r2", name: "setup", origin: "gban_setup" }),
      key({ id: "r3", name: "link", scopes: ["sync"], origin: "web" }),
    ];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(within(dialog()).getByRole("button", { name: "Rotate key" }).className).not.toContain("text-st-blocked");
    await user.click(within(dialog()).getByRole("button", { name: "Cancel" }));
    for (const [row, confirm] of [["Revoke and rerun…", "Revoke key"], ["Unlink and relink…", "Unlink and relink"]]) {
      await user.click(screen.getByRole("button", { name: row }));
      expect(within(dialog()).getByRole("button", { name: confirm }).className).toContain("text-st-blocked");
      await user.click(within(dialog()).getByRole("button", { name: "Cancel" }));
    }
    await user.click(screen.getAllByRole("button", { name: "Revoke" })[0]);
    expect(within(dialog()).getByRole("button", { name: "Revoke key" }).className).toContain("text-st-blocked");
  });

  it("row actions are bordered buttons; no-expiry rows say so", async () => {
    keyList = [key({ id: "b1", origin: "web" })];
    view();
    await keysTab();
    for (const n of ["Rotate", "Revoke"]) {
      expect((await screen.findByRole("button", { name: n })).className).toContain("border-control");
    }
    expect(screen.getByText(/no expiry/i)).toBeInTheDocument();
  });

  it("meta items (expiry, wave, origin) wrap to the second line; buttons stay on the first", async () => {
    keyList = [
      key({ id: "m1", name: "setup-row", origin: "gban_setup" }),
      key({ id: "m2", name: "swamp-row", origin: "gban_swamp_setup", fleet_wave: "wave-7" }),
    ];
    view();
    await keysTab();
    await screen.findByText("setup-row");
    for (const [label, extra] of [["via gban setup", []], ["via gban swamp setup", [/wave-7/]]] as const) {
      const meta = screen.getByText(label).closest("[data-line]")!;
      expect(meta.getAttribute("data-line")).toBe("meta");
      expect(within(meta as HTMLElement).getByText(/no expiry/i)).toBeInTheDocument();
      for (const re of extra) expect(within(meta as HTMLElement).getByText(re)).toBeInTheDocument();
      expect(within(meta as HTMLElement).queryAllByRole("button")).toHaveLength(0);
      const first = meta.parentElement!.querySelector('[data-line="first"]') as HTMLElement;
      expect(first.className).not.toContain("flex-wrap");
      const buttons = within(first).getAllByRole("button", { name: /^(Revoke|Revoke and rerun…)$/ });
      expect(buttons.length).toBeGreaterThan(0);
      for (const b of buttons) expect(b.className).toContain("flex-none");
    }
  });

  it("orders newer live, older live, then revoked", async () => {
    keyList = [
      key({ id: "old", name: "old-live", created_at: "2026-01-01T00:00:00Z" }),
      key({ id: "rev", name: "revoked-one", revoked: true, created_at: "2026-09-01T00:00:00Z" }),
      key({ id: "new", name: "new-live", created_at: "2026-06-01T00:00:00Z" }),
    ];
    view();
    await keysTab();
    await screen.findByText("new-live");
    const names = ["new-live", "old-live", "revoked-one"].map((n) => screen.getByText(n));
    const order = [...document.querySelectorAll("span")].filter((e) => names.includes(e));
    expect(order.map((e) => e.textContent)).toEqual(["new-live", "old-live", "revoked-one"]);
  });

  it("each mint form has a labelled Key expiry select", async () => {
    view();
    const user = userEvent.setup();
    for (const n of ["Mint agent key", "Mint link key", "Mint gate key"]) {
      await user.click(await screen.findByRole("button", { name: n, expanded: false }));
      expect(screen.getByLabelText("Key expiry")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: n, expanded: true }));
    }
  });

  it("rotated command wraps and the close button after a mint is the default variant", async () => {
    keyList = [key({ id: "rc", origin: "web" })];
    view();
    const user = await keysTab();
    await user.click(await screen.findByRole("button", { name: "Rotate" }));
    await user.click(within(dialog()).getByRole("button", { name: "Rotate key" }));
    const pre = await within(dialog()).findByText(/claude mcp add/);
    expect(pre.className).not.toMatch(/truncate|text-ellipsis/);
    expect(pre.className).toContain("break-all");
    await user.click(within(dialog()).getByRole("button", { name: /close/ }));
    await user.click(screen.getByRole("tab", { name: "Set up" }));
    await mint("Agent key", "x");
    expect((await screen.findByRole("button", { name: "I’ve copied it — close" })).className).toContain("bg-accent");
  });
});
