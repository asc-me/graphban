import { Check, ChevronDown, Layers } from "lucide-react";
import * as React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { useCounts } from "@/lib/queries";
import { ORG_BASE, projectPath, viewFromPath } from "@/lib/routes";

/**
 * The project bar: which project you are in, and the way out of it.
 *
 * Drilling from the org into a project is the primary motion of the hosted product, so
 * the breadcrumb runs org → project and both halves are links. Switching keeps the
 * current view — moving from one project's tracker to another's tracker is the common
 * case, and dumping you at a project home every time would make the switcher hostile.
 *
 * Switching is **navigation**, not a stored variable: the URL changes and everything
 * downstream re-derives from it. That is what makes a switch atomic rather than a state
 * change that some component might read a render too early.
 */
export function ProjectBar() {
  const { active, projects } = useProjectCtx();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [open, setOpen] = React.useState(false);
  // Scoped explicitly. Called bare, this asks the server for items with no project and
  // gets whatever it resolves by default — the bar then reports counts for a project the
  // user is not looking at, which is the ambient-project bug in a different costume.
  const { data: counts } = useCounts(active?.id);

  if (!active) return null;
  const view = viewFromPath(pathname);
  const inFlight = counts?.items_in_progress ?? 0;

  return (
    <div className="relative z-30 flex flex-none items-center gap-3 border-b border-line bg-surface/60">
      <span className="w-1 self-stretch" style={{ background: active.accent }} />
      <div className="flex items-center gap-2.5 py-2 pl-2.5">
        <Link
          to={ORG_BASE}
          className="font-mono text-[9.5px] uppercase tracking-[0.06em] text-faint hover:text-accent"
        >
          org
        </Link>
        <span className="font-mono text-[10px] text-faint-2">/</span>
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label="Switch project"
          className="flex items-center gap-2.5 rounded-lg border border-line-2 px-2.5 py-1 hover:border-line-hover"
        >
          <span
            className="h-[7px] w-[7px] shrink-0 rounded-sm"
            style={{ background: active.accent }}
          />
          <span className="text-[13.5px] font-semibold tracking-[-0.15px]">{active.name}</span>
          <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9.5px] text-muted">
            {active.tag}
          </span>
          <ChevronDown
            size={11}
            className={`shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`}
          />
        </button>
      </div>

      <div className="min-w-0 flex-1" />

      <div className="flex shrink-0 items-center gap-3.5 pr-5 font-mono text-[10px] uppercase tracking-[0.05em]">
        <Stat label="items" value={counts?.items ?? 0} />
        <Stat label="in flight" value={inFlight} tone={inFlight ? "text-accent" : undefined} />
      </div>

      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute left-14 top-full z-30 mt-1 w-[296px] overflow-hidden rounded-[11px] border border-line-2 bg-surface-2 shadow-xl">
            <div className="border-b border-line px-3 py-2 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
              SWITCH PROJECT · {projects.length}
            </div>
            <div className="max-h-[290px] overflow-auto">
              {projects.map((p) => (
                <button
                  key={p.id}
                  onClick={() => {
                    setOpen(false);
                    navigate(projectPath(p.tag, view));
                  }}
                  className="flex w-full items-center gap-2.5 border-b border-line px-3 py-2 text-left hover:bg-surface-3"
                >
                  <span
                    className="h-[7px] w-[7px] shrink-0 rounded-sm"
                    style={{ background: p.accent }}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-[12px] text-fg-2">{p.name}</span>
                    <span className="mt-0.5 block font-mono text-[9.5px] text-faint-2">{p.tag}</span>
                  </span>
                  {p.id === active.id && <Check size={12} style={{ color: p.accent }} />}
                </button>
              ))}
            </div>
            <Link
              to={ORG_BASE}
              onClick={() => setOpen(false)}
              className="flex w-full items-center gap-2 px-3 py-2 text-[12px] text-muted hover:bg-surface-3 hover:text-fg"
            >
              <Layers size={12} /> All projects
            </Link>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-faint-2">{label}</span>
      <span className={`text-[11.5px] ${tone ?? "text-muted"}`}>{value}</span>
    </span>
  );
}
