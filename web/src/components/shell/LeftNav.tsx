import { Activity, BarChart3, BookOpen, Brain, Building2, ChartLine, ChevronDown, ChevronRight, ChevronsUpDown, ClipboardCheck, Gauge, Hammer, History, Layers, LayoutGrid, Link as LinkIcon, ListChecks, Map, MessageSquare, PanelLeftClose, PanelLeftOpen, Plug, Radar, Radio, Scale, Server, Settings, ShieldCheck, Star, Users, UsersRound, Workflow } from "lucide-react";
import * as React from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { ProjectSwitcher, ProjectTile } from "@/components/shell/ProjectSwitcher";
import { useProjectCtx } from "@/features/ProjectContext";
import { CloudOrgLinkDialog } from "@/features/settings/CloudOrgLinkDialog";
import { cn } from "@/lib/cn";
import {
  useCounts,
  useIsPlatformAdmin,
  useOrgs,
  useSyncStatus,
} from "@/lib/queries";
import type { ShellCounts } from "@/lib/types";
import { adminPath, consoleHref, orgPath, projectPath, settingsPath, tagFromPath, usableHttpUrl, viewSegment } from "@/lib/routes";

/** The `ShellCounts` field each page's badge restates (GRPH-P59 §D4, GRPH-1132). */
type CountKey = Extract<keyof ShellCounts,
  "items_open" | "requests_untriaged" | "code_nodes" | "mcp_tools" | "fleet_agents" | "outposts" | "live_agents" | "review">;
type Page = {
  view: string;
  icon: React.ReactNode;
  label: string;
  /** One line under the label in a flyout (`Sidebar Nav.dc.html`). */
  desc: string;
  count?: CountKey;
  /** The count reads as presence ("2 live"), so it is green. */
  live?: boolean;
  hostedOnly?: boolean;
};
type Section = "plan" | "build" | "observe";
type SectionDef = { key: Section; label: string; icon: React.ReactNode; pages: Page[] };

/**
 * Plan / Build / Observe (GRPH-940), one list for hosted and self-host (GRPH-1141), grouped
 * and ordered as decided in GRPH-P59 §D2 (GRPH-1142). A view is the path segment after the
 * project: `/p/GRPH/tracker` hosted, `/tracker` self-host. Dashboard is not one of them: it
 * is the project home, rendered above the sections (GRPH-1066). MCP Tools is hosted only —
 * self-host keeps it under Settings.
 */
export const SECTIONS: SectionDef[] = [
  {
    key: "plan", label: "Plan", icon: <ClipboardCheck size={16} />, pages: [
      { view: "triage", icon: <Radar size={16} />, label: "Triage", desc: "Overlaps and duplicates before work starts" },
      { view: "tracker", icon: <ListChecks size={16} />, label: "Tracker", desc: "One linear stream of work", count: "items_open" },
      { view: "requests", icon: <Star size={16} />, label: "Requests", desc: "Submissions from the public form", count: "requests_untriaged" },
      { view: "prds", icon: <BarChart3 size={16} />, label: "PRDs", desc: "Versioned specs with linked items" },
      { view: "roadmap", icon: <Map size={16} />, label: "Roadmap", desc: "MVP → Post-MVP → Later" },
    ],
  },
  {
    key: "build", label: "Build", icon: <Hammer size={16} />, pages: [
      { view: "code", icon: <Workflow size={16} />, label: "Code graph", desc: "Modules, files and symbols agents changed", count: "code_nodes" },
      { view: "links", icon: <LinkIcon size={16} />, label: "Links", desc: "Typed relations between items and requests" },
      { view: "stack", icon: <Layers size={16} />, label: "Stack", desc: "Languages, frameworks and services" },
      { view: "rules", icon: <Scale size={16} />, label: "Rules", desc: "How work moves, and what holds each step" },
      { view: "mcp-tools", icon: <Plug size={16} />, label: "MCP Tools", desc: "How agents connect and call tools", count: "mcp_tools", hostedOnly: true },
      { view: "fleet", icon: <Users size={16} />, label: "Fleet", desc: "Who works here and what they spawn as", count: "fleet_agents" },
      { view: "outposts", icon: <Server size={16} />, label: "Outposts", desc: "Machines your agents run on", count: "outposts" },
    ],
  },
  {
    key: "observe", label: "Observe", icon: <Activity size={16} />, pages: [
      { view: "live", icon: <Radio size={16} />, label: "Live", desc: "Who is on the project right now", count: "live_agents", live: true },
      { view: "activity", icon: <History size={16} />, label: "Activity", desc: "Every accepted mutation, attributed" },
      { view: "memory", icon: <Brain size={16} />, label: "Memory", desc: "Candidates waiting to be published", count: "review" },
      { view: "lessons", icon: <BookOpen size={16} />, label: "Lessons", desc: "Published memory, scored for usefulness" },
      { view: "harness", icon: <Gauge size={16} />, label: "Harness", desc: "Which agent can review which change" },
    ],
  },
];

/** The section that owns the current page, in either URL shape. Null on Dashboard, Settings, … */
export function sectionFromPath(pathname: string): Section | null {
  const view = viewSegment(pathname);
  return SECTIONS.find((s) => s.pages.some((p) => p.view === view))?.key ?? null;
}

/** Remembered per browser until the per-user server store (GRPH-1133) exists. */
export const SIDEBAR_STORAGE_KEY = "gb.sidebar.collapsed";

export function readSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/** `[` belongs to whatever is being typed into, never to the sidebar. */
function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  return /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const EASE = "cubic-bezier(.2,.8,.2,1)";

/**
 * A page's badge text, or undefined when there is no number to show. Absent is not zero:
 * while the counts load, after a failed read, or from a server without the field, nothing
 * renders — a `0` is drawn only when a read succeeded and said 0.
 */
export function badgeText(page: Page, counts: ShellCounts | undefined): string | undefined {
  const n = page.count && counts ? counts[page.count] : undefined;
  if (typeof n !== "number") return undefined;
  const text = n.toLocaleString("en-US");
  return page.live ? `${text} live` : text;
}

type Tip = { group?: string; label: string; meta?: string; live?: boolean; top: number };
type Fly = { key: Section; top: number; trigger: HTMLElement };

/**
 * The sidebar: 216px expanded or a 60px icon rail, toggled from its footer or with `[`
 * (GRPH-P59 §D1). One component for hosted and self-host and every route.
 *
 * The Admin row renders for `owner` and `admin` only, and for a `member` it is **absent** —
 * not greyed, not a permission error (PRD-21 rev2). `authz.require_org_admin` is the same
 * gate server-side, so this hides nothing the API would have allowed. Self-host has no org
 * to administer and so no Admin row.
 */
export function LeftNav({ hosted = false }: { hosted?: boolean }) {
  const { active, activeId } = useProjectCtx();
  // Badge numbers only — never the collections (GRPH-431). The review count is candidates
  // PLUS anything auto-published without them (AL-287), summed server-side.
  const countsQ = useCounts(activeId);
  // A failed refetch keeps the last data; a failed read still shows no number.
  const counts = countsQ.isError ? undefined : countsQ.data;
  const { data: orgs = [] } = useOrgs(hosted);
  const org = hosted ? (orgs[0] ?? null) : null;
  const canAdminister = org?.role === "owner" || org?.role === "admin";
  // 404 (the non-admin case) resolves as an error, so this is false for tenants.
  const { data: adminMe } = useIsPlatformAdmin();
  const isPlatformAdmin = !!adminMe?.is_platform_admin;
  const [orgDialogOpen, setOrgDialogOpen] = React.useState(false);
  const { pathname } = useLocation();
  // The URL is the project, and it is known before the project list answers.
  const homeTag = hosted ? (active?.tag ?? tagFromPath(pathname)) : null;
  const current = sectionFromPath(pathname);
  const view = viewSegment(pathname);

  const [collapsed, setCollapsed] = React.useState(readSidebarCollapsed);
  const [inline, setInline] = React.useState(true);
  const [tip, setTip] = React.useState<Tip | null>(null);
  // The project switcher's menu suppresses rail labels, as an open flyout does.
  const [menus, setMenus] = React.useState(0);
  // One flyout at a time: the section it lists, where it sits, and what opened it.
  const [fly, setFly] = React.useState<Fly | null>(null);
  // Kept after close so the flyout fades out with its content rather than emptying first.
  const [flyShown, setFlyShown] = React.useState<Fly | null>(null);
  const asideRef = React.useRef<HTMLElement>(null);
  const flyRef = React.useRef<HTMLDivElement>(null);
  const reduce = prefersReducedMotion();

  // Landing in another section makes it the inline one, opened.
  React.useEffect(() => setInline(true), [current]);

  const closeFly = React.useCallback(() => setFly(null), []);

  const openFly = (key: Section, trigger: HTMLElement) => {
    setTip(null);
    if (fly?.key === key) return closeFly();
    const box = asideRef.current?.getBoundingClientRect();
    const r = trigger.getBoundingClientRect();
    const next = { key, trigger, top: Math.max(8, r.top - (box?.top ?? 0) - 8) };
    setFly(next);
    setFlyShown(next);
  };

  // Keep the open flyout inside the sidebar and the viewport.
  React.useLayoutEffect(() => {
    if (!fly || !flyRef.current || !asideRef.current) return;
    const box = asideRef.current.getBoundingClientRect();
    const room = Math.min(box.height, window.innerHeight - box.top);
    const h = flyRef.current.offsetHeight;
    const top = Math.max(8, Math.min(fly.top, room - h - 8));
    if (top !== fly.top) {
      const next = { ...fly, top };
      setFly(next);
      setFlyShown(next);
    }
  }, [fly]);

  // Esc or a press anywhere outside the sidebar closes it.
  React.useEffect(() => {
    if (!fly) return;
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape" || !fly) return;
      // Back to the row or icon that opened it, so the keyboard does not lose its place.
      fly.trigger.focus();
      closeFly();
    }
    function onDown(e: MouseEvent) {
      if (!asideRef.current?.contains(e.target as Node)) closeFly();
    }
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [fly, closeFly]);

  const toggle = React.useCallback(() => {
    setTip(null);
    setFly(null);
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // Private mode: the toggle still works for this page.
      }
      return next;
    });
  }, []);

  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "[" || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (isTypingTarget(e.target)) return;
      toggle();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  const meta = (p: Page) => badgeText(p, counts);
  const viewPath = (v: string) =>
    hosted ? (homeTag ? projectPath(homeTag, v) : `/${v}`) : `/${v}`;
  const dashboardPath = hosted ? (homeTag ? projectPath(homeTag) : null) : "/home";
  const sections = SECTIONS.map((s) => ({
    ...s,
    pages: s.pages.filter((p) => hosted || !p.hostedOnly),
  }));
  const onMenu = (open: boolean) => setMenus((n) => Math.max(0, n + (open ? 1 : -1)));

  const tipProps = (t: Omit<Tip, "top">) => {
    const show = (e: React.SyntheticEvent<HTMLElement>) => {
      const box = asideRef.current?.getBoundingClientRect();
      const r = e.currentTarget.getBoundingClientRect();
      setTip({ ...t, top: r.top - (box?.top ?? 0) + r.height / 2 });
    };
    const hide = () => setTip(null);
    return { onMouseEnter: show, onFocus: show, onMouseLeave: hide, onBlur: hide };
  };

  const fade = reduce ? undefined : `opacity 180ms ${EASE}`;
  const slide = reduce ? undefined : `opacity 180ms ${EASE}, transform 240ms ${EASE}`;
  const tipOn = !!tip && collapsed && menus === 0 && !fly;
  const trigger = (s: SectionDef, t?: TipHandlers) => ({
    "aria-haspopup": "dialog" as const,
    "aria-expanded": fly?.key === s.key,
    "data-open": fly?.key === s.key ? "true" : undefined,
    onClick: (e: React.MouseEvent<HTMLButtonElement>) => openFly(s.key, e.currentTarget),
    ...t,
  });

  return (
    <aside
      ref={asideRef}
      aria-label="Sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      style={{ width: collapsed ? 60 : 216, transition: reduce ? "none" : `width 240ms ${EASE}` }}
      className="relative flex-none border-r border-line bg-[rgba(9,11,13,0.5)]"
    >
      <div className="absolute inset-0 overflow-hidden">
        {/* Rail */}
        <div
          data-layer="rail"
          aria-hidden={!collapsed}
          inert={!collapsed}
          style={{ opacity: collapsed ? 1 : 0, transition: fade }}
          className={cn(
            "absolute inset-y-0 left-0 flex w-[60px] flex-col items-center gap-0.5 overflow-y-auto py-3",
            !collapsed && "pointer-events-none",
          )}
        >
          <ProjectEntry variant="tile" orgName={org?.name} hosted={hosted} onOpenChange={onMenu} tipProps={tipProps({ label: "Switch project" })} />
          {dashboardPath && (
            <RailLink to={dashboardPath} end label="Dashboard" icon={<LayoutGrid size={16} />} tipProps={tipProps({ label: "Dashboard" })} />
          )}
          {sections.map((s) =>
            s.key === current ? (
              <React.Fragment key={s.key}>
                <RailDivider />
                {s.pages.map((p) => (
                  <RailLink
                    key={p.view}
                    to={viewPath(p.view)}
                    label={p.label}
                    icon={p.icon}
                    current={p.view === view}
                    tipProps={tipProps({
                      group: s.label.toUpperCase(),
                      label: p.label,
                      meta: meta(p),
                      live: p.live,
                    })}
                  />
                ))}
                <RailDivider />
              </React.Fragment>
            ) : (
              <button
                key={s.key}
                type="button"
                aria-label={s.label}
                {...trigger(s, tipProps({ label: s.label, meta: "›" }))}
                className={cn(RAIL_BTN, "data-[open=true]:bg-surface-3 data-[open=true]:text-fg")}
              >
                {s.icon}
              </button>
            ),
          )}
          <div className="mt-auto" />
          {canAdminister && (
            <RailLink href={consoleHref(adminPath())} label="Admin" icon={<UsersRound size={16} />} tipProps={tipProps({ label: "Admin" })} />
          )}
          <RailLink to="/usage" label="Usage" icon={<ChartLine size={16} />} tipProps={tipProps({ label: "Usage" })} />
          <RailLink to={viewPath("feedback-kit")} label="Feedback Kit" icon={<MessageSquare size={16} />} tipProps={tipProps({ label: "Feedback Kit" })} />
          <RailLink to="/settings" label="Settings" icon={<Settings size={16} />} tipProps={tipProps({ label: "Settings" })} />
          <RailDivider />
          <button
            type="button"
            onClick={toggle}
            aria-label="Expand sidebar"
            {...tipProps({ label: "Expand sidebar", meta: "[" })}
            className={RAIL_BTN}
          >
            <PanelLeftOpen size={16} />
          </button>
        </div>

        {/* Expanded */}
        <div
          data-layer="expanded"
          aria-hidden={collapsed}
          inert={collapsed}
          style={{
            opacity: collapsed ? 0 : 1,
            transform: collapsed ? "translateX(-48px)" : "none",
            transition: slide,
          }}
          className={cn(
            "absolute inset-y-0 left-0 flex w-[216px] flex-col overflow-y-auto px-3 py-4",
            collapsed && "pointer-events-none",
          )}
        >
          <ProjectEntry variant="card" orgName={org?.name} hosted={hosted} onOpenChange={onMenu} />

          <nav aria-label="Project" className="mt-4 flex flex-col gap-0.5">
            {dashboardPath && (
              <NavItem to={dashboardPath} label="Dashboard" end dashboard />
            )}
            {sections.map((s) =>
              s.key === current ? (
                <React.Fragment key={s.key}>
                  <SectionHeader
                    label={s.label}
                    expanded={inline}
                    controls={`nav-${s.key}`}
                    reduce={reduce}
                    onToggle={() => setInline((v) => !v)}
                  />
                  {/* Always mounted so the rows can animate; closed, it is inert. */}
                  <div
                    id={`nav-${s.key}`}
                    data-inline={inline ? "open" : "closed"}
                    aria-hidden={!inline}
                    inert={!inline}
                    style={{
                      display: "grid",
                      gridTemplateRows: inline ? "1fr" : "0fr",
                      opacity: inline ? 1 : 0,
                      transition: reduce ? "none" : `grid-template-rows 240ms ${EASE}, opacity 180ms ${EASE}`,
                    }}
                  >
                    <div className="flex min-h-0 flex-col gap-0.5 overflow-hidden pl-2">
                      {s.pages.map((p) => (
                        <NavItem
                          key={p.view}
                          to={viewPath(p.view)}
                          icon={p.icon}
                          label={p.label}
                          count={meta(p)}
                          live={p.live}
                        />
                      ))}
                    </div>
                  </div>
                </React.Fragment>
              ) : (
                <button
                  key={s.key}
                  type="button"
                  {...trigger(s)}
                  className="flex items-center gap-2.5 rounded-[9px] h-[31px] flex-none px-2.5 leading-[15px] text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2 data-[open=true]:bg-surface-3 data-[open=true]:text-fg"
                >
                  <span className="flex-1 font-medium">{s.label}</span>
                  <ChevronRight size={12} className="text-faint" />
                </button>
              ),
            )}
          </nav>

          <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-2.5">
            {hosted ? (
              <NavItem href={consoleHref(orgPath())} icon={<Building2 size={16} />} label="Organization" />
            ) : (
              <OrgRailItem onOpenDialog={() => setOrgDialogOpen(true)} />
            )}
            {canAdminister && (
              <NavItem href={consoleHref(adminPath())} icon={<UsersRound size={16} />} label="Admin" />
            )}
            <NavItem to="/usage" icon={<ChartLine size={16} />} label="Usage" />
            {isPlatformAdmin && <NavItem href={consoleHref("/operator")} icon={<ShieldCheck size={16} />} label="Operator" />}
            <NavItem to={viewPath("feedback-kit")} icon={<MessageSquare size={16} />} label="Feedback Kit" />
            <NavItem to="/settings" icon={<Settings size={16} />} label="Settings" />
            <div className="my-1.5 border-t border-line" />
            <button
              type="button"
              onClick={toggle}
              title="Collapse to icons"
              className="flex items-center gap-2.5 rounded-[9px] h-[29px] flex-none px-2.5 leading-[15px] text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
            >
              <PanelLeftClose size={16} />
              <span className="flex-1">Collapse sidebar</span>
              <kbd className="rounded border border-line-2 px-1.5 font-mono text-[10px] text-faint">[</kbd>
            </button>
          </div>
        </div>
      </div>

      <div
        role="tooltip"
        aria-hidden="true"
        style={{
          top: tip?.top ?? 0,
          opacity: tipOn ? 1 : 0,
          transform: tipOn ? "translate(4px,-50%)" : "translate(-2px,-50%)",
          transition: reduce ? undefined : `opacity 120ms ${EASE}, transform 120ms ${EASE}`,
        }}
        className="pointer-events-none absolute left-full z-50 ml-2 flex items-center gap-1.5 whitespace-nowrap rounded-lg border border-line-2 bg-surface-3 px-2.5 py-1.5 text-body text-fg shadow-lg"
      >
        <span className="absolute -left-[5px] top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 border-b border-l border-line-2 bg-surface-3" />
        {tipOn && (
          <>
            {tip.group && <span className="font-mono text-meta font-medium text-faint">{tip.group}</span>}
            <span>{tip.label}</span>
            {tip.meta && (
              <span className={cn("font-mono text-[10px]", tip.live ? "text-st-done" : "text-faint")}>{tip.meta}</span>
            )}
          </>
        )}
      </div>

      {flyShown && (
        <SectionFlyout
          ref={flyRef}
          open={!!fly}
          top={(fly ?? flyShown).top}
          section={sections.find((x) => x.key === flyShown.key)!}
          tag={active?.tag ?? homeTag ?? undefined}
          viewPath={viewPath}
          meta={meta}
          reduce={reduce}
          onPick={() => closeFly()}
        />
      )}

      {!hosted && <CloudOrgLinkDialog open={orgDialogOpen} onOpenChange={setOrgDialogOpen} />}
    </aside>
  );
}

type TipHandlers = {
  onMouseEnter: React.MouseEventHandler<HTMLElement>;
  onMouseLeave: React.MouseEventHandler<HTMLElement>;
  onFocus: React.FocusEventHandler<HTMLElement>;
  onBlur: React.FocusEventHandler<HTMLElement>;
};

const RAIL_BTN =
  "relative flex h-9 w-10 flex-none items-center justify-center rounded-[9px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2";

function RailDivider() {
  return <div className="my-1.5 h-px w-6 flex-none bg-line" />;
}

/** The lime bar that marks the current page, in the rail and in the expanded list. */
function CurrentBar({ className }: { className?: string }) {
  return (
    <span
      data-current-bar
      aria-hidden="true"
      className={cn("absolute top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-accent", className)}
    />
  );
}

function RailLink({
  to, href, end, current, label, icon, tipProps,
}: {
  to?: string;
  href?: string;
  end?: boolean;
  /** Overrides NavLink's own match — a page is current only inside its section. */
  current?: boolean;
  label: string;
  icon: React.ReactNode;
  tipProps: TipHandlers;
}) {
  if (href) {
    return (
      <a href={href} aria-label={label} {...tipProps} className={RAIL_BTN}>
        {icon}
      </a>
    );
  }
  return (
    <NavLink
      to={to!}
      end={end}
      aria-label={label}
      {...tipProps}
      className={({ isActive }) =>
        cn(RAIL_BTN, (current ?? isActive) && "bg-surface-3 text-accent hover:text-accent")
      }
    >
      {({ isActive }) => (
        <>
          {(current ?? isActive) && <CurrentBar className="-left-3" />}
          {icon}
        </>
      )}
    </NavLink>
  );
}

/**
 * A section other than the current one, listed beside the sidebar (GRPH-P59 §D2): its pages
 * with a line of description and a count. Opening it never navigates; choosing a page does,
 * and that section becomes the inline one because the route says so.
 */
const SectionFlyout = React.forwardRef<HTMLDivElement, {
  open: boolean;
  top: number;
  section: SectionDef;
  tag?: string;
  viewPath: (v: string) => string;
  meta: (p: Page) => string | undefined;
  reduce: boolean;
  onPick: () => void;
}>(function SectionFlyout({ open, top, section, tag, viewPath, meta, reduce, onPick }, ref) {
  return (
    <div
      ref={ref}
      role="dialog"
      aria-label={section.label}
      aria-hidden={!open}
      inert={!open}
      data-flyout={section.key}
      style={{
        top,
        opacity: open ? 1 : 0,
        transform: open ? "none" : "translateX(-4px) scale(.98)",
        transition: reduce ? "none" : `opacity 120ms ${EASE}, transform 180ms ${EASE}`,
      }}
      className={cn(
        "absolute left-full z-50 ml-2 flex w-[264px] origin-top-left flex-col rounded-xl border border-line-2 bg-surface-2 shadow-2xl",
        !open && "pointer-events-none",
      )}
    >
      <div className="flex items-center justify-between px-3.5 pb-1.5 pt-3 font-mono text-[9px] tracking-[.7px] text-faint">
        <span>{section.label.toUpperCase()}</span>
        {tag && <span>{tag}</span>}
      </div>
      <div className="flex flex-col gap-px px-1.5 pb-1.5 pt-0.5">
        {section.pages.map((p) => {
          const n = meta(p);
          return (
            <Link
              key={p.view}
              to={viewPath(p.view)}
              onClick={onPick}
              className="flex items-start gap-2.5 rounded-lg p-2 text-fg transition-colors hover:bg-surface-3"
            >
              <span className="mt-px flex-none text-muted">{p.icon}</span>
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="text-[13px] font-medium">{p.label}</span>
                <span className="text-small leading-[1.4] text-muted">{p.desc}</span>
              </span>
              {n != null && (
                <span className={cn("mt-0.5 font-mono text-[10px]", p.live ? "text-st-done" : "text-faint")}>
                  {n}
                </span>
              )}
            </Link>
          );
        })}
      </div>
      <div className="border-t border-line px-3.5 py-2 font-mono text-meta text-faint">esc to close</div>
    </div>
  );
});

/** Project card (expanded) or initial tile (rail). Both open the same switcher (GRPH-1143). */
function ProjectEntry({
  variant, orgName, hosted, onOpenChange, tipProps,
}: {
  variant: "card" | "tile";
  orgName?: string;
  hosted: boolean;
  onOpenChange: (open: boolean) => void;
  tipProps?: TipHandlers;
}) {
  const { active } = useProjectCtx();
  return (
    <ProjectSwitcher
      placement={variant === "tile" ? "right" : "below"}
      orgName={orgName}
      hosted={hosted}
      onOpenChange={onOpenChange}
      trigger={({ ref, ...t }) =>
        variant === "tile" ? (
          <button ref={ref} type="button" aria-label="Switch project" {...tipProps} {...t} className={cn(RAIL_BTN, "mb-1")}>
            <ProjectTile project={active} />
          </button>
        ) : (
          <button
            ref={ref}
            type="button"
            aria-label="Switch project"
            {...t}
            className={cn(
              "flex h-11 w-full items-center gap-2.5 rounded-[10px] border bg-surface-2 px-2.5 transition-colors hover:border-control-hover",
              t["aria-expanded"] ? "border-control-hover" : "border-control",
            )}
          >
            <ProjectTile project={active} />
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-[13px] font-semibold leading-tight">
                {active?.name ?? "—"}
              </span>
              <span className="mt-0.5 block truncate font-mono text-[10px] uppercase text-faint">
                {[orgName, active?.tag].filter(Boolean).join(" · ")}
              </span>
            </span>
            <ChevronsUpDown size={12} className="flex-none text-faint" />
          </button>
        )
      }
    />
  );
}

function SectionHeader({
  label, expanded, controls, reduce, onToggle,
}: {
  label: string;
  expanded: boolean;
  controls: string;
  reduce: boolean;
  onToggle: () => void;
}) {
  // `aria-controls` is dropped while collapsed: the panel only exists when open, so
  // pointing at an id that is not in the document would be a lie to a screen reader.
  return (
    <button
      type="button"
      aria-expanded={expanded}
      aria-controls={expanded ? controls : undefined}
      onClick={onToggle}
      className="flex items-center gap-2.5 rounded-[9px] h-[31px] flex-none px-2.5 text-left text-[13px] leading-[17px] text-fg-2 transition-colors hover:bg-surface-3"
    >
      <span className="flex-1 font-medium">{label}</span>
      <ChevronDown
        size={12}
        data-chevron="section"
        style={{
          transform: expanded ? "rotate(180deg)" : "none",
          transition: reduce ? "none" : `transform 240ms ${EASE}`,
        }}
        className="text-faint"
      />
    </button>
  );
}

function OrgRailItem({ onOpenDialog }: { onOpenDialog: () => void }) {
  const { data: status, isLoading } = useSyncStatus();
  const navigate = useNavigate();
  const url = usableHttpUrl(status?.cloud_url);
  if (isLoading && !status) {
    return <NavItem to={settingsPath("deployment/sync")} icon={<Building2 size={16} />} label="Organization" />;
  }
  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer noopener"
        className="flex items-center gap-2.5 rounded-[9px] h-[29px] flex-none px-2.5 leading-[15px] text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
      >
        <Building2 size={16} />
        <span className="flex-1">Organization</span>
      </a>
    );
  }
  const isMalformed = !!(status && status.cloud_url.trim());
  return (
    <button
      type="button"
      onClick={() => (isMalformed ? navigate(`${settingsPath("deployment/sync")}?reason=malformed`) : onOpenDialog())}
      className="flex items-center gap-2.5 rounded-[9px] h-[29px] flex-none px-2.5 leading-[15px] text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
    >
      <span className="relative flex-none">
        <Building2 size={16} />
        {!isMalformed && (
          <span
            className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-st-blocked"
            aria-label="Not linked"
          />
        )}
      </span>
      <span className="flex-1">Organization</span>
    </button>
  );
}

function NavItem({
  to,
  href,
  icon,
  label,
  count,
  live,
  end,
  dashboard,
}: {
  to?: string;
  /** Console origin. Those screens are not routes in this app. */
  href?: string;
  icon?: React.ReactNode;
  label: string;
  /** Dashboard is a text-only row, styled like a section header (Sidebar Nav design). */
  dashboard?: boolean;
  /** Badge text from `badgeText`; undefined renders no badge. */
  count?: string;
  /** Presence ("2 live") reads green. */
  live?: boolean;
  /** Match this path exactly. A prefix would light the parent on every child page. */
  end?: boolean;
}) {
  const base = cn(
    "relative flex flex-none items-center gap-2.5 rounded-[9px] px-2.5 text-[13px] leading-[15px] transition-colors",
    // Fixed heights, not padding + line-height: the 16px icon would push a page row to 30px.
    dashboard ? "h-[33px] font-medium" : "h-[29px]",
  );
  const badge = count != null && (
    <span data-badge className={cn("font-mono text-[10px]", live ? "text-st-done" : "text-faint")}>
      {count}
    </span>
  );
  if (href) {
    return (
      <a href={href} className={cn(base, "text-muted hover:bg-surface-3 hover:text-fg-2")}>
        {icon}
        <span className="flex-1">{label}</span>
        {badge}
      </a>
    );
  }
  return (
    <NavLink
      to={to!}
      end={end}
      className={({ isActive }) =>
        cn(
          base,
          isActive
            ? "bg-surface-3 text-fg [&>svg]:text-accent"
            : "text-muted hover:bg-surface-3 hover:text-fg-2",
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && <CurrentBar className="-left-3" />}
          {icon}
          <span className="flex-1">{label}</span>
          {badge}
        </>
      )}
    </NavLink>
  );
}
