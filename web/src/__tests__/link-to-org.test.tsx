import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LinkedPanel } from "@/features/organization/LinkedPanel";
import {
  expiryText,
  LINK_FINISHED_KEY,
  LinkToOrgWizard,
  resumeTarget,
  unreachableText,
  useLinkFlow,
} from "@/features/organization/LinkToOrgWizard";
import { CONSOLE_URL } from "@/lib/routes";
import type { SyncProjectState, SyncStatus } from "@/lib/types";

const CONSOLE_HOST = new URL(CONSOLE_URL).host;

const api = vi.hoisted(() => ({
  syncLinkStart: vi.fn(),
  syncLinkPoll: vi.fn(),
  syncCloudProjects: vi.fn(),
  syncSaveProjectMap: vi.fn(),
  syncPushAll: vi.fn(),
  projects: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

// GRPH-1562: cloud ids are UUIDs and never equal the tag, so a surface that renders the id fails.
const CORE_ID = "b1d36410-76d9-4c2e-9a51-0f3e2a7c1d01";
const NORTH_ID = "7a0c2e91-3f44-4b8e-8d12-5c6b9e0a4f02";
// What a person can read: text plus every tooltip. An <option value> is not shown.
const noUuid = () => {
  const titles = Array.from(document.querySelectorAll("[title],[aria-label]"))
    .map((e) => `${e.getAttribute("title") ?? ""} ${e.getAttribute("aria-label") ?? ""}`).join(" ");
  const seen = `${document.body.textContent ?? ""} ${titles}`.toLowerCase();
  for (const id of [CORE_ID, NORTH_ID]) expect(seen).not.toContain(id.slice(0, 8));
};

const proj = (id: string, name: string, extra: Partial<SyncProjectState> = {}): SyncProjectState => ({
  project_id: id, name, writable: true, sync_graph: true, total_nodes: 1, synced_nodes: 1,
  pending: 0, last_synced_at: null, status: "live", mapped: false, ...extra,
});

const base: SyncStatus = {
  linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null,
  projects: [proj("core", "Core"), proj("north", "North")],
};

const signedIn: SyncStatus = {
  ...base, linked: true, source: "web", cloud_url: "https://c.dev", org: "Acme",
  signed_in: true, linked_email: "ann@acme.dev", state: "ok",
  projects: [
    proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE", last_push_result: "ok" }),
    proj("north", "North"),
  ],
};

function wizard(status: SyncStatus, props: Partial<React.ComponentProps<typeof LinkToOrgWizard>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <LinkToOrgWizard status={status} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.removeItem(LINK_FINISHED_KEY);
  api.syncLinkStart.mockResolvedValue({
    status: "started", user_code: "WXYZ-1234", verification_url: "https://c.dev/link", interval: 0.01,
  });
  api.syncLinkPoll.mockResolvedValue({ status: "pending" });
  api.projects.mockResolvedValue([
    { id: "core", tag: "CORE", name: "Core" },
    { id: "north", tag: "NRTH", name: "North" },
  ]);
  api.syncCloudProjects.mockResolvedValue({
    status: "ok", projects: [{ id: CORE_ID, tag: "CORE", name: "Core project" }], mapping: [],
  });
});

describe("LinkToOrgWizard", () => {
  it("① reads SIGN-IN · NOT YET, muted with a still dot, until a code exists", () => {
    wizard(base);
    const rail = screen.getByRole("list", { name: "Link steps" });
    const nodes = within(rail).getAllByRole("listitem");
    expect(nodes.map((n) => n.textContent)).toEqual([
      `①Console◌${CONSOLE_HOST}`,
      "②Sign in◌your step",
      "③Projects◌not yet",
      "④First push◌not yet",
    ]);
    const st = screen.getByRole("status");
    expect(st).toHaveTextContent("SIGN-IN · NOT YET");
    expect(st.getAttribute("data-tone")).toBe("muted");
    expect(st.className).toContain("text-faint");
    expect(screen.getByTestId("status-dot").getAttribute("data-pulse")).toBe("false");
  });

  it("shows the rail and a waiting status line, with a pulsing dot, once a code exists", async () => {
    const user = userEvent.setup();
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByTestId("user-code");
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · WAITING");
    expect(screen.getByTestId("status-dot").getAttribute("data-pulse")).toBe("true");
    // Waiting is muted: never the lime accent, never amber.
    const st = screen.getByRole("status");
    expect(st.getAttribute("data-tone")).toBe("wait");
    expect(st.className).toContain("text-muted");
    expect(st.className).not.toMatch(/text-accent|amber|text-st-done/);
    expect(screen.getByTestId("status-dot").className).toContain("motion-reduce:animate-none");
  });

  it("makes the console URL read-only with a note when SYNC_CLOUD_URL sets it on an unlinked box", async () => {
    // What GET /api/sync/status sends for an env-URL-only box: no link yet, so source and
    // cloud_url are empty — only console_url_env carries the baked-in URL (GRPH-1537).
    const user = userEvent.setup();
    wizard({ ...base, console_url_env: "https://baked.dev" });
    const input = screen.getByLabelText("Console URL");
    expect(input).toHaveAttribute("readonly");
    expect(input).toHaveValue("https://baked.dev");
    expect(screen.getByText("SOURCE · env")).toBeInTheDocument();
    expect(input).toHaveAccessibleDescription(
      "SOURCE · env Set by this deployment's environment. Change SYNC_CLOUD_URL to move it.",
    );
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(api.syncLinkStart).toHaveBeenCalledWith("https://baked.dev");
  });

  it("keeps the console URL editable when no SYNC_CLOUD_URL is set", () => {
    wizard(base);
    expect(screen.getByLabelText("Console URL")).not.toHaveAttribute("readonly");
    expect(screen.queryByText(/SYNC_CLOUD_URL/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Console URL")).toHaveAccessibleDescription(
      "The address of your org's Graphban console.",
    );
  });

  it("Next → on ① is disabled with a reason while the URL is empty; ← Back is absent on ①", async () => {
    const user = userEvent.setup();
    wizard(base);
    expect(screen.queryByRole("button", { name: "← Back" })).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Console URL"));
    const next = screen.getByRole("button", { name: "Next →" });
    expect(next).toBeDisabled();
    expect(next).toHaveAccessibleDescription("Enter the console's URL.");
  });

  it("shows the code, copy, open, expiry and terminal command", async () => {
    const user = userEvent.setup();
    api.syncLinkStart.mockResolvedValue({
      status: "started", user_code: "WXYZ-1234", verification_url: "https://c.dev/link", interval: 0.01,
      expires_at: new Date(Date.now() + 7 * 60000 - 1000).toISOString(),
    });
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByTestId("user-code")).toHaveTextContent("WXYZ-1234");
    const block = screen.getByTestId("code-block");
    expect(within(block).getByText("YOUR STEP")).toBeInTheDocument();
    expect(block).toHaveTextContent("Open c.dev/link and enter the code.");
    expect(within(block).getByText("c.dev/link").tagName).toBe("STRONG");
    expect(screen.getByText("Waiting for you to approve it in the console.")).toBeInTheDocument();
    expect(within(block).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the console →" })).toHaveAttribute("href", "https://c.dev/link");
    expect(screen.getByRole("button", { name: "Copy link" })).toBeInTheDocument();
    // The expiry is the console's expires_at, not a fixed "10 minutes".
    expect(screen.getByTestId("expiry")).toHaveTextContent("Expires in 7 minutes.");
    const term = screen.getByTestId("prefer-terminal");
    expect(term).toHaveTextContent("PREFER THE TERMINAL?");
    expect(term).toHaveTextContent(`$ graphban link --cloud-url ${CONSOLE_URL}`);
    expect(within(term).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    // Rail ② is pending and waiting; Next is off with the reason.
    expect(document.querySelector('[data-rail-node="2"]')).toHaveTextContent("②Sign in◌waiting…");
    const next = screen.getByRole("button", { name: "Next →" });
    expect(next).toBeDisabled();
    expect(next).toHaveAccessibleDescription("Approve the code in the console first.");
    expect(screen.getByRole("button", { name: "← Back" })).toBeInTheDocument();
  });

  it("no expires_at, no expiry line — never a hard-coded one", async () => {
    const user = userEvent.setup();
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByTestId("user-code");
    expect(screen.queryByTestId("expiry")).not.toBeInTheDocument();
    expect(screen.queryByText(/Expires in/)).not.toBeInTheDocument();
  });

  it("expiryText counts down from expires_at", () => {
    const now = Date.parse("2026-11-12T10:00:00Z");
    expect(expiryText("2026-11-12T10:10:00Z", now)).toBe("Expires in 10 minutes.");
    expect(expiryText("2026-11-12T10:00:30Z", now)).toBe("Expires in 1 minute.");
    expect(expiryText("2026-11-12T09:59:00Z", now)).toBe("This code has expired.");
    expect(expiryText(undefined, now)).toBeNull();
  });

  it("Copied is a visible label on the button that was pressed", async () => {
    const user = userEvent.setup();
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    const block = await screen.findByTestId("code-block");
    await user.click(within(block).getByRole("button", { name: "Copy" }));
    const copied = within(block).getByRole("button", { name: "Copied" });
    expect(copied).toBeVisible();
    expect(copied.className).not.toContain("sr-only");
    // Only that one: the terminal block's Copy is untouched.
    expect(within(screen.getByTestId("prefer-terminal")).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(document.querySelector(".sr-only")?.textContent ?? "").not.toMatch(/Copied/);
  });

  it.each([
    ["declined", "The code was declined in the console."],
    ["expired", "This code expired. Get a new code."],
    ["not_member", "That account isn't a member of an org."],
    ["no_seat", "You don't have a seat in Acme. Ask an org admin."],
  ])("says %s word for word, in red, with Get a new code", async (reason, text) => {
    const user = userEvent.setup();
    api.syncLinkPoll.mockResolvedValue({ status: "refused", reason });
    wizard({ ...base, org: "Acme" });
    await user.click(screen.getByRole("button", { name: "Next →" }));
    const msg = await screen.findByText(text);
    expect(msg.className).toContain("text-st-blocked");
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · REFUSED");
    expect(screen.getByRole("button", { name: "Get a new code" })).toBeInTheDocument();
  });

  it("an unreachable console is grey with Try again, never refused", async () => {
    const user = userEvent.setup();
    api.syncLinkPoll.mockResolvedValue({ status: "unknown", reason: "down" });
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("CONSOLE · UNREACHABLE");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("grey");
    expect(screen.queryByText(/refused/i)).not.toBeInTheDocument();
    // The line carries the backend's reason.
    expect(screen.getByTestId("unreachable")).toHaveTextContent(`? Couldn't reach ${CONSOLE_HOST}: down.`);
    expect(document.querySelector('[data-rail-node="2"]')).toHaveTextContent("②Sign in?couldn't reach");
  });

  it("an unreachable start shows the reason the backend gave", async () => {
    const user = userEvent.setup();
    api.syncLinkStart.mockResolvedValue({ status: "unknown", reason: "connection timed out after 10s" });
    wizard({ ...base, console_url_env: "https://cloud.acme.dev" });
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByTestId("unreachable")).toHaveTextContent(
      "Couldn't reach cloud.acme.dev: connection timed out after 10s.",
    );
    expect(unreachableText("https://x.dev", "")).toBe("Couldn't reach x.dev.");
  });

  it("approval reads SIGNED IN · <ORG>, then Projects, each defaulting to Local only", async () => {
    const user = userEvent.setup();
    api.syncLinkPoll.mockResolvedValue({ status: "approved", org: "Acme", email: "ann@acme.dev" });
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByText(/Signed in to Acme as/)).toHaveTextContent("✓ Signed in to Acme as ann@acme.dev.");
    expect(screen.getByRole("status")).toHaveTextContent("SIGNED IN · ACME");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("ok");
    expect(document.querySelector('[data-rail-node="1"]')).toHaveTextContent(`①Console✓${CONSOLE_HOST}`);
    expect(document.querySelector('[data-rail-node="2"]')).toHaveTextContent("②Sign in✓Acme · ann@acme.dev");
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByLabelText("Cloud project for Core")).toHaveValue("");
    expect(screen.getByText("Pick where each local project syncs in Acme. Local only keeps it on this box.")).toBeInTheDocument();
    expect(screen.getAllByText("YOUR STEP")).toHaveLength(1);
    // Rows are labelled with the local project's tag.
    expect(await screen.findByText("CORE", { selector: "label" })).toHaveAttribute("title", "Core");
    expect(screen.getByText("NRTH", { selector: "label" })).toBeInTheDocument();
    expect(document.querySelector('[data-rail-node="3"]')).toHaveTextContent("③Projects◌0 synced · 2 local");
    expect(screen.getByLabelText("Cloud project for North")).toHaveValue("");
    expect(screen.getByText("Nothing will sync. The box stays linked to Acme.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("PROJECTS · 0 SYNCED · 2 LOCAL");
    // Nothing synced yet is muted, not green.
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("muted");
    expect(screen.getByRole("status").className).not.toContain("text-st-done");
    await user.selectOptions(screen.getByLabelText("Cloud project for Core"), CORE_ID);
    expect(screen.getByText("☁ CORE")).toBeInTheDocument();
    noUuid();
    expect(screen.getByRole("status")).toHaveTextContent("PROJECTS · 1 SYNCED · 1 LOCAL");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("ok");
    expect(screen.getByRole("status").className).toContain("text-st-done");
    expect(document.querySelector('[data-rail-node="3"]')).toHaveTextContent("1 synced · 1 local");
  });

  it("a seat-removed refusal of the cloud projects names it and offers Sign in again", async () => {
    api.syncCloudProjects.mockResolvedValue({ status: "refused", reason: "seat_removed" });
    wizard(signedIn, { initialStep: 3 });
    expect(await screen.findByText("Your seat in Acme was removed. Nothing is syncing.")).toBeInTheDocument();
    expect(screen.queryByText("seat_removed")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · REFUSED");
  });

  it("says no reachable projects, and unknown is grey rather than 'no projects'", async () => {
    api.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [] });
    const { unmount } = wizard(signedIn, { initialStep: 3 });
    expect(await screen.findByText(/You don't have access to any projects in Acme yet. Ask an org admin./)).toBeInTheDocument();
    // No reachable projects: no selects at all.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText(/Pick where/)).not.toBeInTheDocument();
    unmount();

    api.syncCloudProjects.mockResolvedValue({ status: "unknown", reason: "down" });
    wizard(signedIn, { initialStep: 3 });
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(/don't have access/)).not.toBeInTheDocument();
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("grey");
    // The unknown line is the backend's reason, not a fixed sentence.
    expect(screen.getByTestId("unreachable")).toHaveTextContent("Couldn't reach c.dev: down.");
    expect(document.querySelector('[data-rail-node="3"]')).toHaveTextContent("③Projects?couldn't reach");
    const next = screen.getByRole("button", { name: "Next →" });
    expect(next).toBeDisabled();
    expect(next).toHaveAccessibleDescription("Projects couldn't be loaded.");
  });

  it("First push: done, refused on that row only, then Done", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    api.syncPushAll.mockResolvedValue({
      projects: [
        { project_id: "core", status: "done", nodes: 1284, edges: 3906, at: "2026-11-11T14:02:00" },
        { project_id: "north", status: "refused", reason: "no_access" },
      ],
    });
    wizard(
      { ...signedIn, projects: [
        proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE" }),
        proj("north", "North", { mapped: true, cloud_project_id: NORTH_ID, cloud_project_tag: "NORTH" }),
      ] },
      { initialStep: 4, onDone },
    );
    expect(screen.getByRole("status")).toHaveTextContent("PUSH · NOT YET");
    await user.click(screen.getByRole("button", { name: "Push now" }));
    expect(await screen.findByText("Pushed 1,284 nodes and 3,906 edges to CORE at 14:02.")).toBeInTheDocument();
    expect(screen.getByText("No access to NORTH. Ask an org admin.")).toBeInTheDocument();
    noUuid();
    expect(screen.getByRole("status")).toHaveTextContent("PUSH · 1 DONE · 1 REFUSED");
    expect(screen.queryByText(/seat in Acme was removed/)).not.toBeInTheDocument();
    // After a successful push the wizard offers Done, which goes to the Linked window.
    expect(screen.queryByRole("button", { name: "Push later" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("Push later reveals the command and keeps the wizard open", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    const neverPushed = { ...signedIn, projects: [
      proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE" }),
    ] };
    wizard(neverPushed, { initialStep: 4, onDone });
    expect(screen.queryByText("graphban sync")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Push later" }));
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByTestId("link-wizard")).toBeInTheDocument();
    const block = screen.getByTestId("push-later");
    expect(block).toHaveTextContent("④ stays pending. Run this on the box when you're ready.");
    expect(within(block).getByText("graphban sync")).toBeInTheDocument();
    expect(within(block).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    // Still pushable from here.
    expect(screen.getByRole("button", { name: "Push now" })).toBeInTheDocument();
  });

  it("④ with every project local only says Nothing to push and offers Done", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    wizard({ ...signedIn, projects: [proj("core", "Core"), proj("north", "North")] }, { initialStep: 4, onDone });
    expect(screen.getByText("Nothing to push. Every project is local only.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Push now" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Push later" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("Finish with nothing chosen at ③ lands on Nothing to push and remembers the link is finished", async () => {
    const user = userEvent.setup();
    api.syncSaveProjectMap.mockResolvedValue({ mapping: [] });
    const unmapped = { ...signedIn, projects: [proj("core", "Core"), proj("north", "North")] };
    wizard(unmapped, { initialStep: 3 });
    await screen.findByLabelText("Cloud project for Core");
    await user.click(screen.getByRole("button", { name: "Next →" }));
    expect(await screen.findByText("Nothing to push. Every project is local only.")).toBeInTheDocument();
    expect(localStorage.getItem(LINK_FINISHED_KEY)).toBe("1");
    expect(resumeTarget(unmapped)).toBeNull();
  });

  it("First push: an unknown row is grey, not refused", async () => {
    const user = userEvent.setup();
    api.syncPushAll.mockResolvedValue({ projects: [{ project_id: "core", status: "unknown", reason: "down" }] });
    wizard(signedIn, { initialStep: 4 });
    await user.click(screen.getByRole("button", { name: "Push now" }));
    const row = await screen.findByText(/UNKNOWN/);
    expect(row.className).toContain("text-faint");
    expect(screen.queryByText("☁ REFUSED")).not.toBeInTheDocument();
    expect(screen.queryByText(/No access/)).not.toBeInTheDocument();
  });

  it("First push: seat removed refuses the whole wizard", async () => {
    const user = userEvent.setup();
    api.syncPushAll.mockResolvedValue({
      projects: [{ project_id: "core", status: "refused", reason: "seat_removed" }],
    });
    wizard(signedIn, { initialStep: 4 });
    await user.click(screen.getByRole("button", { name: "Push now" }));
    expect(await screen.findByText(/Your seat in Acme was removed. Nothing is syncing./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · SEAT REMOVED");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("red");
    expect(screen.getByText("☁ REFUSED")).toBeInTheDocument();
    expect(document.querySelector('[data-rail-node="4"]')).toHaveTextContent("④First push✕seat removed");
  });

  it("④ while pushing: every row reads ◌ PUSHING…, the line is muted and pulses", async () => {
    const user = userEvent.setup();
    let resolve!: (v: unknown) => void;
    api.syncPushAll.mockReturnValue(new Promise((r) => (resolve = r)));
    wizard(signedIn, { initialStep: 4 });
    await user.click(screen.getByRole("button", { name: "Push now" }));
    expect(await screen.findByText("◌ PUSHING…")).toBeInTheDocument();
    const st = screen.getByRole("status");
    expect(st.getAttribute("data-tone")).toBe("wait");
    expect(st.className).not.toContain("text-st-done");
    expect(screen.getByTestId("status-dot").getAttribute("data-pulse")).toBe("true");
    expect(document.querySelector('[data-rail-node="4"]')).toHaveTextContent("◌pushing…");
    resolve({ projects: [{ project_id: "core", status: "done", nodes: 1284, edges: 3906 }] });
    expect(await screen.findByText(/Pushed 1,284 nodes/)).toBeInTheDocument();
    expect(screen.queryByText("◌ PUSHING…")).not.toBeInTheDocument();
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("ok");
    expect(document.querySelector('[data-rail-node="4"]')).toHaveTextContent("④First push✓1,284 nodes");
  });

  it("④ every push unknown is grey CONSOLE · UNREACHABLE, never green", async () => {
    const user = userEvent.setup();
    api.syncPushAll.mockResolvedValue({ projects: [{ project_id: "core", status: "unknown", reason: "timed out" }] });
    wizard(signedIn, { initialStep: 4 });
    await user.click(screen.getByRole("button", { name: "Push now" }));
    await screen.findByText(/UNKNOWN/);
    expect(screen.getByRole("status")).toHaveTextContent("CONSOLE · UNREACHABLE");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("grey");
    expect(screen.getByText(/UNKNOWN/)).toHaveTextContent("Couldn't reach c.dev: timed out.");
  });

  it("③ saves the tag from the reachable-projects answer with the mapping (GRPH-1562)", async () => {
    const user = userEvent.setup();
    api.syncSaveProjectMap.mockResolvedValue({ mapping: [] });
    wizard(signedIn, { initialStep: 3 });
    await screen.findByLabelText("Cloud project for Core");
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await waitFor(() => expect(api.syncSaveProjectMap).toHaveBeenCalled());
    expect(api.syncSaveProjectMap.mock.calls[0][0]).toEqual([
      { local_project_id: "core", cloud_project_id: CORE_ID, cloud_project_tag: "CORE" },
    ]);
  });

  it("④ with no stored tag and no projects read shows ☁ ?, never the id", () => {
    wizard(
      { ...signedIn, projects: [proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "" })] },
      { initialStep: 4 },
    );
    expect(screen.getByText("☁ ?")).toBeInTheDocument();
    noUuid();
  });

  it("resumes at the first unfinished step", () => {
    // signed in with a mapped project that never pushed → First push
    wizard({ ...signedIn, projects: [proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE" })] });
    expect(screen.getByRole("button", { name: "Push now" })).toBeInTheDocument();
    expect(document.querySelector('[data-rail-node="4"]')).toHaveAttribute("aria-current", "step");
  });

  describe("resumeTarget (design L3)", () => {
    const pushed = signedIn;
    const neverPushed = { ...signedIn, projects: [proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID })] };
    const nothingMapped = { ...signedIn, projects: [proj("core", "Core")] };
    it("no token and no console URL → ①", () => expect(resumeTarget(base, false)).toBe(1));
    it("no token with a known console URL → ②", () =>
      expect(resumeTarget({ ...base, cloud_url: "https://c.dev" }, false)).toBe(2));
    it("no token with a sign-in pending → ②", () =>
      expect(resumeTarget({ ...base, sign_in_pending: true }, false)).toBe(2));
    it("signed in, nothing mapped, never finished → ③", () => expect(resumeTarget(nothingMapped, false)).toBe(3));
    it("signed in, nothing mapped, finished → Linked", () => expect(resumeTarget(nothingMapped, true)).toBeNull());
    it("mapped but never pushed → ④", () => expect(resumeTarget(neverPushed, false)).toBe(4));
    it("mapped and pushed → Linked", () => expect(resumeTarget(pushed, false)).toBeNull());
    it("a linked box with a sign-in in flight → ② (GRPH-1575)", () =>
      expect(resumeTarget({ ...pushed, sign_in_pending: true }, false)).toBe(2));
    it("a key link still holding its credential → Linked", () =>
      expect(resumeTarget({ ...base, linked: true, cloud_url: "https://c.dev", credential_set: true, legacy: true }, false)).toBeNull());
  });
});

describe("resuming a sign-in in flight (GRPH-1575)", () => {
  const inFlight: SyncStatus = {
    ...signedIn,
    sign_in_pending: true,
    pending_sign_in: {
      console_url: "https://c.dev", user_code: "MX54-RS6Y", verification_url: "https://c.dev/link",
      expires_at: null, interval: 0.01,
    },
  };

  it("opens at ② with the pending code, waiting, and polls — never a new code", async () => {
    wizard(inFlight);
    expect(document.querySelector('[data-rail-node="2"]')).toHaveAttribute("aria-current", "step");
    expect(screen.getByTestId("user-code")).toHaveTextContent("MX54-RS6Y");
    expect(screen.getByText("Waiting for you to approve it in the console.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · WAITING");
    expect(screen.queryByRole("button", { name: "Get a sign-in code" })).toBeNull();
    await waitFor(() => expect(api.syncLinkPoll).toHaveBeenCalled());
    expect(api.syncLinkStart).not.toHaveBeenCalled();
  });

  it("Sign in again (opened at ②) resumes the code in flight", () => {
    wizard(inFlight, { initialStep: 2 });
    expect(screen.getByTestId("user-code")).toHaveTextContent("MX54-RS6Y");
  });

  it("a code approved while no tab was open completes on the first poll and moves to ③", async () => {
    api.syncLinkPoll.mockResolvedValue({ status: "approved", org: "Walk Org", email: "m2@walk.dev" });
    const onLinked = vi.fn();
    // A long interval: only the immediate first poll can answer within the test.
    wizard({ ...inFlight, pending_sign_in: { ...inFlight.pending_sign_in!, interval: 60 } }, { onLinked });
    await waitFor(() =>
      expect(document.querySelector('[data-rail-node="3"]')).toHaveAttribute("aria-current", "step"),
    );
    expect(api.syncLinkPoll).toHaveBeenCalledTimes(1);
    expect(onLinked).toHaveBeenCalled();
    expect(document.querySelector('[data-rail-node="2"]')).toHaveAttribute("data-state", "done");
  });

  it("the link controller keeps the wizard open once the approval clears the pending flag", () => {
    function Probe({ status }: { status: SyncStatus }) {
      const flow = useLinkFlow(status);
      return <span data-testid="probe">{flow.wizard ? `wizard@${flow.initialStep}` : "linked"}</span>;
    }
    const qc = new QueryClient();
    const ui = (st: SyncStatus) => (
      <QueryClientProvider client={qc}>
        <Probe status={st} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui(inFlight));
    expect(screen.getByTestId("probe")).toHaveTextContent("wizard@2");
    // Approved: signed in, mapped and pushed — on its own that is the Linked window.
    rerender(ui(signedIn));
    expect(screen.getByTestId("probe")).toHaveTextContent("wizard@2");
  });
});

describe("LinkedPanel", () => {
  const noop = () => {};
  const panel = (status: SyncStatus) =>
    render(
      <LinkedPanel status={status} orgHref="https://c.dev" onChooseProjects={noop} onSignIn={noop} onUnlink={noop} />,
    );

  it("reads who it is linked as, the projects, and the actions", () => {
    panel(signedIn);
    const who = document.querySelector("[data-linked-as]")!;
    expect(who).toHaveTextContent("This deployment is linked to Acme as ann@acme.dev.");
    expect(within(who as HTMLElement).getByText("Acme").tagName).toBe("STRONG");
    expect(within(who as HTMLElement).getByText("ann@acme.dev").tagName).toBe("CODE");
    expect(screen.getByText("☁ CORE")).toBeInTheDocument();
    noUuid();
    expect(screen.getByText("1 project is local only.")).toBeInTheDocument();
    for (const n of ["Choose projects", "Sign in again", "Unlink"])
      expect(screen.getByRole("button", { name: n })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the org →" })).toBeInTheDocument();
    expect(screen.getByText("Stops all sync. Every project becomes local only. Cloud items remain.")).toBeInTheDocument();
  });

  it("a row with no stored tag reads ☁ ? with the tooltip, never the id", () => {
    panel({ ...signedIn, projects: [proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "" })] });
    const tag = screen.getByText("☁ ?");
    expect(tag).toHaveAttribute("title", "Couldn't read the cloud project's tag");
    noUuid();
  });

  it("seat removed: red line, every row REFUSED, Sign in again note", () => {
    panel({ ...signedIn, state: "refused" });
    const line = screen.getByText("Your seat in Acme was removed. Nothing is syncing.");
    expect(line.className).toContain("text-st-blocked");
    expect(screen.getByText("☁ REFUSED")).toBeInTheDocument();
    expect(screen.getByText(/Anyone with a seat in Acme can sign this box in/)).toBeInTheDocument();
    // The row shows only ☁ REFUSED: no ☁ CORE tag beside it, no SYNCED time.
    const row = document.querySelector("[data-synced-project]")!;
    expect(row.textContent).toBe("Core☁ REFUSED");
    expect(row).not.toHaveTextContent("☁ CORE");
    expect(row).not.toHaveTextContent("SYNCED");
  });

  it("an unknown row is grey, never REFUSED", () => {
    panel({ ...signedIn, state: "unknown", projects: [
      proj("core", "Core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE", last_push_result: "unknown" }),
    ] });
    expect(screen.getByText("☁ UNKNOWN").className).toContain("text-faint");
    expect(screen.queryByText("☁ REFUSED")).not.toBeInTheDocument();
  });

  it("the old-key relink warning is muted — not amber, not red", () => {
    panel({ ...signedIn, signed_in: false, legacy: true, state: "ok" });
    const warn = screen.getByTestId("relink-warning");
    expect(warn).toHaveTextContent(
      "This deployment still uses an old link key. Sign in again to link it to your seat. It keeps syncing until then.",
    );
    expect(warn.className).toContain("text-faint");
    expect(warn.className).not.toMatch(/st-blocked|st-review|amber|red/);
  });

  it("no warning once signed in; old key refused is a red line", () => {
    const { unmount } = panel(signedIn);
    expect(screen.queryByTestId("relink-warning")).not.toBeInTheDocument();
    unmount();
    panel({ ...signedIn, signed_in: false, legacy: true, state: "refused" });
    expect(screen.queryByTestId("relink-warning")).not.toBeInTheDocument();
    const line = screen.getByText("This link used a key. Sign in to keep syncing.");
    expect(line.className).toContain("text-st-blocked");
  });

  it("an env-baked link can still sign in again and choose projects; only Unlink is off", () => {
    render(
      <LinkedPanel status={{ ...signedIn, legacy: true, signed_in: false, state: "refused", source: "env" }}
        unlinkable={false} onChooseProjects={noop} onSignIn={noop} onUnlink={noop} />,
    );
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Choose projects" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Unlink" })).toBeDisabled();
  });

  it("a pushed row reads core → ☁ CORE · ☁ SYNCED <time>", () => {
    const at = "2026-11-12T14:02:00Z";
    const hhmm = new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    panel({ ...signedIn, projects: [
      proj("core", "core", { mapped: true, cloud_project_id: CORE_ID, cloud_project_tag: "CORE", last_push_result: "ok", last_push_at: at }),
    ] });
    expect(document.querySelector("[data-synced-project]")).toHaveTextContent(`core→☁ CORE· ☁ SYNCED ${hhmm}`);
  });

  it("confirmUnlink asks first, with the same copy, then calls back", async () => {
    const user = userEvent.setup();
    const onUnlink = vi.fn();
    render(<LinkedPanel status={signedIn} confirmUnlink onChooseProjects={noop} onSignIn={noop} onUnlink={onUnlink} />);
    await user.click(screen.getByRole("button", { name: "Unlink" }));
    expect(onUnlink).not.toHaveBeenCalled();
    const confirm = screen.getByRole("alertdialog", { name: "Unlink this deployment" });
    expect(confirm).toHaveTextContent("Stops all sync. Every project becomes local only. Cloud items remain.");
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Unlink" }));
    await user.click(screen.getByRole("button", { name: "Unlink this deployment" }));
    expect(onUnlink).toHaveBeenCalledTimes(1);
  });

  it("Unlink calls back", async () => {
    const user = userEvent.setup();
    const onUnlink = vi.fn();
    render(<LinkedPanel status={signedIn} onChooseProjects={noop} onSignIn={noop} onUnlink={onUnlink} />);
    await user.click(screen.getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(onUnlink).toHaveBeenCalled());
  });
});
