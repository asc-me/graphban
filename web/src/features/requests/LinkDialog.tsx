import { Check, Link2 } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { STATUS_META } from "@/lib/meta";
import { useItems, useLinkRequest } from "@/lib/queries";
import type { RequestItem } from "@/lib/types";

export function LinkDialog({ request }: { request: RequestItem }) {
  const { data: items = [] } = useItems();
  const link = useLinkRequest();
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");

  const filtered = items.filter(
    (i) => i.title.toLowerCase().includes(q.toLowerCase()) || i.id.toLowerCase().includes(q.toLowerCase()),
  );

  async function choose(itemId: string | null) {
    await link.mutateAsync({ id: request.id, itemId });
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button
          onClick={(e) => e.stopPropagation()}
          className="inline-flex items-center gap-1 rounded-md border border-control px-2 py-1 font-mono text-[10px] text-muted transition-colors hover:border-control-hover hover:text-fg"
        >
          <Link2 size={11} />
          {request.linked_to ?? "link"}
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Link {request.id} to an item</DialogTitle>
        </DialogHeader>
        <Input placeholder="Search items…" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <div className="mt-3 max-h-[320px] space-y-1 overflow-y-auto">
          {filtered.map((it) => (
            <button
              key={it.id}
              onClick={() => choose(it.id)}
              className="flex w-full items-center gap-2.5 rounded-lg border border-control bg-surface-2 px-3 py-2 text-left transition-colors hover:border-control-hover"
            >
              <span
                className="h-1.5 w-1.5 flex-none rounded-full"
                style={{ background: STATUS_META[it.status].color }}
              />
              <span className="w-[52px] flex-none font-mono text-[10px] text-faint">{it.id}</span>
              <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">{it.title}</span>
              {request.linked_to === it.id && <Check size={13} className="text-accent" />}
            </button>
          ))}
        </div>
        {request.linked_to && (
          <div className="mt-3 flex justify-end">
            <Button variant="outline" size="sm" onClick={() => choose(null)}>
              Unlink
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
