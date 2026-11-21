import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProfileView, HANDLE_CHECK_DELAY_MS } from "@/features/profile/ProfileView";
import { api } from "@/lib/api";
import { dotFill } from "@/lib/dot";
import type { User } from "@/lib/types";

const BASE: User = {
  id: "u1", name: "Ada Lovelace", handle: "ada", email: "ada@x.io", avatar: "#c6f24e", initials: "AL",
};
const setUser = vi.fn();
let current: User = BASE;

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: current, setUser }),
}));
vi.mock("@/lib/api", () => ({
  api: {
    config: vi.fn(async () => ({ hosted_mode: false })),
    myMemberships: vi.fn(async () => []),
    handleCheck: vi.fn(),
    updateMe: vi.fn(),
    changePassword: vi.fn(),
  },
}));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ProfileView /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  current = BASE;
});

describe("dot renderer", () => {
  it("draws solid without avatar2 or pattern, ombre and pattern otherwise", () => {
    expect(dotFill({ colour: "#c6f24e" })).toBe("#c6f24e");
    expect(dotFill({ colour: "#c6f24e", colour2: "#38bdf8", pattern: "none" })).toContain("linear-gradient");
    expect(dotFill({ colour: "#c6f24e", pattern: "stripes" })).toContain("repeating-linear-gradient");
  });
});

describe("ProfileView", () => {
  it("shows a taken handle with its org and disables Save", async () => {
    vi.mocked(api.handleCheck).mockResolvedValue({ handle: "xyz", available: false, code: "handle_taken", org: "Acme" });
    mount();
    const user = userEvent.setup();
    const field = screen.getByLabelText("Handle");
    await user.clear(field);
    await user.type(field, "xyz");
    await user.tab();
    const found = await screen.findAllByText("@xyz is taken in Acme", {}, { timeout: HANDLE_CHECK_DELAY_MS + 1500 });
    expect(found.length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("says why an invalid handle is refused without calling the server", async () => {
    mount();
    const user = userEvent.setup();
    const field = screen.getByLabelText("Handle");
    await user.clear(field);
    await user.type(field, "ab");
    await user.tab();
    expect(screen.getAllByText(/3–24 characters/).length).toBeGreaterThan(0);
    expect(api.handleCheck).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves changes together, then discard restores saved values", async () => {
    vi.mocked(api.updateMe).mockResolvedValue({ ...BASE, name: "Ada L", avatar_pattern: "stripes" });
    mount();
    const user = userEvent.setup();
    const name = screen.getByLabelText("Name");
    await user.clear(name);
    await user.type(name, "Ada L");
    await user.click(screen.getByRole("button", { name: "Pattern Diagonal stripes" }));
    expect(screen.getByText(/Changed: name, pattern/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateMe).toHaveBeenCalledWith({ name: "Ada L", avatar_pattern: "stripes" }));
    expect(setUser).toHaveBeenCalledWith(expect.objectContaining({ name: "Ada L", avatar_pattern: "stripes" }));
  });

  it("Discard restores the saved values", async () => {
    mount();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "x");
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Ada Lovelace");
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("puts Password on the page and lists the local account rows", async () => {
    mount();
    expect(await screen.findByText("Projects on this box")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change password" })).toBeInTheDocument();
  });
});
