/**
 * The credentials view (PRD-25 S5, GRPH-511).
 *
 * **Two controls come first, because "render nothing" satisfies most of the assertions here.**
 * A panel that returned `null` would pass a test asserting a pending credential is not
 * offerable as default. So: a deployment with one credential still renders it, and the tags a
 * delete would refuse on are actually shown.
 *
 * The three claims with teeth:
 *
 * - a `pending_validation` credential cannot be chosen as default or fallback — an unusable
 *   credential that can still be selected is the same defect one layer along;
 * - an `unreachable` row shows `last_error` — an operator told it failed and not why has to go
 *   and reproduce it;
 * - `falling_back` is shown distinctly from `used_by` — a project can point at a credential and
 *   not be served by it, which is exactly what §4 required the UI to surface.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CredentialsPanel } from "@/features/settings/CredentialsPanel";
import { ProjectProvider } from "@/features/ProjectContext";
import type { Credential } from "@/lib/types";

const cred = (over: Partial<Credential> = {}): Credential => ({
  id: "cred_a", kind: "anthropic", label: "Primary", base_url: "", model: "claude-x",
  key_set: true, state: "valid", last_error: "", used_by: [], falling_back: [],
  is_default: false, is_fallback: false, is_embed: false, ...over,
});

const state: { credentials: Credential[] } = { credentials: [] };
const setDefaults = vi.fn(async () => ({ scope: "" }));
const createCredential = vi.fn(async () => ({ id: "cred_new", state: "pending_validation" }));
const deleteCredential = vi.fn(async () => undefined);
const retryCredential = vi.fn(async () => ({
  id: "cred_a", state: "valid", last_error: "", validation_attempts: 0,
}));
// Declared WITH parameters so `mock.calls[0][2]` is typed — a no-arg `vi.fn` gives an empty
// tuple, and indexing it passes the tests while failing the typecheck.
const updateCredential = vi.fn(
  async (_projectId: string, _id: string, _body: Record<string, unknown>) =>
    ({ id: "cred_a", state: "valid" }),
);
const setProjectCredential = vi.fn(
  async (_projectId: string, _body: Record<string, unknown>) =>
    ({ project_id: "core", credential_id: null, model_override: "" }),
);

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
        description: "", provides: [], depends_on: [] },
      { id: "web", tag: "WEB", name: "Web", accent: "#4ea3f2", visibility: "private",
        description: "", provides: [], depends_on: [] },
    ]),
    credentials: vi.fn(async () => state),
    aiProviders: vi.fn(async () => ({
      providers: [
        { id: "anthropic", label: "Anthropic", kind: "anthropic", embeds: false,
          base_url: "", chat_model: "claude-x", embed_model: "", auth: true },
        { id: "ollama", label: "Ollama", kind: "ollama", embeds: true,
          base_url: "http://localhost:11434", chat_model: "llama3.1:8b", embed_model: "bge-m3", auth: false },
        { id: "stub", label: "Offline stub", kind: "stub", embeds: true,
          base_url: "", chat_model: "", embed_model: "", auth: false },
      ],
    })),
    reindexStatus: vi.fn(async () => ({ running: false, tables: [] })),
    createCredential: (...a: unknown[]) => createCredential(...(a as [])),
    deleteCredential: (...a: unknown[]) => deleteCredential(...(a as [])),
    retryCredential: (...a: unknown[]) => retryCredential(...(a as [])),
    setScopeDefaults: (...a: unknown[]) => setDefaults(...(a as [])),
    updateCredential: (p: string, id: string, body: Record<string, unknown>) =>
      updateCredential(p, id, body),
    setProjectCredential: (p: string, body: Record<string, unknown>) =>
      setProjectCredential(p, body),
  },
}));

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/p/core/settings"]}>
      <QueryClientProvider client={qc}>
        <ProjectProvider>
          <CredentialsPanel />
        </ProjectProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}


/** Controls are collapsed by default — open the row before acting on it. */
async function openRow(id = "cred_a") {
  const row = await screen.findByTestId(`credential-${id}`);
  await userEvent.click(within(row).getByRole("button", { name: /actions for/i }));
  return row;
}

beforeEach(() => {
  state.credentials = [];
  vi.clearAllMocks();
});

describe("CredentialsPanel", () => {
  // ---- the controls -------------------------------------------------------------------

  it("renders a credential that exists", async () => {
    // The control. Every assertion below about what is NOT offered would pass against a panel
    // that rendered nothing at all.
    state.credentials = [cred({ label: "Primary key" })];
    show();

    // Asserted on the ROW, not a bare text match: the label appears both as text and inside
    // the toggle button's accessible name, so `findByText` now finds two elements.
    expect(await screen.findByTestId("credential-cred_a")).toHaveTextContent("Primary key");
  });

  it("shows which projects use a credential, because a delete refusal must be predictable", async () => {
    state.credentials = [cred({ used_by: ["core", "web"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    expect(within(row).getByText("core")).toBeInTheDocument();
    expect(within(row).getByText("web")).toBeInTheDocument();
  });

  // ---- the state gate -----------------------------------------------------------------

  it("will not offer a pending credential as default or fallback", async () => {
    state.credentials = [cred({ state: "pending_validation" })];
    show();

    const row = await openRow();
    expect(within(row).getByRole("button", { name: /set default/i })).toBeDisabled();
    expect(within(row).getByRole("button", { name: /set fallback/i })).toBeDisabled();
  });

  it("DOES offer an unreachable credential — the asymmetry is deliberate", async () => {
    // `unreachable` was asked and did not answer, which an operator may knowingly point at.
    // `pending_validation` means nobody ever established it works.
    state.credentials = [cred({ state: "unreachable", last_error: "connection refused" })];
    show();

    const row = await openRow();
    expect(within(row).getByRole("button", { name: /set default/i })).toBeEnabled();
  });

  it("shows why an unreachable credential failed, not merely that it did", async () => {
    state.credentials = [cred({ state: "unreachable", last_error: "connection refused" })];
    show();

    expect(await screen.findByText(/connection refused/)).toBeInTheDocument();
  });

  it("puts the tags on the RIGHT of the card", async () => {
    // Asserted on the class that does the pushing, which is admittedly brittle — but the
    // alternative was no coverage at all: dropping `ml-auto` passed all sixteen other tests,
    // and "tags on the right" was an explicit requirement, not an incidental.
    state.credentials = [cred({ is_default: true, used_by: ["core"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    const tags = within(row).getByText("default").parentElement!;

    expect(tags.className).toContain("ml-auto");
  });

  // ---- project tags carry the project's accent -----------------------------------------

  it("glows each project tag in that project's accent colour", async () => {
    // Two projects with different accents, so "uses the accent" can be told from "uses a
    // colour". A single project would let a hardcoded colour pass.
    state.credentials = [cred({ used_by: ["core", "web"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    const core = within(row).getByText("core");
    const web = within(row).getByText("web");

    expect(core.style.boxShadow).toContain("#c6f24e");
    expect(web.style.boxShadow).toContain("#4ea3f2");
    expect(core.style.boxShadow).not.toEqual(web.style.boxShadow);
  });

  // ---- falling back -------------------------------------------------------------------

  it("distinguishes a project that POINTS here from one being SERVED here", async () => {
    // The distinction §4 required. `used_by` alone cannot make it, and without it a project
    // silently running on the deployment default looks identical to one that is fine.
    state.credentials = [cred({ state: "unreachable", used_by: ["core", "web"], falling_back: ["web"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    const served = within(row).getByText("core");
    const notServed = within(row).getByText("web");
    expect(notServed.className).not.toEqual(served.className);
    expect(notServed.getAttribute("title")).toMatch(/not being served/i);
  });

  // ---- the dialog ---------------------------------------------------------------------

  it("opens a DIALOG and asks which provider first", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));

    // The picker comes first: choosing a provider is a different question from filling in its
    // details, and one form with a kind dropdown makes the reader work out which fields matter.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("provider-picker")).toBeInTheDocument();
    expect(screen.queryByTestId("credential-form")).not.toBeInTheDocument();
  });

  it("does not offer the offline stub as something to add", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));

    // The stub is not a credential — it is what you get when there are none.
    const picker = await screen.findByTestId("provider-picker");
    expect(within(picker).queryByText(/offline stub/i)).not.toBeInTheDocument();
  });

  it("asks for what the CHOSEN provider needs, from the registry", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.click(await screen.findByText("Anthropic"));

    // anthropic has auth:true and is not ollama — a key, no endpoint.
    expect(await screen.findByLabelText(/api key/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/endpoint/i)).not.toBeInTheDocument();
  });

  it("asks a DIFFERENT provider for different fields", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.click(await screen.findByText("Ollama"));

    // ollama has auth:false — an endpoint, no key.
    expect(await screen.findByLabelText(/endpoint/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/api key/i)).not.toBeInTheDocument();
  });

  it("cannot save without a field the chosen provider requires", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.click(await screen.findByText("Anthropic"));

    expect(await screen.findByRole("button", { name: /add credential/i })).toBeDisabled();
    expect(screen.getByText(/anthropic needs/i)).toBeInTheDocument();
    expect(createCredential).not.toHaveBeenCalled();
  });

  it("saves once the required fields are present", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.click(await screen.findByText("Anthropic"));
    await userEvent.type(await screen.findByLabelText(/api key/i), "sk-live");

    await userEvent.click(screen.getByRole("button", { name: /add credential/i }));

    expect(createCredential).toHaveBeenCalledTimes(1);
  });

  it("can go back to the picker without leaving the dialog", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.click(await screen.findByText("Anthropic"));
    await userEvent.click(await screen.findByRole("button", { name: /^back$/i }));

    expect(await screen.findByTestId("provider-picker")).toBeInTheDocument();
  });

  // ---- refusals reach the operator ------------------------------------------------------

  it("surfaces a delete refusal verbatim, because it names the projects to fix", async () => {
    deleteCredential.mockRejectedValueOnce(new Error("used by core, web"));
    state.credentials = [cred({ used_by: ["core", "web"] })];
    show();

    const row = await openRow();
    await userEvent.click(within(row).getByRole("button", { name: /delete/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("used by core, web");
  });
});

describe("wiring", () => {
  it("is mounted in the settings view, not merely written", async () => {
    // A panel nothing renders is the GRPH-496 shape — correct, and unreachable from the
    // product. Asserted against the view's source because rendering SettingsView needs the
    // whole tab shell; the claim here is that the component is referenced at all.
    const src = await import("@/features/settings/SettingsView?raw").then((m) => m.default as string);

    expect(src).toContain("CredentialsPanel");
  });
});

describe("collapsing, health, editing and overrides", () => {
  it("hides the controls until the row is opened", async () => {
    // A list exists to be read. Five buttons per row on a deployment with a dozen credentials
    // is a wall, and the chevron opens only the row being acted on.
    state.credentials = [cred()];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    expect(within(row).queryByRole("button", { name: /set default/i })).not.toBeInTheDocument();

    await openRow();

    expect(within(row).getByRole("button", { name: /set default/i })).toBeInTheDocument();
  });

  it("reports the open state to assistive tech, not just visually", async () => {
    state.credentials = [cred()];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    const toggle = within(row).getByRole("button", { name: /actions for/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await openRow();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it.each([
    ["valid", "valid"],
    ["pending_validation", "pending validation"],
    ["unreachable", "unreachable"],
  ])("shows a labelled health dot for %s", async (state_, label) => {
    // The dot replaced a text chip that read like another tag. Colour alone would be worse
    // than the chip for anyone not distinguishing red from green, so it is labelled.
    state.credentials = [cred({ state: state_ as Credential["state"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    expect(within(row).getByRole("img", { name: label })).toBeInTheDocument();
  });

  it("no longer renders state as a chip beside the usage tags", async () => {
    state.credentials = [cred({ state: "valid", is_default: true })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    // "default" is a tag; "valid" is not one any more.
    expect(within(row).getByText("default")).toBeInTheDocument();
    expect(within(row).queryByText("valid")).not.toBeInTheDocument();
  });

  it("edits a credential's model without touching its key", async () => {
    state.credentials = [cred({ key_set: true, model: "claude-old" })];
    show();
    await openRow();
    await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));

    const model = await screen.findByLabelText(/^model$/i);
    await userEvent.clear(model);
    await userEvent.type(model, "claude-new");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateCredential).toHaveBeenCalledTimes(1);
    const body = updateCredential.mock.calls[0][2];
    expect(body.model).toBe("claude-new");
    // An empty key means "leave it alone" — sending "" would erase a working credential
    // because the operator opened the dialog to change the model.
    expect(body).not.toHaveProperty("api_key");
  });

  it("sends the key only when one was typed", async () => {
    state.credentials = [cred({ key_set: true })];
    show();
    await openRow();
    await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    await userEvent.type(await screen.findByLabelText(/api key/i), "sk-rotated");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateCredential.mock.calls[0][2].api_key).toBe("sk-rotated");
  });

  it("sets a project override on the project being changed, not the active one", async () => {
    // `setProjectCredential` targets the project BEING CHANGED, which is also what scopes the
    // server-side authz check. Passing the currently-viewed project would edit the wrong row.
    state.credentials = [cred({ id: "cred_a", label: "Primary" })];
    show();

    const overrides = await screen.findByTestId("project-overrides");
    await userEvent.selectOptions(
      within(overrides).getByLabelText(/credential for web/i), "cred_a");

    expect(setProjectCredential).toHaveBeenCalledWith("web", { credential_id: "cred_a" });
  });

  it("clears an override back to the deployment default", async () => {
    state.credentials = [cred({ id: "cred_a", used_by: ["web"] })];
    show();

    const overrides = await screen.findByTestId("project-overrides");
    const row = within(overrides).getByLabelText(/credential for web/i).closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: /clear/i }));

    expect(setProjectCredential).toHaveBeenCalledWith("web", { credential_id: null });
  });

  it("cannot clear a project that is already inheriting", async () => {
    state.credentials = [cred({ id: "cred_a", used_by: [] })];
    show();

    const overrides = await screen.findByTestId("project-overrides");
    const row = within(overrides).getByLabelText(/credential for core/i).closest("li")!;
    expect(within(row).getByRole("button", { name: /clear/i })).toBeDisabled();
  });
});
