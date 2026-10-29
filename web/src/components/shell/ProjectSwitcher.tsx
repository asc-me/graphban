import { Check, Plus, Search } from "lucide-react";
import * as React from "react";
import { createPortal } from "react-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { NewProjectDialog } from "@/features/onboarding/NewProjectDialog";
import { cn } from "@/lib/cn";
import { useProjectSummaries } from "@/lib/queries";
import { consoleHref, orgPath } from "@/lib/routes";
import type { Project, ProjectSummary } from "@/lib/types";

const WIDTH = 340;
const DAY_MS = 86_400_000;

/** Where the popover opens relative to its trigger: under the sidebar card or breadcrumb, beside the rail tile. */
export type SwitcherPlacement = "below" | "right";

export interface SwitcherTriggerProps {
  ref: React.Ref<HTMLButtonElement>;
  onClick: () => void;
  "aria-haspopup": "dialog";
  "aria-expanded": boolean;
}

/**
 * `N items · N agents · <branch>`, or `N items · idle Nd` once nobody has touched it for a day
 * (GRPH-P59 §D3). A null branch drops its segment rather than printing a placeholder. A
 * project with no summary — still loading, or the read failed — gets an empty line: a
 * guessed "0 items" would read as a real count.
 */
export function summaryMeta(s: ProjectSummary | undefined, now = Date.now()): string {
  if (!s) return "";
  const parts = [`${s.item_count} ${s.item_count === 1 ? "item" : "items"}`];
  const last = s.last_activity_at ? Date.parse(s.last_activity_at) : NaN;
  const idleDays = Number.isNaN(last) ? 0 : Math.floor((now - last) / DAY_MS);
  if (s.live_agents === 0 && idleDays >= 1) {
    parts.push(`idle ${idleDays}d`);
    return parts.join(" · ");
  }
  if (s.agent_count > 0) parts.push(`${s.agent_count} ${s.agent_count === 1 ? "agent" : "agents"}`);
  if (s.default_branch) parts.push(s.default_branch);
  return parts.join(" · ");
}

function matches(p: Project, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return p.name.toLowerCase().includes(needle) || (p.tag ?? "").toLowerCase().includes(needle);
}

export function ProjectTile({ project }: { project: Pick<Project, "name" | "accent"> | null }) {
  const accent = project?.accent ?? "#c6f24e";
  return (
    <span
      className="flex h-6 w-6 flex-none items-center justify-center rounded-md border font-mono text-small font-semibold"
      style={{ color: accent, borderColor: `${accent}4d`, background: `${accent}24` }}
    >
      {(project?.name ?? "?").slice(0, 1).toUpperCase()}
    </span>
  );
}

/**
 * The project switcher (GRPH-P59 §D3): one searchable popover behind the sidebar card, the
 * rail tile and the breadcrumb. Switching is `setActiveId` — navigation that keeps the
 * current view, or lands on the Dashboard from a page that is not a project view.
 *
 * Portalled to the body with fixed positioning: the sidebar clips its layers, and the
 * popover has to sit over the page beside it.
 */
export function ProjectSwitcher({
  placement,
  orgName,
  hosted = false,
  onOpenChange,
  trigger,
}: {
  placement: SwitcherPlacement;
  orgName?: string;
  hosted?: boolean;
  onOpenChange?: (open: boolean) => void;
  trigger: (props: SwitcherTriggerProps) => React.ReactNode;
}) {
  const { projects, active, setActiveId } = useProjectCtx();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [hi, setHi] = React.useState(0);
  const [pos, setPos] = React.useState({ top: 0, left: 0 });
  const [newProjectOpen, setNewProjectOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const popRef = React.useRef<HTMLDivElement>(null);
  const listId = React.useId();
  // Fetched only once someone opens the switcher; failure or loading leaves meta blank.
  const { data: summaries } = useProjectSummaries(open);
  const byId = React.useMemo(
    () => new Map((summaries ?? []).map((s) => [s.id, s])),
    [summaries],
  );

  const list = projects.filter((p) => matches(p, query));
  const optionId = (i: number) => `${listId}-opt-${i}`;

  // Reported as a pair so a parent counting open menus stays balanced, unmount included.
  const onOpenChangeRef = React.useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  React.useEffect(() => {
    if (!open) return;
    onOpenChangeRef.current?.(true);
    return () => onOpenChangeRef.current?.(false);
  }, [open]);

  const close = React.useCallback((refocus = true) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  const place = React.useCallback(() => {
    const t = triggerRef.current;
    if (!t) return;
    const r = t.getBoundingClientRect();
    if (placement === "below") {
      setPos({ top: r.bottom + 6, left: r.left });
    } else {
      // Beside the sidebar, not the tile, so it clears the rail's edge.
      const aside = t.closest("aside")?.getBoundingClientRect();
      setPos({ top: Math.max(8, r.top), left: (aside?.right ?? r.right) + 8 });
    }
  }, [placement]);

  const toggle = () => {
    if (open) return close(false);
    setQuery("");
    setHi(0);
    place();
    setOpen(true);
  };

  // Keep it inside the viewport once its height is known.
  React.useLayoutEffect(() => {
    if (!open || !popRef.current) return;
    const h = popRef.current.offsetHeight;
    const top = Math.max(8, Math.min(pos.top, window.innerHeight - h - 8));
    const left = Math.max(8, Math.min(pos.left, window.innerWidth - WIDTH - 8));
    if (top !== pos.top || left !== pos.left) setPos({ top, left });
  }, [open, pos, list.length]);

  // A press outside both the popover and its trigger closes it.
  React.useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      const t = e.target as Node;
      if (popRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      close(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, close]);

  const pick = (p: Project) => {
    close(false);
    if (p.id !== active?.id) setActiveId(p.id);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHi((i) => Math.min(i + 1, list.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHi((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (list[hi]) pick(list[hi]);
    } else if (e.key === "Escape") {
      // Stop here so the sidebar's own Esc handler does not also act on it.
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };

  const header = ["YOUR PROJECTS", orgName?.toUpperCase()].filter(Boolean).join(" · ");

  return (
    <>
      {trigger({
        ref: triggerRef,
        onClick: toggle,
        "aria-haspopup": "dialog",
        "aria-expanded": open,
      })}
      {open &&
        createPortal(
          <div
            ref={popRef}
            role="dialog"
            aria-label="Switch project"
            onKeyDown={onKey}
            style={{ top: pos.top, left: pos.left, width: WIDTH }}
            className="fixed z-50 flex flex-col rounded-xl border border-line-2 bg-surface-2 shadow-[0_18px_48px_rgba(0,0,0,0.5)]"
          >
            <div className="relative px-2.5 pb-1.5 pt-2.5">
              <Search size={13} className="pointer-events-none absolute left-[21px] top-1/2 mt-0.5 -translate-y-1/2 text-faint" />
              <input
                autoFocus
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setHi(0);
                }}
                placeholder="Find a project…"
                aria-label="Find a project"
                role="combobox"
                aria-expanded
                aria-controls={listId}
                aria-activedescendant={list[hi] ? optionId(hi) : undefined}
                className="h-8 w-full rounded-lg border border-control bg-surface pl-[30px] pr-2.5 text-body text-fg outline-none placeholder:text-faint focus:border-control-hover"
              />
            </div>
            <div className="flex items-center justify-between px-3.5 pb-1 pt-1.5 font-mono text-[9px] tracking-[0.07em] text-faint">
              <span>{header}</span>
              <span>
                {list.length} OF {projects.length}
              </span>
            </div>
            <div
              id={listId}
              role="listbox"
              aria-label="Projects"
              className="flex max-h-[300px] flex-col gap-px overflow-y-auto px-1.5 pb-1.5 pt-0.5"
            >
              {list.map((p, i) => {
                const s = byId.get(p.id);
                const current = p.id === active?.id;
                return (
                  <div
                    key={p.id}
                    id={optionId(i)}
                    role="option"
                    aria-selected={current}
                    data-highlighted={i === hi ? "true" : undefined}
                    onMouseEnter={() => setHi(i)}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => pick(p)}
                    className={cn(
                      "flex cursor-pointer items-center gap-2.5 rounded-lg p-2",
                      i === hi && "bg-surface-3",
                    )}
                  >
                    <ProjectTile project={p} />
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="flex items-center gap-[7px]">
                        <span data-testid="switcher-name" className="truncate text-[13px] font-medium text-fg">{p.name}</span>
                        <span className="rounded border border-line px-1.5 font-mono text-meta text-muted">
                          {p.tag}
                        </span>
                      </span>
                      <span data-testid="switcher-meta" className="min-h-[14px] truncate font-mono text-[10px] text-faint">
                        {summaryMeta(s)}
                      </span>
                    </span>
                    {s && s.live_agents > 0 && (
                      <span className="flex flex-none items-center gap-[5px] font-mono text-meta text-[#5fd07a]">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#5fd07a]" />
                        {s.live_agents} live
                      </span>
                    )}
                    <Check
                      size={13}
                      aria-hidden
                      className={cn("flex-none text-accent", !current && "opacity-0")}
                    />
                  </div>
                );
              })}
              {list.length === 0 && (
                <div className="px-2 py-[18px] text-center text-[12px] text-muted">
                  No projects match “{query}”.
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 border-t border-line px-2.5 py-2">
              {hosted && (
                <a
                  href={consoleHref(orgPath())}
                  onClick={() => close(false)}
                  className="flex h-7 items-center rounded-[7px] px-2.5 text-[12px] text-muted hover:bg-surface-3 hover:text-fg"
                >
                  All projects
                </a>
              )}
              <span className="flex-1" />
              <span className="font-mono text-meta text-faint-2">↑↓ · ↵ · esc</span>
              <button
                type="button"
                onClick={() => {
                  close(false);
                  setNewProjectOpen(true);
                }}
                className="flex h-7 items-center gap-1.5 rounded-[7px] border border-control bg-surface px-2.5 text-[12px] text-fg-2 hover:border-control-hover"
              >
                <Plus size={11} /> New project
              </button>
            </div>
          </div>,
          document.body,
        )}
      <NewProjectDialog open={newProjectOpen} onOpenChange={setNewProjectOpen} />
    </>
  );
}
