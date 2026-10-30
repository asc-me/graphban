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
import { useItem, useOwnerClose } from "@/lib/queries";
import type { EvidenceReceipt, Item } from "@/lib/types";

function existingPrUrl(item: Item, evidence: EvidenceReceipt[] = []): string {
  if (item.github_url) return item.github_url;
  for (const e of evidence) {
    if (e.url && /\/(pull|merge_requests|pull-requests)\//i.test(e.url)) return e.url;
  }
  return "";
}

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
  // The list row carries no evidence (GRPH-1228); a PR link may sit in a receipt.
  const { data: detail } = useItem(item?.id, open);
  const [commit, setCommit] = React.useState("");
  const [prUrl, setPrUrl] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    if (!open || !item) return;
    setCommit("");
    setPrUrl(existingPrUrl(item));
    setError("");
    close.reset();
  }, [open, item?.id]); // eslint-disable-line react-hooks/exhaustive-deps -- reset only on open/item

  React.useEffect(() => {
    if (!open || !item || !detail) return;
    const url = existingPrUrl(item, detail.evidence);
    setPrUrl((cur) => cur || url);
  }, [open, item?.id, detail]); // eslint-disable-line react-hooks/exhaustive-deps -- fill only an empty field

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!item || !commit.trim()) return;
    setError("");
    try {
      await close.mutateAsync({
        id: item.id,
        commit: commit.trim(),
        pr_url: prUrl.trim() || undefined,
      });
      onClose();
    } catch (err) {
      setError(errorDetail(err, "Could not close this item as merged."));
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Close as merged</DialogTitle>
          <DialogDescription>
            Records the merge commit and who closed it. This is an owner close, not a
            reviewer sign-off.
            {item ? ` ${item.id}` : ""}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
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
          {error && (
            <p className="text-small text-st-blocked" role="alert">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={!commit.trim() || close.isPending}>
              Close as merged
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
