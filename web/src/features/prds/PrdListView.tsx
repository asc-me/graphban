import { FileText, Plus, Upload, X } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import {
  PlannerEmpty,
  PlannerError,
  PlannerFilteredEmpty,
  PrdListSkeleton,
} from "@/components/planner/PlannerStates";
import { ViewBar, ViewFooter } from "@/components/planner/ViewBar";
import { hiddenSummary, scopeRows, statusMatches, useCondensed, useViews } from "@/components/planner/viewScope";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { keys, usePrds } from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";

import { relTime } from "@/lib/relTime";

import { PRD_STATUS_ORDER, prdStatusMeta } from "./meta";
import { RequestDots } from "./PrdRequests";

/** "2h ago" -> "2h"; absence stays a dash. */
function shortAgo(rel: string | null): string {
  return rel ? rel.replace(/ ago$/, "") : "—";
}

export function PrdListView() {
  const { activeId } = useProjectCtx();
  const { data: prds = [], isLoading, isError, refetch } = usePrds(activeId);
  const navigate = useNavigate();
  const views = useViews("prds");
  const [condensed, toggleCondensed] = useCondensed();

  const scopeOf = (v: { window: number; hide: number }) =>
    scopeRows(prds, v, {
      activity: (p) => p.updated_at,
      finishedAt: (p) => (p.status === "closed" ? p.closed_at ?? p.updated_at : null),
    });
  const scoped = scopeOf(views.state);
  const [chip, setChip] = React.useState<string>("all");
  const inView = scoped.shown.filter((p) => statusMatches(p.status, views.state.status, []));
  const chipCount = (st: string) => inView.filter((p) => st === "all" || p.status === st).length;
  const visible = inView.filter((p) => chip === "all" || p.status === chip);
  const hidden = hiddenSummary(scoped, views.state, "closed");
  const showHidden = () => views.patch({ window: 0, hide: 0 });
  const showing = !isLoading && !isError && prds.length > 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="PRDs"
        purpose="Product specs with version history, linked items, and AI drafting commands."
        action={<NewPrdDialog onCreated={(id) => navigate(`/prds/${id}`)} />}
        condensed={condensed}
      />

      {showing && (
        <ViewBar
          views={views}
          doneWord="closed"
          condensed={condensed}
          onToggleCondense={toggleCondensed}
          countFor={(v) => scopeOf(v).shown.filter((p) => statusMatches(p.status, v.status, [])).length}
        />
      )}

      {showing && (
        <div className="flex flex-wrap gap-1.5 px-4 pt-3" role="group" aria-label="Filter by status">
          {["all", ...PRD_STATUS_ORDER].map((st) => {
            const label = st === "all" ? "All" : prdStatusMeta(st).label;
            return (
              <button
                key={st}
                aria-pressed={chip === st}
                onClick={() => setChip(st)}
                className={
                  "inline-flex items-center gap-1.5 rounded-[7px] border px-2 py-0.5 text-small transition-colors " +
                  (chip === st
                    ? "border-control-hover bg-surface-4 text-fg"
                    : "border-control text-muted hover:text-fg-2")
                }
              >
                {st !== "all" && (
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: prdStatusMeta(st).color }} />
                )}
                {label}
                <span className="font-mono text-meta text-faint">{chipCount(st)}</span>
              </button>
            );
          })}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <PrdListSkeleton rows={prds.length > 0 ? prds.length : 6} />
        ) : isError ? (
          <PlannerError title="PRDs unavailable" onRetry={() => refetch()} />
        ) : prds.length === 0 ? (
          <PlannerEmpty
            title="No PRDs yet"
            description="Product specs live here — versioned requirements with linked items and AI drafting. Write the first PRD to define what you are building."
            action={<NewPrdDialog onCreated={(id) => navigate(`/prds/${id}`)} />}
          />
        ) : visible.length === 0 ? (
          <PlannerFilteredEmpty
            message={`No PRDs in this view${hidden ? ` — ${hidden}` : ""}.`}
            onClear={() => views.select(views.views.find((v) => v.id === "all") ?? views.current)}
          />
        ) : (
          <div className="space-y-2 p-4">
            {visible.map((p) => {
              const meta = prdStatusMeta(p.status);
              return (
                <div
                  key={p.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => navigate(`/prds/${p.id}`)}
                  onKeyDown={(e) => {
                    if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      navigate(`/prds/${p.id}`);
                    }
                  }}
                  className="flex w-full cursor-pointer items-center gap-3 rounded-[12px] border border-line-2 bg-surface-2 px-4 py-3 text-left transition-colors hover:border-line-hover"
                >
                  <FileText size={16} className="flex-none text-muted" />
                  <span className="w-14 flex-none font-mono text-small text-faint">{p.id}</span>
                  <span className="min-w-0 flex-1 truncate text-[13.5px] text-fg-2">{p.title}</span>
                  <div className="flex flex-none items-center gap-1.5">
                    {p.linked.slice(0, 4).map((id) => (
                      <span key={id} className="rounded-md border border-line-2 px-1.5 py-0.5 font-mono text-meta text-muted">
                        {id}
                      </span>
                    ))}
                    <RequestDots prd={p} onOpen={() => navigate(`/prds/${p.id}?tab=requests`)} />
                  </div>
                  <span className="flex-none rounded-md bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted-2">
                    {p.version}
                  </span>
                  <span
                    className="flex w-[86px] flex-none items-center gap-1.5 font-mono text-[10px] uppercase tracking-wide"
                    style={{ color: meta.color }}
                  >
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.color }} />
                    {meta.label}
                  </span>
                  <span className="w-[52px] flex-none text-right font-mono text-[10px] text-faint-2">
                    {shortAgo(relTime(p.updated_at))}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
      {showing && <ViewFooter shown={visible.length} hidden={hidden} onShowHidden={showHidden} />}
    </div>
  );
}

/** Title from the markdown's first `# heading`, else the file name without extension. */
function titleFromMarkdown(md: string, fileName: string): string {
  const heading = md.split("\n").find((l) => /^#\s+/.test(l));
  if (heading) return heading.replace(/^#\s+/, "").trim();
  return fileName.replace(/\.(md|markdown|txt)$/i, "").replace(/[-_]+/g, " ").trim();
}

function NewPrdDialog({ onCreated }: { onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = React.useState(false);
  const [title, setTitle] = React.useState("");
  const [template, setTemplate] = React.useState("standard");
  const [imported, setImported] = React.useState<{ name: string; body: string } | null>(null);
  const [busy, setBusy] = React.useState(false);

  function reset() {
    setTitle("");
    setTemplate("standard");
    setImported(null);
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const body = await file.text();
    setImported({ name: file.name, body });
    setTitle((t) => t.trim() || titleFromMarkdown(body, file.name));
    e.target.value = ""; // allow re-selecting the same file
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      const prd = await api.createPrd(title.trim(), template, imported?.body);
      qc.invalidateQueries({ queryKey: keys.prds });
      setOpen(false);
      reset();
      onCreated(prd.id);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) reset(); }}>
      <DialogTrigger asChild>
        <Button size="sm">
          <Plus size={14} />
          New PRD
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{imported ? "Import PRD" : "New PRD"}</DialogTitle>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <Input placeholder="PRD title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />

          {imported ? (
            <div className="flex items-center gap-2 rounded-lg border border-line-2 bg-surface-2 px-3 py-2 text-body">
              <FileText size={14} className="flex-none text-accent" />
              <span className="min-w-0 flex-1 truncate text-fg-2">{imported.name}</span>
              <span className="flex-none font-mono text-[10px] text-faint">
                {imported.body.split("\n").length} lines
              </span>
              <button type="button" onClick={() => setImported(null)} className="flex-none text-faint hover:text-fg">
                <X size={13} />
              </button>
            </div>
          ) : (
            <>
              <div className="flex gap-2">
                {["standard", "blank"].map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTemplate(t)}
                    className={
                      "flex-1 rounded-lg border px-3 py-2 text-body capitalize transition-colors " +
                      (template === t
                        ? "border-control-hover bg-surface-3 text-fg"
                        : "border-control bg-surface-2 text-muted hover:text-fg-2")
                    }
                  >
                    {t} template
                  </button>
                ))}
              </div>
              <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-line-2 bg-surface-2 px-3 py-2 text-body text-muted hover:border-line-hover hover:text-fg-2">
                <Upload size={14} />
                Import a .md file
                <input type="file" accept=".md,.markdown,.txt,text/markdown" className="hidden" onChange={onFile} />
              </label>
            </>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button type="button" variant="outline" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" size="sm" disabled={!title.trim() || busy}>
              {imported ? "Import" : "Create"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
