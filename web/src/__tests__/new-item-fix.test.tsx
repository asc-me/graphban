import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { NewItemDialog } from "@/features/tracker/NewItemDialog";

const project = {
  id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "",
  share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "",
};
let servedLine: { id: string; title: string } | null = null;

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    me: vi.fn(async () => ({ name: "Alex" })),
    prds: vi.fn(async () => [{ id: "P1", title: "Big PRD" }]),
    items: vi.fn(async () => [{ id: "AL-9", title: "Blocked thing", prd_id: "P1" }, { id: "AL-1", title: "Other", prd_id: null }]),
    line: vi.fn(async () => servedLine),
    createLine: vi.fn(async () => { servedLine = { id: "L1", title: "Maintenance" }; return servedLine; }),
    createItem: vi.fn(async () => ({ id: "AL-10" })),
  },
}));

async function setup() {
  const { api } = await import("@/lib/api");
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ProjectProvider>
          <NewItemDialog />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await user.click(await screen.findByRole("button", { name: /New item/ }));
  await user.type(screen.getByPlaceholderText("Title"), "A fix");
  await user.selectOptions(screen.getByLabelText("Kind"), "fix");
  return { api, user };
}

describe("New item dialog — filing a fix", () => {
  beforeEach(() => { vi.clearAllMocks(); servedLine = null; });

  it("offers two scopes and will not submit with neither; Security only for a fix", async () => {
    const { user } = await setup();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Create item" })).toBeDisabled();
    expect(screen.getByLabelText(/Severity: Security/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Kind"), "feature");
    expect(screen.queryByLabelText(/Severity: Security/)).not.toBeInTheDocument();
  });

  it("blocking requires the blocked item and sends blocks", async () => {
    const { api, user } = await setup();
    await user.click(screen.getByLabelText("Blocking a PRD"));
    await user.selectOptions(screen.getByLabelText("PRD"), "P1");
    expect(screen.getByRole("button", { name: "Create item" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Blocked item"), "AL-9");
    await user.click(screen.getByRole("button", { name: "Create item" }));
    await waitFor(() => expect(api.createItem).toHaveBeenCalled());
    expect(vi.mocked(api.createItem).mock.calls[0][1]).toMatchObject({ kind: "fix", prd_id: "P1", blocks: "AL-9" });
  });

  it("with no line, creates it in place and files on it", async () => {
    const { api, user } = await setup();
    await user.click(screen.getByLabelText("On the maintenance line"));
    await user.click(await screen.findByRole("button", { name: "Create the maintenance line" }));
    await screen.findByText(/Filed on Maintenance/);
    await user.click(screen.getByLabelText(/Severity: Security/));
    await user.click(screen.getByRole("button", { name: "Create item" }));
    await waitFor(() => expect(api.createItem).toHaveBeenCalled());
    expect(vi.mocked(api.createItem).mock.calls[0][1]).toMatchObject({ line_id: "L1", severity: "security" });
  });

  it("shows a server refusal verbatim and stays open", async () => {
    servedLine = { id: "L1", title: "Maintenance" };
    const { api, user } = await setup();
    vi.mocked(api.createItem).mockRejectedValueOnce(new Error(JSON.stringify({ detail: "a fix needs a scope: nope" })));
    await user.click(screen.getByLabelText("On the maintenance line"));
    await screen.findByText(/Filed on Maintenance/);
    await user.click(screen.getByRole("button", { name: "Create item" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("a fix needs a scope: nope");
    expect(screen.getByPlaceholderText("Title")).toBeInTheDocument();
  });
});

describe("New item dialog — ?new=1", () => {
  it("opens only the instance that reads the query, so an empty tracker shows one dialog", async () => {
    window.history.pushState({}, "", "/?new=1");
    try {
      render(
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter>
            <ProjectProvider>
              <NewItemDialog openFromQuery />
              <NewItemDialog icon={false} />
            </ProjectProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      expect(await screen.findAllByPlaceholderText("Title")).toHaveLength(1);
    } finally {
      window.history.pushState({}, "", "/");
    }
  });
});
