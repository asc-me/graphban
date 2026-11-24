import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { errorDetail } from "@/lib/errors";
import { useItem, useOwnerClose, useResolveItem } from "@/lib/queries";
import type { EvidenceReceipt, Item } from "@/lib/types";

function existingPrUrl(item: Item, evidence: EvidenceReceipt[] = []): string {
  if (item.github_url) return item.github_url;
  for (const e of evidence) {
    if (e.url && /\/(pull|merge_requests|pull-requests)\//i.test(e.url)) return e.url;
  }
  return "";
}

/** How the owner is closing it. `merged` records the merge (GRPH-1030); the other two close
 * it as done with nothing delivered, pointing at the work that replaced it (GRPH-1671). */
type Mode = "merged" | "verified" | "duplicate" | "superseded";

const MODES: { id: Mode; label: string }[] = [
  { id: "merged", label: "Merged" },
  { id: "verified", label: "Verified" },
  { id: "duplicate", label: "Duplicate" },
  { id: "superseded", label: "Superseded" },
];

const TITLES: Record<Mode, string> = {
  merged: "Close as merged",
  verified: "Close as verified",
  duplicate: "Close as duplicate",
  superseded: "Close as superseded",
};

const DESCRIPTIONS: Record<Mode, string> = {
  merged:
    "Records the merge commit and who closed it. This is an owner close, not a reviewer sign-off.",
  verified: "Records the revision you checked and where. Nothing is recorded as merged.",
  duplicate:
    "Points at the finished item that already did this work. Nothing is counted as delivered here.",
  superseded:
    "Points at the item or PRD that replaced this one. Nothing is counted as delivered here.",
};

export function OwnerCloseDialog({
  item,
  open,
  onClose,
}: {
  item: Item | null;
  open: boolean;
  onClose: () => void;
}) {
  const close = useOwnerClose();
  const resolve = useResolveItem();
  // The list row carries no evidence (GRPH-1228); a PR link may sit in a receipt.
  const { data: detail } = useItem(item?.id, open);
  const [mode, setMode] = React.useState<Mode>("merged");
  const [commit, setCommit] = React.useState("");
  const [prUrl, setPrUrl] = React.useState("");
  const [of, setOf] = React.useState("");
  const [reason, setReason] = React.useState("");
  const [note, setNote] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    if (!open || !item) return;
    setMode("merged");
    setCommit("");
    setPrUrl(existingPrUrl(item));
    setOf("");
    setReason("");
    setNote("");
    setUrl("");
    setError("");
    close.reset();
    resolve.reset();
  }, [open, item?.id]); // eslint-disable-line react-hooks/exhaustive-deps -- reset only on open/item

  React.useEffect(() => {
    if (!open || !item || !detail) return;
    const url = existingPrUrl(item, detail.evidence);
    setPrUrl((cur) => cur || url);
  }, [open, item?.id, detail]); // eslint-disable-line react-hooks/exhaustive-deps -- fill only an empty field

  const ready =
    mode === "merged"
      ? !!commit.trim()
      : mode === "verified"
      ? !!commit.trim() && !!note.trim()
      : !!of.trim() && (mode === "duplicate" || !!reason.trim());
  const pending = close.isPending || resolve.isPending;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!item || !ready) return;
    setError("");
    try {
      if (mode === "merged") {
        await close.mutateAsync({
          id: item.id,
          commit: commit.trim(),
          pr_url: prUrl.trim() || undefined,
        });
      } else if (mode === "verified") {
        await close.mutateAsync({
          id: item.id,
          kind: "verified",
          commit: commit.trim(),
          note: note.trim(),
          url: url.trim() || undefined,
        });
      } else {
        await resolve.mutateAsync({
          id: item.id,
          kind: mode,
          of: of.trim(),
          reason: reason.trim() || undefined,
        });
      }
      onClose();
    } catch (err) {
      setError(errorDetail(err, `Could not ${TITLES[mode].toLowerCase()}.`));
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{TITLES[mode]}</DialogTitle>
          <DialogDescription>
            {DESCRIPTIONS[mode]}
            {item ? ` ${item.id}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div role="radiogroup" aria-label="How it closed" className="flex gap-1.5">
          {MODES.map((m) => (
            <Button
              key={m.id}
              type="button"
              role="radio"
              aria-checked={mode === m.id}
              variant={mode === m.id ? "default" : "outline"}
              size="sm"
              onClick={() => {
                setMode(m.id);
                setError("");
              }}
            >
              {m.label}
            </Button>
          ))}
        </div>
        <form onSubmit={submit} className="space-y-3">
          {mode === "merged" ? (
            <>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  Merge commit
                </span>
                <Input
                  placeholder="sha that landed on the base"
                  value={commit}
                  onChange={(e) => setCommit(e.target.value)}
                  autoFocus
                  required
                />
              </label>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  PR url
                </span>
                <Input
                  placeholder="https://github.com/org/repo/pull/123"
                  value={prUrl}
                  onChange={(e) => setPrUrl(e.target.value)}
                />
              </label>
            </>
          ) : mode === "verified" ? (
            <>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  Commit
                </span>
                <Input
                  placeholder="the revision you verified"
                  value={commit}
                  onChange={(e) => setCommit(e.target.value)}
                  autoFocus
                  required
                />
              </label>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  Note
                </span>
                <Input
                  placeholder="where you checked it"
                  value={note}
                  maxLength={200}
                  onChange={(e) => setNote(e.target.value)}
                  required
                />
              </label>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  URL (optional)
                </span>
                <Input
                  placeholder="walk log or artifact"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </label>
            </>
          ) : (
            <>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  {mode === "duplicate" ? "Duplicate of" : "Superseded by"}
                </span>
                <Input
                  placeholder={
                    mode === "duplicate" ? "the finished item, e.g. GRPH-1668" : "item or PRD id"
                  }
                  value={of}
                  onChange={(e) => setOf(e.target.value)}
                  autoFocus
                  required
                />
              </label>
              <label className="block space-y-1">
                <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
                  {mode === "duplicate" ? "Note (optional)" : "Why"}
                </span>
                <Input
                  placeholder={mode === "duplicate" ? "" : "what replaced it, in one line"}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  required={mode === "superseded"}
                />
              </label>
            </>
          )}
          {error && (
            <p className="text-small text-st-blocked" role="alert">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={!ready || pending}>
              {TITLES[mode]}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
