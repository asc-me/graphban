import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import loginSrc from "@/features/auth/LoginPage.tsx?raw";
import { contrastRatio, TOKENS } from "./design-tokens.test";

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

describe("login page (GRPH-921)", () => {
  it("tagline copy meets the 4.5:1 contrast floor on the page canvas", () => {
    const ratio = contrastRatio(TOKENS["color-muted-2"]!, TOKENS["color-bg"]!);
    expect(ratio).toBeGreaterThanOrEqual(4.5);
    expect(loginSrc).toMatch(
      /text-muted-2[\s\S]{0,120}AGENT MEMORY · LINEAR EXECUTION/,
    );
    expect(loginSrc).not.toMatch(
      /text-faint[\s\S]{0,120}AGENT MEMORY · LINEAR EXECUTION/,
    );
  });

  it("forgot password is a named control with helper as aria-describedby when email is empty", async () => {
    await renderLogin();
    const forgot = screen.getByRole("button", { name: /forgot your password/i });
    expect(forgot).toBeDisabled();
    expect(forgot).toHaveAttribute("aria-describedby", "login-forgot-hint");
    expect(screen.getByText(/enter your email above/i)).toBeVisible();
  });

  it("enables forgot password once email is filled", async () => {
    await renderLogin();
    const forgot = screen.getByRole("button", { name: /forgot your password/i });
    expect(forgot).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Email"), "planner@example.com");
    expect(forgot).toBeEnabled();
    expect(forgot).not.toHaveAttribute("aria-describedby");
  });

  it("sabotage: static forgot copy without a button would fail the control tests above", () => {
    expect(loginSrc).toMatch(/Forgot your password\?/);
    expect(loginSrc).toMatch(/type="button"/);
    expect(loginSrc).toMatch(/aria-describedby=\{!email\.trim\(\) \? FORGOT_HINT_ID : undefined\}/);
  });
});
