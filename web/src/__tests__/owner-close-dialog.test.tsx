import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OwnerCloseDialog } from "@/features/tracker/OwnerCloseDialog";
import { api } from "@/lib/api";
import type { Item } from "@/lib/types";

const item = { id: "GRPH-9", title: "t", status: "review" } as unknown as Item;

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OwnerCloseDialog item={item} open onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("OwnerCloseDialog Verified mode", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "item").mockResolvedValue({ ...item, evidence: [] } as never);
  });

  it("shows its fields and blocks an empty note", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("radio", { name: "Verified" }));
    expect(screen.getByText(/Records the revision you checked and where/)).toBeTruthy();
    await user.type(screen.getByPlaceholderText("the revision you verified"), "abc123");
    expect(screen.getByPlaceholderText("where you checked it")).toBeTruthy();
    expect(screen.getByPlaceholderText("walk log or artifact")).toBeTruthy();
    const submit = screen.getByRole("button", { name: "Close as verified" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });

  it("sends the exact body and shows a 422", async () => {
    const user = userEvent.setup();
    const spy = vi.spyOn(api, "ownerClose").mockRejectedValue(new Error("note too vague"));
    mount();
    await user.click(screen.getByRole("radio", { name: "Verified" }));
    await user.type(screen.getByPlaceholderText("the revision you verified"), "abc123");
    await user.type(screen.getByPlaceholderText("where you checked it"), "staging walk");
    await user.type(screen.getByPlaceholderText("walk log or artifact"), "https://x.test/log");
    await user.click(screen.getByRole("button", { name: "Close as verified" }));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("GRPH-9", {
        kind: "verified",
        commit: "abc123",
        note: "staging walk",
        url: "https://x.test/log",
      }),
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
