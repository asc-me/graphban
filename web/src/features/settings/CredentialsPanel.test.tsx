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

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
        description: "", provides: [], depends_on: [] },
    ]),
    credentials: vi.fn(async () => state),
    reindexStatus: vi.fn(async () => ({ running: false, tables: [] })),
    createCredential: (...a: unknown[]) => createCredential(...(a as [])),
    deleteCredential: (...a: unknown[]) => deleteCredential(...(a as [])),
    retryCredential: (...a: unknown[]) => retryCredential(...(a as [])),
    setScopeDefaults: (...a: unknown[]) => setDefaults(...(a as [])),
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

    expect(await screen.findByText("Primary key")).toBeInTheDocument();
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

    const row = await screen.findByTestId("credential-cred_a");
    expect(within(row).getByRole("button", { name: /set default/i })).toBeDisabled();
    expect(within(row).getByRole("button", { name: /set fallback/i })).toBeDisabled();
  });

  it("DOES offer an unreachable credential — the asymmetry is deliberate", async () => {
    // `unreachable` was asked and did not answer, which an operator may knowingly point at.
    // `pending_validation` means nobody ever established it works.
    state.credentials = [cred({ state: "unreachable", last_error: "connection refused" })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
    expect(within(row).getByRole("button", { name: /set default/i })).toBeEnabled();
  });

  it("shows why an unreachable credential failed, not merely that it did", async () => {
    state.credentials = [cred({ state: "unreachable", last_error: "connection refused" })];
    show();

    expect(await screen.findByText(/connection refused/)).toBeInTheDocument();
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

  it("cannot save a credential missing a field its kind requires", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));

    // anthropic needs a key and a model; neither is filled.
    expect(screen.getByRole("button", { name: /add credential/i })).toBeDisabled();
    expect(screen.getByText(/anthropic needs/i)).toBeInTheDocument();
    expect(createCredential).not.toHaveBeenCalled();
  });

  it("asks for what the KIND needs, not a fixed set of fields", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));

    // anthropic: a key, no endpoint.
    expect(screen.getByLabelText(/api key/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/endpoint/i)).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText(/provider kind/i), "ollama");

    // ollama: an endpoint, no key.
    expect(screen.getByLabelText(/endpoint/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/api key/i)).not.toBeInTheDocument();
  });

  it("saves once every required field is present", async () => {
    show();
    await userEvent.click(await screen.findByRole("button", { name: /add provider/i }));
    await userEvent.type(screen.getByLabelText(/api key/i), "sk-live");
    await userEvent.type(screen.getByLabelText(/^model$/i), "claude-x");

    await userEvent.click(screen.getByRole("button", { name: /add credential/i }));

    expect(createCredential).toHaveBeenCalledTimes(1);
  });

  // ---- refusals reach the operator ------------------------------------------------------

  it("surfaces a delete refusal verbatim, because it names the projects to fix", async () => {
    deleteCredential.mockRejectedValueOnce(new Error("used by core, web"));
    state.credentials = [cred({ used_by: ["core", "web"] })];
    show();

    const row = await screen.findByTestId("credential-cred_a");
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
