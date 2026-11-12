import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LinkedPanel } from "@/features/organization/LinkedPanel";
import { LinkToOrgWizard } from "@/features/organization/LinkToOrgWizard";
import type { SyncProjectState, SyncStatus } from "@/lib/types";

const api = vi.hoisted(() => ({
  syncLinkStart: vi.fn(),
  syncLinkPoll: vi.fn(),
  syncCloudProjects: vi.fn(),
  syncSaveProjectMap: vi.fn(),
  syncPushAll: vi.fn(),
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
  api.syncLinkStart.mockResolvedValue({
    status: "started", user_code: "WXYZ-1234", verification_url: "https://c.dev/link", interval: 0.01,
  });
  api.syncLinkPoll.mockResolvedValue({ status: "pending" });
  api.syncCloudProjects.mockResolvedValue({
    status: "ok", projects: [{ id: CORE_ID, tag: "CORE", name: "Core project" }], mapping: [],
  });
});

describe("LinkToOrgWizard", () => {
  it("shows the rail and a waiting status line, with a pulsing dot", async () => {
    wizard(base);
    for (const l of ["① Console", "② Sign in", "③ Projects", "④ First push"])
      expect(screen.getByText(l)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · WAITING");
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
    expect(screen.getByText(/SYNC_CLOUD_URL/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    expect(api.syncLinkStart).toHaveBeenCalledWith("https://baked.dev");
  });

  it("keeps the console URL editable when no SYNC_CLOUD_URL is set", () => {
    wizard(base);
    expect(screen.getByLabelText("Console URL")).not.toHaveAttribute("readonly");
    expect(screen.queryByText(/SYNC_CLOUD_URL/)).not.toBeInTheDocument();
  });

  it("shows the code, copy, open, expiry and terminal command", async () => {
    const user = userEvent.setup();
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    expect(await screen.findByTestId("user-code")).toHaveTextContent("WXYZ-1234");
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the console →" })).toHaveAttribute("href", "https://c.dev/link");
    expect(screen.getByRole("button", { name: "Copy link" })).toBeInTheDocument();
    expect(screen.getByText("Expires in 10 minutes.")).toBeInTheDocument();
    expect(screen.getByText(/graphban link/)).toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    const msg = await screen.findByText(text);
    expect(msg.className).toContain("text-st-blocked");
    expect(screen.getByRole("status")).toHaveTextContent("SIGN-IN · REFUSED");
    expect(screen.getByRole("button", { name: "Get a new code" })).toBeInTheDocument();
  });

  it("an unreachable console is grey with Try again, never refused", async () => {
    const user = userEvent.setup();
    api.syncLinkPoll.mockResolvedValue({ status: "unknown", reason: "down" });
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("CONSOLE · UNREACHABLE");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("grey");
    expect(screen.queryByText(/refused/i)).not.toBeInTheDocument();
  });

  it("approval reads SIGNED IN · <ORG>, then Projects, each defaulting to Local only", async () => {
    const user = userEvent.setup();
    api.syncLinkPoll.mockResolvedValue({ status: "approved", org: "Acme", email: "ann@acme.dev" });
    wizard(base);
    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    expect(await screen.findByText("Signed in to Acme as ann@acme.dev.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("SIGNED IN · ACME");
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("ok");
    await user.click(screen.getByRole("button", { name: "Choose projects →" }));
    expect(await screen.findByLabelText("Cloud project for Core")).toHaveValue("");
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
    expect(await screen.findByText("You don't have access to any projects in Acme yet. Ask an org admin.")).toBeInTheDocument();
    unmount();

    api.syncCloudProjects.mockResolvedValue({ status: "unknown", reason: "down" });
    wizard(signedIn, { initialStep: 3 });
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(/don't have access/)).not.toBeInTheDocument();
    expect(screen.getByRole("status").getAttribute("data-tone")).toBe("grey");
  });

  it("First push: done, refused on that row only, and push later names graphban sync", async () => {
    const user = userEvent.setup();
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
      { initialStep: 4 },
    );
    expect(screen.getByRole("status")).toHaveTextContent("PUSH · NOT YET");
    expect(screen.getByText(/graphban sync/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Push now" }));
    expect(await screen.findByText("Pushed 1,284 nodes and 3,906 edges to CORE at 14:02.")).toBeInTheDocument();
    expect(screen.getByText("No access to NORTH. Ask an org admin.")).toBeInTheDocument();
    noUuid();
    expect(screen.getByRole("status")).toHaveTextContent("PUSH · 1 DONE · 1 REFUSED");
    expect(screen.queryByText(/seat in Acme was removed/)).not.toBeInTheDocument();
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
    expect(await screen.findByText("Your seat in Acme was removed. Nothing is syncing.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
  });

  it("③ saves the tag from the reachable-projects answer with the mapping (GRPH-1562)", async () => {
    const user = userEvent.setup();
    api.syncSaveProjectMap.mockResolvedValue({ mapping: [] });
    wizard(signedIn, { initialStep: 3 });
    await screen.findByLabelText("Cloud project for Core");
    await user.click(screen.getByRole("button", { name: "Continue" }));
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
    wizard(signedIn);
    // signed in with a mapped project → First push
    expect(screen.getByRole("button", { name: "Push now" })).toBeInTheDocument();
    expect(screen.getByText("④ First push")).toHaveAttribute("aria-current", "step");
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
    expect(screen.getByText("This deployment is linked to Acme as ann@acme.dev.")).toBeInTheDocument();
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

  it("Unlink calls back", async () => {
    const user = userEvent.setup();
    const onUnlink = vi.fn();
    render(<LinkedPanel status={signedIn} onChooseProjects={noop} onSignIn={noop} onUnlink={onUnlink} />);
    await user.click(screen.getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(onUnlink).toHaveBeenCalled());
  });
});
