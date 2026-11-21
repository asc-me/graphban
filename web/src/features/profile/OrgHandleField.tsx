import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { handleClash } from "@/features/profile/orgHandle";
import { errorDetail } from "@/lib/errors";

/**
 * "Your handle in <org>": a field prefilled with the server's suggestion. `onSubmit` rejects
 * with the API error; a second clash replaces the suggestion rather than failing silently.
 */
export function OrgHandleField({
  orgName,
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  orgName: string;
  initial: string;
  submitLabel: string;
  onSubmit: (handle: string) => Promise<unknown>;
  onCancel?: () => void;
}) {
  const [value, setValue] = React.useState(initial);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  async function go(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSubmit(value.trim().replace(/^@/, "").toLowerCase());
    } catch (err) {
      const clash = handleClash(err);
      if (clash) {
        setValue(clash.suggested);
        setError(`That handle is taken in ${orgName}. Try ${clash.suggested}.`);
      } else {
        setError(errorDetail(err, "could not set the handle"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={go} className="space-y-2">
      <label className="block">
        <span className="mb-1 block text-[12px] text-muted">Your handle in {orgName}</span>
        <Input value={value} onChange={(e) => setValue(e.target.value)} className="font-mono" aria-invalid={error !== ""} />
      </label>
      {error && (
        <p role="status" className="text-[12px] text-st-blocked">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || value.trim() === ""}>
          {busy ? "Saving…" : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" size="sm" variant="outline" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}
