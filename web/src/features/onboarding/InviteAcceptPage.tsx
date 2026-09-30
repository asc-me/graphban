import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2 } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/features/auth/AuthContext";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { InvitePreview } from "@/lib/types";

/**
 * Landing page for an emailed org invite link (`/invite/:token`), AL-74b. Mounted
 * at the top level (outside the authed app shell) so it works whether or not the
 * visitor is signed in:
 *  - signed out → a sign-up form (email locked to the invited address; registering
 *    with the token joins the org) with a sign-in fallback for existing users;
 *  - signed in as the invited email → a one-click Accept;
 *  - signed in as someone else → a mismatch notice with a sign-out.
 */
export function InviteAcceptPage() {
  const { token = "" } = useParams();
  const { user } = useAuth();

  const preview = useQuery({
    queryKey: ["invite-preview", token],
    queryFn: () => api.previewInvite(token),
    enabled: !!token,
    retry: false,
  });

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-[9px] bg-[rgba(198,242,78,0.12)]">
            <Building2 size={18} className="text-accent" />
          </div>
          <div className="text-[15px] font-semibold tracking-tight">Graphban</div>
        </div>

        <div className="rounded-[16px] border border-line bg-surface-3/70 p-6 shadow-[0_24px_60px_rgba(0,0,0,0.4)]">
          {preview.isLoading && <p className="text-body text-muted">Loading invitation…</p>}
          {preview.isError && (
            <>
              <h1 className="mb-1 text-[16px] font-semibold">Invitation unavailable</h1>
              <p className="text-body text-muted">
                This invitation link is invalid, has already been used, or has expired. Ask whoever
                invited you to send a fresh one.
              </p>
            </>
          )}
          {preview.data && (
            <InviteBody token={token} preview={preview.data} currentEmail={user?.email ?? null} />
          )}
        </div>
      </div>
    </div>
  );
}

function InviteBody({
  token,
  preview,
  currentEmail,
}: {
  token: string;
  preview: InvitePreview;
  currentEmail: string | null;
}) {
  const invitedBy = preview.invited_by ? `${preview.invited_by} invited you` : "You've been invited";
  const isPlatform = preview.kind === "platform";

  return (
    <>
      {isPlatform ? (
        <>
          <h1 className="mb-1 text-[16px] font-semibold">
            Welcome to <span className="text-accent">Graphban</span>
          </h1>
          <p className="mb-5 text-body text-muted">
            {invitedBy} to Graphban. Create your account and you'll set up your
            organization next.
          </p>
        </>
      ) : (
        <>
          <h1 className="mb-1 text-[16px] font-semibold">
            Join <span className="text-accent">{preview.org_name}</span>
          </h1>
          <p className="mb-5 text-body text-muted">
            {invitedBy} to join <strong>{preview.org_name}</strong> as {preview.role}.
          </p>
        </>
      )}

      {currentEmail === null ? (
        // A platform invite is redeemed by registering — no sign-in path, since an
        // existing account can't consume one (it needs an additional-org request).
        <SignedOutFlow token={token} email={preview.email} signupOnly={isPlatform} />
      ) : isPlatform ? (
        <AlreadySignedIn currentEmail={currentEmail} />
      ) : currentEmail.toLowerCase() === preview.email.toLowerCase() ? (
        <AcceptButton token={token} />
      ) : (
        <MismatchNotice invitedEmail={preview.email} currentEmail={currentEmail} />
      )}
    </>
  );
}

function AlreadySignedIn({ currentEmail }: { currentEmail: string }) {
  const { logout } = useAuth();
  return (
    <>
      <p className="mb-4 rounded-[10px] border border-line-2 bg-surface-2 p-3 text-[12px] text-muted">
        This invitation creates a <strong>new</strong> account and organization, but you're
        already signed in as <strong>{currentEmail}</strong>. Sign out to redeem it — or, if
        you need an additional organization on your existing account, request one from your
        organization settings.
      </p>
      <Button variant="outline" className="w-full" onClick={logout}>
        Sign out
      </Button>
    </>
  );
}

function AcceptButton({ token }: { token: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  async function accept() {
    setBusy(true);
    setError("");
    try {
      await api.acceptInvite(token);
      await qc.invalidateQueries({ queryKey: keys.orgs });
      navigate("/", { replace: true });
    } catch {
      setError("Could not accept the invitation. It may have expired.");
      setBusy(false);
    }
  }

  return (
    <>
      {error && <p className="mb-4 text-[12px] text-st-blocked">{error}</p>}
      <Button className="w-full" disabled={busy} onClick={accept}>
        {busy ? "Joining…" : "Accept invitation"}
      </Button>
    </>
  );
}

function MismatchNotice({
  invitedEmail,
  currentEmail,
}: {
  invitedEmail: string;
  currentEmail: string;
}) {
  const { logout } = useAuth();
  return (
    <>
      <p className="mb-4 rounded-[10px] border border-line-2 bg-surface-2 p-3 text-[12px] text-muted">
        This invitation is for <strong>{invitedEmail}</strong>, but you're signed in as{" "}
        <strong>{currentEmail}</strong>. Sign out and sign in with the invited address to accept.
      </p>
      <Button variant="outline" className="w-full" onClick={logout}>
        Sign out
      </Button>
    </>
  );
}

function SignedOutFlow({
  token,
  email,
  signupOnly = false,
}: {
  token: string;
  email: string;
  signupOnly?: boolean;
}) {
  const { login, register } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [mode, setMode] = React.useState<"signup" | "signin">("signup");
  const [name, setName] = React.useState("");
  const [handle, setHandle] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mode === "signup") {
        // Registering with the token both creates the account and joins the org.
        await register(name.trim(), email, handle.trim(), password, token);
      } else {
        // Existing account: sign in, then redeem the invite.
        await login(email, password);
        await api.acceptInvite(token);
      }
      await qc.invalidateQueries({ queryKey: keys.orgs });
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        err && typeof err === "object" && "message" in err && typeof err.message === "string"
          ? err.message
          : "Something went wrong. Please try again.",
      );
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <Field label="Email">
        <Input value={email} readOnly disabled className="opacity-70" />
      </Field>

      {mode === "signup" && (
        <>
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </Field>
          <Field label="Handle">
            <Input
              value={handle}
              onChange={(e) => setHandle(e.target.value)}
              placeholder="yourhandle"
              autoComplete="username"
            />
          </Field>
        </>
      )}

      <Field label="Password">
        <Input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === "signup" ? "new-password" : "current-password"}
        />
      </Field>

      {error && <p className="mb-4 text-[12px] text-st-blocked">{error}</p>}

      <Button type="submit" className="w-full" disabled={busy}>
        {busy
          ? signupOnly
            ? "Creating account…"
            : "Joining…"
          : signupOnly
            ? "Create account"
            : mode === "signup"
              ? "Create account & join"
              : "Sign in & join"}
      </Button>

      <div className={`mt-5 text-center text-[12px] text-muted ${signupOnly ? "hidden" : ""}`}>
        {mode === "signup" ? (
          <>
            Already have an account?{" "}
            <button
              type="button"
              onClick={() => {
                setMode("signin");
                setError("");
              }}
              className="text-accent hover:underline"
            >
              Sign in
            </button>
          </>
        ) : (
          <>
            Need an account?{" "}
            <button
              type="button"
              onClick={() => {
                setMode("signup");
                setError("");
              }}
              className="text-accent hover:underline"
            >
              Create one
            </button>
          </>
        )}
      </div>
    </form>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
        {label}
      </label>
      {children}
    </div>
  );
}
