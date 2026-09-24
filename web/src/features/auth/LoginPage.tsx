import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";

import { api } from "@/lib/api";

import { useAuth } from "./AuthContext";

type Mode = "signin" | "signup";

const SIGNIN_ERROR = "Invalid email or password.";

export function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = React.useState<Mode>("signin");
  const [name, setName] = React.useState("");
  const [handle, setHandle] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [sent, setSent] = React.useState(false);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const isSignup = mode === "signup";
  const authFailed = !isSignup && error === SIGNIN_ERROR;

  function clearError() {
    if (error) setError("");
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }

    setBusy(true);
    setError("");
    try {
      if (isSignup) {
        await register(name.trim(), email.trim(), handle.trim(), password);
      } else {
        await login(email.trim(), password);
      }
    } catch (err) {
      setError(
        isSignup
          ? messageFor(err, "Could not create account. That email or handle may already be in use.")
          : SIGNIN_ERROR,
      );
    } finally {
      setBusy(false);
    }
  }

  async function forgot() {
    if (!email.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api.requestPasswordReset(email.trim());
    } catch {
      // Swallowed on purpose. The request endpoint answers 202 for every address, so the only
      // errors reachable here are transport or the rate limit — and surfacing "429" beside an
      // email field invites a reader to infer something about the ADDRESS. The confirmation
      // below is shown either way, which is the same promise the server makes.
    } finally {
      // Set regardless of outcome, for the reason above: the message must not depend on
      // anything the server would not tell an anonymous caller.
      setSent(true);
      setBusy(false);
    }
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError("");
  }

  return (
    <div className="flex min-h-full min-w-0 items-center justify-center overflow-x-hidden p-6">
      <div className="w-full max-w-sm min-w-0">
        <div className="mb-8 flex items-center gap-3">
          <LogoMark />
          <div className="leading-none">
            <div className="text-[17px] font-bold tracking-tight">Graphban</div>
            <div className="mt-1 font-mono text-[9.5px] tracking-[0.6px] text-faint">
              AGENT MEMORY · LINEAR EXECUTION
            </div>
          </div>
        </div>

        <form
          onSubmit={onSubmit}
          className="rounded-[16px] border border-line bg-surface-3/70 p-6 shadow-[0_24px_60px_rgba(0,0,0,0.4)]"
        >
          <h1 className="mb-1 text-[16px] font-semibold">
            {isSignup ? "Create your account" : "Sign in"}
          </h1>
          <p className="mb-5 text-[12.5px] text-muted">
            {isSignup
              ? "Set up a workspace and start capturing agent memory."
              : "Welcome back. Pick up where the agents left off."}
          </p>

          {isSignup && (
            <>
              <Field label="Name" htmlFor="signup-name">
                <Input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    clearError();
                  }}
                  autoComplete="name"
                  required
                />
              </Field>
              <Field label="Handle" htmlFor="signup-handle">
                <Input
                  value={handle}
                  onChange={(e) => {
                    setHandle(e.target.value);
                    clearError();
                  }}
                  placeholder="yourhandle"
                  autoComplete="username"
                  required
                  className="placeholder:italic"
                />
              </Field>
            </>
          )}

          <Field label="Email" htmlFor="login-email">
            <Input
              type="email"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
                clearError();
              }}
              autoComplete={isSignup ? "email" : "username"}
              required
              aria-invalid={authFailed || undefined}
            />
          </Field>

          <Field label="Password" htmlFor="login-password">
            <Input
              type="password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                clearError();
              }}
              autoComplete={isSignup ? "new-password" : "current-password"}
              required
              aria-invalid={authFailed || undefined}
            />
          </Field>

          {error && (
            <p className="mb-4 text-[12px] text-st-blocked" role="alert">
              {error}
            </p>
          )}

          <Button type="submit" className="min-h-6 w-full" disabled={busy}>
            {busy
              ? isSignup
                ? "Creating account…"
                : "Signing in…"
              : isSignup
                ? "Create account"
                : "Sign in"}
          </Button>

          {!isSignup && (
            <div className="mt-3 text-center text-[12px]">
              {sent ? (
                // ALWAYS THIS SENTENCE, whether or not the address is registered. The API
                // answers 202 identically either way so it cannot be used to discover who has
                // an account here (GRPH-359); a UI that said "no account found" would
                // reintroduce the oracle the server refuses to be. "Check your inbox" rather
                // than "Email sent", because `send_email` falls back to an in-process outbox
                // and the server cannot observe delivery either.
                <span className="text-muted">
                  If an account exists for that address, a reset link is on its way. Check your inbox.
                </span>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={forgot}
                    disabled={busy || !email.trim()}
                    className={cn(
                      "min-h-6 rounded-sm px-1 text-muted-2",
                      "[@media(hover:hover)_and_(pointer:fine)]:hover:text-fg-2 [@media(hover:hover)_and_(pointer:fine)]:hover:underline",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                      "disabled:cursor-not-allowed disabled:text-muted-2",
                    )}
                  >
                    Forgot your password?
                  </button>
                  {!email.trim() && (
                    <p className="mt-1.5 text-[11px] text-muted-2">
                      Enter your email above to request a reset link.
                    </p>
                  )}
                </>
              )}
            </div>
          )}

          <div className="mt-5 text-center text-[12px] text-muted">
            {isSignup ? (
              <>
                Already have an account?{" "}
                <button
                  type="button"
                  onClick={() => switchMode("signin")}
                  className="min-h-6 rounded-sm px-0.5 text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [@media(hover:hover)_and_(pointer:fine)]:hover:underline"
                >
                  Sign in
                </button>
              </>
            ) : (
              <>
                New here?{" "}
                <button
                  type="button"
                  onClick={() => switchMode("signup")}
                  className="min-h-6 rounded-sm px-0.5 text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [@media(hover:hover)_and_(pointer:fine)]:hover:underline"
                >
                  Create an account
                </button>
              </>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactElement<{ id?: string }>;
}) {
  return (
    <div className="mb-4 min-w-0">
      <label
        htmlFor={htmlFor}
        className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint"
      >
        {label}
      </label>
      {React.cloneElement(children, { id: htmlFor })}
    </div>
  );
}

function messageFor(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "message" in err && typeof err.message === "string") {
    return err.message;
  }
  return fallback;
}

function LogoMark() {
  return (
    <div
      className="flex h-9 w-9 items-center justify-center rounded-[9px]"
      style={{
        background: "linear-gradient(150deg,#c6f24e,#8fd12e)",
        boxShadow: "0 0 0 1px rgba(198,242,78,.35),0 6px 18px rgba(198,242,78,.18)",
      }}
    >
      <svg width="18" height="18" viewBox="0 0 16 16" fill="none">
        <path d="M3 2v12M3 4h8M3 8h6M3 12h9" stroke="#0a0c0e" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </div>
  );
}
