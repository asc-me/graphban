import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";

/**
 * Changing your own password (moved here from Settings → Account, GRPH-1792). The server
 * revokes every other session, so this says so rather than letting it read as a side effect.
 */
export function PasswordCard() {
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState("");

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < 8;
  const ready = current.length > 0 && next.length >= 8 && next === confirm && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setDone(false);
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(errorDetail(err, "could not change the password"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-label="Password" className="max-w-[380px] space-y-3">
      <p className="text-[12px] text-muted">
        Changing it signs out every other device — that is the point, not a side effect.
      </p>
      <Input type="password" autoComplete="current-password" placeholder="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      <Input type="password" autoComplete="new-password" placeholder="New password" value={next} onChange={(e) => setNext(e.target.value)} />
      <Input type="password" autoComplete="new-password" placeholder="Confirm new password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      {tooShort && <p className="text-[12px] text-muted">At least 8 characters.</p>}
      {mismatch && <p className="text-[12px] text-muted">Those two don’t match.</p>}
      {error && <p className="text-[12px] text-red-400">{error}</p>}
      {done && <p className="text-[12px] text-emerald-400">Password changed. Other devices are signed out.</p>}
      <Button type="submit" disabled={!ready}>
        {busy ? "Changing…" : "Change password"}
      </Button>
    </form>
  );
}
