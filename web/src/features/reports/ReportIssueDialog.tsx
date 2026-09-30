import { useMutation, useQuery } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";

const TYPES = [
  { key: "bug", label: "Bug" },
  { key: "feature", label: "Feature" },
  { key: "enhancement", label: "Enhancement" },
  { key: "feedback", label: "Feedback" },
] as const;

/**
 * "Report an issue with Graphban" — the in-app, user-initiated side of the upstream
 * feedback channel. Forwards a bug/idea about the tool itself to the maintainer's intake,
 * showing where it goes (transparency — never silent).
 */
export function ReportIssueDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { data: cfg } = useQuery({
    queryKey: ["upstream-config"],
    queryFn: () => api.upstreamConfig(),
    enabled: open,
  });
  const [type, setType] = React.useState<string>("bug");
  const [title, setTitle] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [done, setDone] = React.useState<{ request_id: string | null } | null>(null);

  const submit = useMutation({
    mutationFn: () => api.upstreamReport({ type, title: title.trim(), detail: detail.trim() }),
    onSuccess: (r) => setDone({ request_id: r.request_id }),
  });

  function close(o: boolean) {
    onOpenChange(o);
    if (!o) {
      setTitle("");
      setDetail("");
      setType("bug");
      setDone(null);
      submit.reset();
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Report an issue with Graphban</DialogTitle>
          <DialogDescription>A bug or idea about Graphban itself — not your project's work.</DialogDescription>
        </DialogHeader>

        {done ? (
          <div className="space-y-3">
            <div className="rounded-[11px] border border-accent/40 bg-[rgba(198,242,78,0.06)] p-3 text-[12.5px] text-fg-2">
              Thanks — your report was sent{done.request_id ? ` (${done.request_id})` : ""}.
            </div>
            <div className="flex justify-end">
              <Button size="sm" onClick={() => close(false)}>Done</Button>
            </div>
          </div>
        ) : cfg && !cfg.enabled ? (
          <p className="text-[12.5px] text-muted">Upstream reporting is turned off on this deployment.</p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (title.trim() && !submit.isPending) submit.mutate();
            }}
            className="space-y-3"
          >
            <div className="flex flex-wrap gap-1.5">
              {TYPES.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setType(t.key)}
                  className={cn(
                    "rounded-lg border px-2.5 py-1 text-[11.5px] transition-colors",
                    type === t.key
                      ? "border-accent/50 bg-surface-2 text-fg"
                      : "border-control bg-surface-2 text-muted hover:text-fg-2",
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Short summary" autoFocus />
            <Textarea
              rows={4}
              value={detail}
              onChange={(e) => setDetail(e.target.value)}
              placeholder="What happened, or what you'd want. Include repro steps for a bug."
            />
            {submit.isError && (
              <p className="text-[11.5px] text-st-blocked">Couldn't send — the upstream intake may be unreachable.</p>
            )}
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-faint">
                {cfg?.target ? (
                  <>Sends to <span className="font-mono text-muted">{cfg.target}</span></>
                ) : (
                  "Sends to the Graphban maintainers"
                )}
              </span>
              <Button size="sm" type="submit" disabled={!title.trim() || submit.isPending}>
                {submit.isPending ? "Sending…" : "Send report"}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
