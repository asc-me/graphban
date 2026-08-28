import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The emailed reset link lands somewhere, and the request form keeps the server's promise
 * (GRPH-570, the UI half of GRPH-359).
 *
 * The API answers `202` with one identical sentence whether or not the address is registered,
 * deliberately — otherwise it is an account-enumeration oracle. **That property is trivially
 * broken from the UI side** by a well-meaning "we couldn't find that account" message, so it
 * is asserted here rather than assumed to survive contact with a form.
 */
const calls = vi.hoisted(() => ({ request: [] as string[], confirm: [] as unknown[] }));
const outcome = vi.hoisted(() => ({
  confirm: null as Error | null,
  request: null as Error | null,
}));

// Partial mock: only the reset calls are replaced, so the rest of the module (`hasSession`,
// token storage, the request helper) stays real and the AuthProvider boots the way it does in
// the app rather than against a hand-built stub of itself.
vi.mock("@/lib/api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  api: {
    ...((await orig<{ api: Record<string, unknown> }>()).api),
    requestPasswordReset: async (email: string) => {
      calls.request.push(email);
      // The real endpoint cannot fail for an UNKNOWN address — it answers 202 either way —
      // but it can fail for transport or the rate limit, and that is the branch a UI is
      // tempted to turn into a message about the address.
      if (outcome.request) throw outcome.request;
    },
    confirmPasswordReset: async (token: string, pw: string) => {
      calls.confirm.push([token, pw]);
      if (outcome.confirm) throw outcome.confirm;
      return { id: "u1", name: "Locked Out", email: "locked@example.com" };
    },
  },
}));

beforeEach(() => {
  calls.request.length = 0;
  calls.confirm.length = 0;
  outcome.confirm = null;
  outcome.request = null;
});

async function resetPageAt(search: string) {
  const { ResetPasswordPage } = await import("@/features/auth/ResetPasswordPage");
  const { AuthProvider } = await import("@/features/auth/AuthContext");
  render(
    <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={[`/reset-password${search}`]}>
      <AuthProvider>
        <Routes>
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/" element={<div>signed in</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the reset link's landing page", () => {
  it("spends the token and lands the user signed in", async () => {
    await resetPageAt("?token=tok_abc");
    await userEvent.type(screen.getByLabelText("New password"), "a long enough password");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    await waitFor(() => expect(calls.confirm).toEqual([["tok_abc", "a long enough password"]]));
    // Landing signed in is the point: the server revoked every other session and issued a
    // fresh pair, so a login form here would ask the user to prove what they just proved.
    await waitFor(() => expect(screen.getByText("signed in")).toBeInTheDocument());
  });

  it("offers the way forward when the link is spent", async () => {
    outcome.confirm = new Error("400");
    await resetPageAt("?token=tok_spent");
    await userEvent.type(screen.getByLabelText("New password"), "a long enough password");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    const msg = await screen.findByText(/no longer valid/i);
    // "Invalid token" is an error the user cannot act on. The one thing they CAN do is ask
    // for another, so that is what the page says.
    expect(msg.textContent?.toLowerCase()).toContain("ask for a new one");
  });

  it("says so when the link arrived without a token", async () => {
    await resetPageAt("");
    expect(await screen.findByText(/missing its token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });
});

describe("asking for a reset from the sign-in form", () => {
  const emailInput = () =>
    document.querySelector('input[type="email"]') as HTMLInputElement;

  async function loginPage() {
    const { LoginPage } = await import("@/features/auth/LoginPage");
    const { AuthProvider } = await import("@/features/auth/AuthContext");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <AuthProvider>
            <LoginPage />
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("says the same thing for a registered and an unregistered address", async () => {
    // THE ONE THAT MATTERS. The server refuses to distinguish these; a UI that renders a
    // different message for one of them hands back the oracle.
    await loginPage();
    await userEvent.type(emailInput(), "known@example.com");
    await userEvent.click(screen.getByRole("button", { name: /forgot your password/i }));
    const first = (await screen.findByText(/if an account exists/i)).textContent;

    // A real unmount between the two renders. `document.body.innerHTML = ""` leaves React
    // holding a tree it thinks is mounted, and the second `findByText` can then match the
    // FIRST render's node — which would make this test pass by reading its own earlier output.
    cleanup();
    await loginPage();
    await userEvent.type(emailInput(), "nobody@example.com");
    await userEvent.click(screen.getByRole("button", { name: /forgot your password/i }));
    const second = (await screen.findByText(/if an account exists/i)).textContent;

    expect(first).toBe(second);
    expect(calls.request).toEqual(["known@example.com", "nobody@example.com"]);
  });

  it("does not promise delivery it cannot observe", async () => {
    // `send_email` falls back to an in-process outbox and the server returns 202 rather than
    // 200 for exactly this reason. "Email sent" would be a claim neither end can make.
    await loginPage();
    await userEvent.type(emailInput(), "known@example.com");
    await userEvent.click(screen.getByRole("button", { name: /forgot your password/i }));

    const msg = await screen.findByText(/if an account exists/i);
    expect(msg.textContent).toMatch(/check your inbox/i);
    expect(msg.textContent).not.toMatch(/\bemail sent\b/i);
  });

  it("says the same thing even when the request itself fails", async () => {
    // THE MUTATION THAT SURVIVED THE FIRST PASS. The two tests above never reach the `catch`
    // branch, because the mocked request cannot fail — so a UI that rendered "No account
    // found for that address" in there passed both of them, and the oracle was back with the
    // suite green. A failing call is the only way that branch runs.
    outcome.request = new Error("429");
    await loginPage();
    await userEvent.type(emailInput(), "known@example.com");
    await userEvent.click(screen.getByRole("button", { name: /forgot your password/i }));

    const msg = await screen.findByText(/if an account exists/i);
    expect(msg).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/no account|not found|unknown|does not exist/i);
  });
});
