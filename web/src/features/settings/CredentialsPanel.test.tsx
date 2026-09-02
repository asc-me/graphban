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
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CredentialsPanel } from "@/features/settings/CredentialsPanel";
import { SettingsView } from "@/features/settings/SettingsView";
import { ProjectProvider } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import type { Credential, Project } from "@/lib/types";

const cred = (over: Partial<Credential> = {}): Credential => ({
  id: "cred_a", kind: "anthropic", label: "Primary", base_url: "", model: "claude-x",
  key_set: true, state: "valid", last_error: "", used_by: [], falling_back: [],
  is_default: false, is_fallback: false, is_embed: false, ...over,
});

const state: { credentials: Credential[] } = { credentials: [] };

const proj = (id: string, over: Partial<Project> = {}): Project => ({
  id, tag: id.toUpperCase().slice(0, 4), name: id, accent: id === "core" ? "#c6f24e" : "#4ea3f2",
  visibility: "private", description: "", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
  credential_id: null, model_override: "", chat_roles: {}, ...over,
});

let projectList: Project[] = [];
const setDefaults = vi.fn(async () => ({ scope: "" }));
const createCredential = vi.fn(
  async (_projectId: string, _body: Record<string, unknown>) =>
    ({ id: "cred_new", state: "pending_validation" }),
);
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
  async (projectId: string, body: Record<string, unknown>) => {
    // Behaves like the server: the write changes what `api.projects` returns next. Without
    // that, a test cannot tell an invalidated query from a stale one — both render the same
    // thing, which is exactly the bug this file now covers.
    projectList = projectList.map((p) =>
      p.id === projectId
        ? { ...p,
            credential_id: (body.credential_id as string | null) ?? null,
            model_override: (body.model_override as string) ?? p.model_override }
        : p);
    // `used_by` moves too — it is derived from the same pointer server-side. Without this the
    // credential card can never go stale in a test, and the invalidation that keeps it fresh
    // has nothing to prove.
    const target = (body.credential_id as string | null) ?? null;
    state.credentials = state.credentials.map((c) => ({
      ...c,
      used_by: c.id === target
        ? Array.from(new Set([...c.used_by, projectId]))
        : c.used_by.filter((p) => p !== projectId),
    }));
    return { project_id: projectId, credential_id: null, model_override: "" };
  },
);

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => projectList),
    // A SNAPSHOT, not the shared object. Returning `state` itself put the live array into
    // react-query's cache, so mutating it updated the cached value in place and the cache
    // could never be stale — which made an invalidation test unable to fail.
    credentials: vi.fn(async () => ({ credentials: [...state.credentials] })),
    aiProviders: vi.fn(async () => ({
      providers: [
        { id: "anthropic", label: "Anthropic", kind: "anthropic", embeds: false,
          base_url: "", chat_model: "claude-x", embed_model: "", auth: true },
        { id: "ollama", label: "Ollama", kind: "ollama", embeds: true,
          base_url: "http://localhost:11434", chat_model: "llama3.1:8b", embed_model: "bge-m3", auth: false },
        { id: "openai", label: "OpenAI", kind: "openai", embeds: true,
          base_url: "https://api.openai.com/v1", chat_model: "gpt-4o-mini",
          embed_model: "text-embedding-3-small", auth: true },
        { id: "custom", label: "Custom (OpenAI-compat)", kind: "openai", embeds: false,
          base_url: "", chat_model: "", embed_model: "", auth: true },
        { id: "stub", label: "Offline stub", kind: "stub", embeds: true,
          base_url: "", chat_model: "", embed_model: "", auth: false },
      ],
    })),
    reindexStatus: vi.fn(async () => ({ running: false, tables: [] })),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    createCredential: (p: string, body: Record<string, unknown>) => createCredential(p, body),
    deleteCredential: (...a: unknown[]) => deleteCredential(...(a as [])),
    retryCredential: (...a: unknown[]) => retryCredential(...(a as [])),
    setScopeDefaults: (...a: unknown[]) => setDefaults(...(a as [])),
    updateCredential: (p: string, id: string, body: Record<string, unknown>) =>
      updateCredential(p, id, body),
    setProjectCredential: (p: string, body: Record<string, unknown>) =>
      setProjectCredential(p, body),
    setProjectRoles: vi.fn(async () => ({ project_id: "core", chat_roles: {}, known_roles: [] })),
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


/** Header button when the dialog is closed; form submit when it is open. */
function addCredentialButton() {
  const form = screen.queryByTestId("credential-form");
  if (form) return within(form).getByRole("button", { name: /add credential/i });
  return screen.getByRole("button", { name: /add credential/i });
}

/** Controls are collapsed by default — open the row before acting on it. */
async function openRow(id = "cred_a") {
  const row = await screen.findByTestId(`credential-${id}`);
  await userEvent.click(within(row).getByRole("button", { name: /actions for/i }));
  return row;
}

beforeEach(() => {
  state.credentials = [];
  projectList = [proj("core"), proj("web")];
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

  it("offers inherit for every task model so a missing role is not a forced pick", async () => {
    state.credentials = [cred({ label: "Primary key" })];
    show();
    const panel = await screen.findByTestId("task-roles");
    expect(within(panel).getAllByRole("option", { name: /inherit project chat/i }).length).toBe(5);
    expect(within(panel).getByLabelText("Memory / eval judge")).toHaveValue("");
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
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));

    // The picker comes first: choosing a provider is a different question from filling in its
    // details, and one form with a kind dropdown makes the reader work out which fields matter.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /add a credential/i })).toBeInTheDocument();
    expect(screen.getByTestId("provider-picker")).toBeInTheDocument();
    expect(screen.queryByTestId("credential-form")).not.toBeInTheDocument();
  });

  it("does not offer the offline stub as something to add", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));

    // The stub is not a credential — it is what you get when there are none.
    const picker = await screen.findByTestId("provider-picker");
    expect(within(picker).queryByText(/offline stub/i)).not.toBeInTheDocument();
  });

  it("asks for what the CHOSEN provider needs, from the registry", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Anthropic"));

    // anthropic has auth:true and is not ollama — a provider key, no endpoint.
    // Not "API key": that is Graphban identity (Looking for API keys?).
    expect(await screen.findByLabelText(/provider key/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^api key$/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/endpoint/i)).not.toBeInTheDocument();
    // Label is the credential's name, not the secret. "Anthropic key" sat next
    // to Provider key and read as the same object.
    expect(screen.getByPlaceholderText("Anthropic credential")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Anthropic key")).not.toBeInTheDocument();
  });

  it("asks a DIFFERENT provider for different fields", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Ollama"));

    // ollama has auth:false — an endpoint, no key.
    expect(await screen.findByLabelText(/endpoint/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/provider key/i)).not.toBeInTheDocument();
  });

  it("cannot save without a field the chosen provider requires", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Anthropic"));

    expect(addCredentialButton()).toBeDisabled();
    expect(screen.getByText(/anthropic needs a provider key/i)).toBeInTheDocument();
    expect(createCredential).not.toHaveBeenCalled();
  });

  it("shows the endpoint for an OpenAI-compat provider, prefilled and editable", async () => {
    // GRPH-625. The field used to be ollama-only — a compat provider could never be aimed
    // at a gateway, proxy, or local server. Picking a provider prefills the registry's URL,
    // so api.openai.com stays zero-typing; editing it is the feature.
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("OpenAI"));

    const endpoint = await screen.findByLabelText(/endpoint/i);
    expect(endpoint).toHaveValue("https://api.openai.com/v1");
    await userEvent.clear(endpoint);
    await userEvent.type(endpoint, "https://gateway.internal/v1");
    await userEvent.type(screen.getByLabelText(/provider key/i), "sk-x");
    await userEvent.click(addCredentialButton());

    expect(createCredential).toHaveBeenCalledTimes(1);
    expect(createCredential.mock.calls[0][1]).toMatchObject({
      kind: "openai", base_url: "https://gateway.internal/v1",
    });
  });

  it("a custom endpoint demands the URL, the key, and a model before it saves", async () => {
    // The `custom` shape is all empty defaults, so everything is asked for — and nothing
    // pre-fills a lie about someone's gateway.
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Custom (OpenAI-compat)"));

    const endpoint = await screen.findByLabelText(/endpoint/i);
    expect(endpoint).toHaveValue("");
    expect(addCredentialButton()).toBeDisabled();
    expect(screen.getByText(/needs an endpoint/)).toBeInTheDocument();

    await userEvent.type(endpoint, "http://localhost:1234/v1");
    await userEvent.type(screen.getByLabelText(/provider key/i), "none");
    await userEvent.type(screen.getByLabelText(/^model$/i), "qwen2.5");
    await userEvent.click(addCredentialButton());

    expect(createCredential).toHaveBeenCalledTimes(1);
    expect(createCredential.mock.calls[0][1]).toMatchObject({
      kind: "custom", base_url: "http://localhost:1234/v1", model: "qwen2.5",
    });
  });

  it("cannot save an ollama credential with an empty endpoint even when the catalog has a default", async () => {
    // THE BOUNCE. needsOf skipped the URL check whenever the catalog had a default,
    // then create posted the empty form field. The saved row was pending_validation
    // and the edit dialog hides the endpoint when base_url is empty, so it could
    // not be repaired (GRPH-511).
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Ollama"));
    const endpoint = await screen.findByLabelText(/endpoint/i);
    await userEvent.clear(endpoint);

    expect(addCredentialButton()).toBeDisabled();
    expect(createCredential).not.toHaveBeenCalled();
  });

  it("saves once the required fields are present", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
    await userEvent.click(await screen.findByText("Anthropic"));
    await userEvent.type(await screen.findByLabelText(/provider key/i), "sk-live");

    await userEvent.click(addCredentialButton());

    expect(createCredential).toHaveBeenCalledTimes(1);
  });

  it("can go back to the picker without leaving the dialog", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add credential/i }));
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

function showSettings(path: string, hosted: boolean) {
  vi.mocked(api.config).mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={qc}>
        <ProjectProvider>
          <SettingsView />
        </ProjectProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("wiring", () => {
  it("the hosted AI Providers tab actually renders the panel", async () => {
    // THE CALL (GRPH-511 bounce). A source grep of the identifier stays green if the
    // hosted `{tab === "AI Providers" && <CredentialsPanel />}` is deleted — the import
    // and the SelfHostPane still mention the name. Rendering the hosted view is what
    // pins the tab.
    showSettings("/settings", true);

    expect(await screen.findByRole("link", { name: /^AI Providers$/ })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /^credentials$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add credential/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add provider/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /looking for api keys\?/i }))
      .toHaveAttribute("href", "/settings/project/api-keys");
  });

  it("the self-host providers route lives under This box (GRPH-625)", async () => {
    showSettings("/settings/deployment/providers", false);

    expect(await screen.findByText("This box")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /^credentials$/i })).toBeInTheDocument();
    // Nav placement too — "under the deployment section" is a nav fact, not just a route fact.
    const link = screen.getByRole("link", { name: "AI providers" });
    expect(link).toHaveAttribute("href", "/settings/deployment/providers");
    expect(screen.getByRole("link", { name: /looking for api keys\?/i }))
      .toHaveAttribute("href", "/settings/project/api-keys");
  });

  it("the old project-scoped deep link redirects instead of 404-ing into the project form", async () => {
    // Without the redirect branch, /settings/project/providers matches the project/*
    // fallback and renders ProjectPanel — plausible-looking, entirely the wrong pane.
    showSettings("/settings/project/providers", false);

    expect(await screen.findByRole("heading", { name: /^credentials$/i })).toBeInTheDocument();
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

  it("edit shows the endpoint for a custom credential even when the saved URL is empty", async () => {
    // GRPH-511/625. Hiding the field when base_url is empty made an unrepaired
    // custom row: you could not type the URL that the add form had refused to save.
    state.credentials = [cred({ kind: "custom", base_url: "" })];
    show();
    await openRow();
    await userEvent.click(screen.getByRole("button", { name: /^edit$/i }));

    expect(await screen.findByLabelText(/endpoint/i)).toBeInTheDocument();
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
    await userEvent.type(await screen.findByLabelText(/provider key/i), "sk-rotated");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(updateCredential.mock.calls[0][2].api_key).toBe("sk-rotated");
  });

  it("lists only projects that HAVE a rule", async () => {
    // Listing every project made the common case — most projects inherit — into a wall of
    // rows saying nothing.
    state.credentials = [cred({ id: "cred_a", label: "Primary", model: "claude-x" })];
    projectList = [
      proj("core", { credential_id: "cred_a" }),
      proj("web", { credential_id: null }),
    ];
    show();

    const rules = await screen.findByTestId("override-rules");
    expect(within(rules).getByTestId("rule-core")).toBeInTheDocument();
    expect(within(rules).queryByTestId("rule-web")).not.toBeInTheDocument();
  });

  it("shows the provider AND the model on a rule", async () => {
    // A rule naming only the provider hides the thing most often overridden: two projects
    // sharing a key and wanting different models is exactly what `model_override` is for.
    state.credentials = [cred({ id: "cred_a", label: "Primary", kind: "ollama", model: "mistral" })];
    projectList = [proj("core", { credential_id: "cred_a", model_override: "qwen" })];
    show();

    const rule = await screen.findByTestId("rule-core");
    expect(rule).toHaveTextContent("Primary");
    expect(rule).toHaveTextContent("ollama");
    expect(rule).toHaveTextContent("qwen");
  });

  it("falls back to the credential's own model when there is no override", async () => {
    state.credentials = [cred({ id: "cred_a", label: "Primary", model: "mistral" })];
    projectList = [proj("core", { credential_id: "cred_a", model_override: "" })];
    show();

    expect(await screen.findByTestId("rule-core")).toHaveTextContent("mistral");
  });

  it("says so when nothing overrides", async () => {
    state.credentials = [cred()];
    projectList = [proj("core", { credential_id: null })];
    show();

    expect(await screen.findByText(/every project uses the deployment default/i)).toBeInTheDocument();
  });

  it("adds a rule through a dialog, naming provider and model in the picker", async () => {
    state.credentials = [cred({ id: "cred_a", label: "Primary", kind: "ollama", model: "mistral" })];
    projectList = [proj("core"), proj("web", { credential_id: null })];
    show();

    await userEvent.click(await screen.findByRole("button", { name: /add rule/i }));
    const form = await screen.findByTestId("rule-form");
    // The credential option carries both, because two credentials can share a provider and
    // differ only by model.
    expect(within(form).getByRole("option", { name: /Primary — ollama · mistral/ })).toBeInTheDocument();

    await userEvent.selectOptions(within(form).getByTestId("rule-project"), "web");
    await userEvent.selectOptions(within(form).getByTestId("rule-credential"), "cred_a");
    await userEvent.click(within(form).getByRole("button", { name: /add rule/i }));

    expect(setProjectCredential).toHaveBeenCalledWith("web", { credential_id: "cred_a" });
  });

  it("seeds the model from the chosen credential, and sends an override only if changed", async () => {
    state.credentials = [cred({ id: "cred_a", label: "Primary", model: "mistral" })];
    projectList = [proj("core"), proj("web", { credential_id: null })];
    show();

    await userEvent.click(await screen.findByRole("button", { name: /add rule/i }));
    const form = await screen.findByTestId("rule-form");
    await userEvent.selectOptions(within(form).getByTestId("rule-project"), "web");
    await userEvent.selectOptions(within(form).getByTestId("rule-credential"), "cred_a");

    // Seeded, so the dialog shows what the rule will actually use.
    expect(within(form).getByLabelText(/^model$/i)).toHaveValue("mistral");

    const model = within(form).getByLabelText(/^model$/i);
    await userEvent.clear(model);
    await userEvent.type(model, "qwen");
    await userEvent.click(within(form).getByRole("button", { name: /add rule/i }));

    expect(setProjectCredential).toHaveBeenCalledWith("web", {
      credential_id: "cred_a", model_override: "qwen",
    });
  });

  it("will not offer a project that already has a rule", async () => {
    // "Add" must not silently mean "replace".
    state.credentials = [cred({ id: "cred_a" })];
    projectList = [proj("core", { credential_id: "cred_a" }), proj("web", { credential_id: null })];
    show();

    await userEvent.click(await screen.findByRole("button", { name: /add rule/i }));
    const picker = within(await screen.findByTestId("rule-form")).getByTestId("rule-project");

    expect(within(picker).queryByRole("option", { name: "core" })).not.toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: "web" })).toBeInTheDocument();
  });

  it("removes the tag from the credential card too", async () => {
    // The mirror of the reported bug, and it survived a sabotage: dropping the CREDENTIALS
    // invalidation passed every test, so the half the author happened to see working was the
    // half nothing covered.
    state.credentials = [cred({ id: "cred_a", used_by: ["core"] })];
    projectList = [proj("core", { credential_id: "cred_a" }), proj("web")];
    show();

    const card = await screen.findByTestId("credential-cred_a");
    expect(within(card).getByText("core")).toBeInTheDocument();

    await userEvent.click(within(await screen.findByTestId("rule-core"))
      .getByRole("button", { name: /remove/i }));

    await waitFor(() =>
      expect(within(screen.getByTestId("credential-cred_a")).queryByText("core"))
        .not.toBeInTheDocument());
  });

  it("removes the rule from the LIST, not just the tag above it", async () => {
    // FOUND BY USING IT. `refresh` invalidated only the credentials query, and the rules list
    // reads `useProjects` — so the tag vanished from the credential card while the rule row
    // stayed, showing an override that no longer existed.
    state.credentials = [cred({ id: "cred_a", used_by: ["core"] })];
    projectList = [proj("core", { credential_id: "cred_a" }), proj("web")];
    show();

    await userEvent.click(within(await screen.findByTestId("rule-core"))
      .getByRole("button", { name: /remove/i }));

    await waitFor(() =>
      expect(screen.queryByTestId("rule-core")).not.toBeInTheDocument());
  });

  it("shows a newly added rule without a reload", async () => {
    // The same staleness in the other direction: adding a rule wrote the pointer and left the
    // list empty until something else happened to refetch projects.
    state.credentials = [cred({ id: "cred_a", label: "Primary", model: "mistral" })];
    projectList = [proj("core"), proj("web")];
    show();

    await userEvent.click(await screen.findByRole("button", { name: /add rule/i }));
    const form = await screen.findByTestId("rule-form");
    await userEvent.selectOptions(within(form).getByTestId("rule-project"), "web");
    await userEvent.selectOptions(within(form).getByTestId("rule-credential"), "cred_a");
    await userEvent.click(within(form).getByRole("button", { name: /add rule/i }));

    await waitFor(() => expect(screen.getByTestId("rule-web")).toBeInTheDocument());
  });

  it("removing a rule clears the model override with it", async () => {
    // A model override left behind would apply to whatever the project inherits next, which
    // is not what "remove this rule" means.
    state.credentials = [cred({ id: "cred_a" })];
    projectList = [proj("core", { credential_id: "cred_a", model_override: "qwen" })];
    show();

    await userEvent.click(within(await screen.findByTestId("rule-core"))
      .getByRole("button", { name: /remove/i }));

    expect(setProjectCredential).toHaveBeenCalledWith("core", {
      credential_id: null, model_override: "",
    });
  });
});
