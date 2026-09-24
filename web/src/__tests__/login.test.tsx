import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import loginSrc from "@/features/auth/LoginPage.tsx?raw";

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
}));

vi.mock("@/features/auth/AuthContext", async (orig) => {
  const actual = await orig<typeof import("@/features/auth/AuthContext")>();
  return {
    ...actual,
    useAuth: () => ({
      user: null,
      loading: false,
      login: auth.login,
      register: auth.register,
      logout: vi.fn(),
      completePasswordReset: vi.fn(),
    }),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  auth.login.mockRejectedValue(new Error("401"));
});

async function renderLogin() {
  const { LoginPage } = await import("@/features/auth/LoginPage");
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <LoginPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("login page (GRPH-912)", () => {
  it("associates the Email label so getByLabel works", async () => {
    await renderLogin();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("does not show invalid-credentials copy on empty submit", async () => {
    await renderLogin();
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(screen.queryByText("Invalid email or password.")).not.toBeInTheDocument();
    expect(auth.login).not.toHaveBeenCalled();
  });

  it("announces failed credentials and marks both fields invalid", async () => {
    await renderLogin();
    await userEvent.type(screen.getByLabelText("Email"), "wrong@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "bad-password");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid email or password.");
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(auth.login).toHaveBeenCalledWith("wrong@example.com", "bad-password"));
  });

  it("sabotage: an unassociated label would fail the getByLabel test above", () => {
    expect(loginSrc).toMatch(/htmlFor="login-email"/);
    expect(loginSrc).toMatch(/htmlFor="login-password"/);
    expect(loginSrc).not.toMatch(
      /<label className="mb-1\.5 block font-mono text-\[10px\] uppercase tracking-wide text-faint">\s*\{label\}/,
    );
  });
});
