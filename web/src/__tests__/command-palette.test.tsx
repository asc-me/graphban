import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import paletteSrc from "../components/shell/CommandPalette.tsx?raw";
import { CommandPalette } from "@/components/shell/CommandPalette";
import { TOP_BAR_JUMP_PLACEHOLDER, TopBar } from "@/components/shell/TopBar";
import { ProjectProvider } from "@/features/ProjectContext";
import { TrackerView } from "@/features/tracker/TrackerView";
import type { Item, PrdSummary } from "@/lib/types";

const project = {
  id: "core",
  tag: "CORE",
  name: "Core",
  accent: "#a78bfa",
  visibility: "private",
  description: "",
  share_global_memory: false,
  auto_extract: true,
  mcp_enabled: true,
  embed_model: "",
};

const items: Item[] = [
  {
    id: "AL-01",
    project_id: "core",
    title: "Palette target item",
    description: "Opened from the command palette.",
    status: "in_progress",
    tags: [],
    touchpoints: [],
    effort: 3,
    sort_order: 0,
    blocker: "",
    bounce_reason: "",
    date: "Sep 23",
    reporter: { name: "Alex", handle: "alex", avatar: "#a78bfa" },
    pr: null,
    github_url: "",
    evidence: [],
    assignee: "",
    claimed_by: null,
    prd_id: null,
    prd_section: "",
    fidelity: "low",
    reach: "repo",
    created_at: "",
    updated_at: "",
  },
];

const prds: PrdSummary[] = [
  {
    id: "PRD-1",
    title: "Sample PRD",
    status: "draft",
    version: "1.0",
    linked: [],
    updated: "Sep 1",
  },
];

let servedItems: Item[] = items;
let servedPrds: PrdSummary[] = prds;

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({
    user: { id: "u1", name: "Alex", handle: "alex", initials: "AC", avatar: "#a78bfa" },
    loading: false,
    login: vi.fn(),
    register: vi.fn(),
    completePasswordReset: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [project]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    items: vi.fn(async () => servedItems),
    prds: vi.fn(async () => servedPrds),
    counts: vi.fn(async () => ({ items: servedItems.length, items_in_progress: 1, requests: 0, review: 0 })),
    apiKeys: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 57, tools: [] })),
    shards: vi.fn(async () => []),
    updateItem: vi.fn(async (id: string, body: Partial<Item>) => ({ ...items[0], id, ...body })),
    reorderItems: vi.fn(async () => items),
    assistantProviders: vi.fn(async () => ({ providers: [] })),
    assistantThreads: vi.fn(async () => []),
    links: vi.fn(async () => []),
  },
}));

function PaletteHarness({
  initialPath = "/tracker",
  startOpen = false,
}: {
  initialPath?: string;
  startOpen?: boolean;
}) {
  const [open, setOpen] = React.useState(startOpen);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[initialPath]}>
        <ProjectProvider>
          <TopBar agentOpen={false} onToggleAgent={() => {}} onOpenPalette={() => setOpen(true)} />
          <CommandPalette open={open} onOpenChange={setOpen} />
          <Routes>
            <Route path="/tracker" element={<TrackerView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("command palette (GRPH-911)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    servedItems = items;
    servedPrds = prds;
    Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
  });

  it("opens with Meta+k, typing an item id lands on Tracker with the detail panel open", async () => {
    const user = userEvent.setup();
    render(<PaletteHarness />);

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const palette = await screen.findByRole("dialog", { name: "Command palette" });
    const paletteInput = within(palette).getByLabelText("Command palette search");
    await within(palette).findByText("AL-01");
    await user.clear(paletteInput);
    await user.type(paletteInput, "AL-01");
    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Palette target item" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
    expect(screen.getByText("Opened from the command palette.")).toBeInTheDocument();
  });

  it("does not claim memory search in the top-bar placeholder", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <TopBar agentOpen={false} onToggleAgent={() => {}} onOpenPalette={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const field = screen.getByPlaceholderText(TOP_BAR_JUMP_PLACEHOLDER);
    expect(field).toHaveAttribute("placeholder", TOP_BAR_JUMP_PLACEHOLDER);
    expect(field.getAttribute("placeholder") ?? "").not.toMatch(/memory/i);
  });

  it("shows echoed miss copy for an unmatched query", async () => {
    const user = userEvent.setup();
    render(<PaletteHarness startOpen />);
    const palette = await screen.findByRole("dialog", { name: "Command palette" });
    const paletteInput = within(palette).getByLabelText("Command palette search");
    await within(palette).findByText("AL-01");
    await user.type(paletteInput, "zzznomatch");
    expect(within(palette).getByText('No items match "zzznomatch"')).toBeInTheDocument();
    expect(within(palette).getByText('No PRDs match "zzznomatch"')).toBeInTheDocument();
  });

  it("shows looked-at empty copy when the project has zero items", async () => {
    servedItems = [];
    render(<PaletteHarness startOpen />);
    expect(await screen.findByText("No items in this project")).toBeInTheDocument();
  });

  it("opens the palette when the top-bar field is focused", async () => {
    const user = userEvent.setup();
    render(<PaletteHarness />);
    await user.click(screen.getByPlaceholderText(TOP_BAR_JUMP_PLACEHOLDER));
    expect(await screen.findByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
  });

  it("sabotage: removing the keybinding fails the palette contract", () => {
    expect(paletteSrc).toMatch(/isCommandPaletteShortcut/);
    expect(paletteSrc).toMatch(/e\.key === "k"/);
    expect(paletteSrc).toMatch(/metaKey \|\| e\.ctrlKey/);
    expect(paletteSrc).toMatch(/addEventListener\("keydown"/);
  });
});
