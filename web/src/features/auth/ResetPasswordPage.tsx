import { KeyRound } from "lucide-react";
import * as React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/features/auth/AuthContext";

/**
 * Where an emailed reset link lands (`/reset-password?token=…`), GRPH-570.
 *
 * Mounted at the top level, outside the authed shell, because the whole premise is that the
 * visitor cannot sign in. Without this page the link 404s and the user meets a broken product
 * at the exact moment they are already locked out — which is worse than the missing feature
 * the reset flow was built to fix.
 *
 * **The copy is load-bearing here, not decoration.**
 *
 * - A spent or expired link returns 400. The message offers the way FORWARD — ask for another
 *   — because "invalid token" is an error the user cannot act on, and the one thing they can
 *   do is the thing the page should say.
 * - On success the user lands SIGNED IN. The server revoked every other session and issued a
 *   fresh pair, so sending them to a login form would ask them to prove what they have just
 *   proved.
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const { completePasswordReset } = useAuth();
  const navigate = useNavigate();

  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await completePasswordReset(token, password);
      navigate("/", { replace: true });
    } catch {
      // One message for expired, already-used and never-existed — the server does not
      // distinguish them, deliberately, and a UI that guessed would be inventing detail it
      // does not have.
      setError("That link is no longer valid. Ask for a new one from the sign-in page.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-[9px] bg-[rgba(198,242,78,0.12)]">
            <KeyRound size={18} className="text-accent" />
          </div>
          <div className="text-[15px] font-semibold tracking-tight">Graphban</div>
        </div>

        <div className="rounded-[16px] border border-line bg-surface-3/70 p-6 shadow-[0_24px_60px_rgba(0,0,0,0.4)]">
          <h1 className="mb-1 text-[15px] font-semibold tracking-tight">Choose a new password</h1>

          {!token ? (
            // A link that lost its token cannot be completed, and saying so beats a form that
            // fails on submit for a reason the user cannot see.
            <p className="text-[12.5px] text-muted">
              This link is missing its token. Ask for a new one from the sign-in page.
            </p>
          ) : (
            <form onSubmit={submit} className="mt-4 space-y-3">
              <Input
                type="password"
                autoFocus
                placeholder="New password"
                aria-label="New password"
                minLength={8}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              {error && <p className="text-[12px] text-red-400">{error}</p>}
              <Button type="submit" className="w-full" disabled={busy || password.length < 8}>
                {busy ? "Setting…" : "Set password and sign in"}
              </Button>
              <p className="text-[11.5px] text-faint">
                Every other session will be signed out.
              </p>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
