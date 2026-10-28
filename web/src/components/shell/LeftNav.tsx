import { Activity, BarChart3, BookMarked, Boxes, Building2, Check, ChevronDown, ChevronRight, ChevronsUpDown, Gauge, GitFork, Inbox, LayoutGrid, ListChecks, Map, Network, PanelLeftClose, PanelLeftOpen, Plug, Plus, Radar, ScrollText, Server, Settings, ShieldCheck, Sparkles, Star, Users, UsersRound } from "lucide-react";
import * as React from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useProjectCtx } from "@/features/ProjectContext";
import { NewProjectDialog } from "@/features/onboarding/NewProjectDialog";
import { CloudOrgLinkDialog } from "@/features/settings/CloudOrgLinkDialog";
import { cn } from "@/lib/cn";
import {
  useCounts,
  useIsPlatformAdmin,
  useOrgs,
  useSyncStatus,
} from "@/lib/queries";
import { adminPath, consoleHref, orgPath, projectPath, settingsPath, tagFromPath, usableHttpUrl, viewSegment } from "@/lib/routes";

type CountKey = "items" | "requests" | "review";
type Page = { view: string; icon: React.ReactNode; label: string; count?: CountKey; hostedOnly?: boolean };
type Section = "plan" | "build" | "observe";

/**
 * Plan / Build / Observe (GRPH-940), one list for hosted and self-host (GRPH-1141). A view
 * is the path segment after the project: `/p/GRPH/tracker` hosted, `/tracker` self-host.
 * Dashboard is not one of them: it is the project home, rendered above the sections (GRPH-1066).
 * MCP Tools is hosted only — self-host keeps it under Settings.
 */
const SECTIONS: { key: Section; label: string; icon: React.ReactNode; pages: Page[] }[] = [
  {
    key: "plan", label: "Plan", icon: <ListChecks size={16} />, pages: [
      { view: "tracker", icon: <ListChecks size={16} />, label: "Tracker", count: "items" },
      { view: "requests", icon: <Star size={16} />, label: "Requests", count: "requests" },
      { view: "triage", icon: <Radar size={16} />, label: "Triage" },
      { view: "prds", icon: <BarChart3 size={16} />, label: "PRDs" },
      { view: "roadmap", icon: <Map size={16} />, label: "Roadmap" },
    ],
  },
  {
    key: "build", label: "Build", icon: <Network size={16} />, pages: [
      { view: "code", icon: <Network size={16} />, label: "Code graph" },
      { view: "stack", icon: <Boxes size={16} />, label: "Stack" },
      { view: "links", icon: <GitFork size={16} />, label: "Links" },
      { view: "mcp-tools", icon: <Plug size={16} />, label: "MCP Tools", hostedOnly: true },
      { view: "fleet", icon: <Users size={16} />, label: "Fleet" },
      { view: "outposts", icon: <Server size={16} />, label: "Outposts" },
    ],
  },
  {
    key: "observe", label: "Observe", icon: <Activity size={16} />, pages: [
      { view: "memory-review", icon: <Inbox size={16} />, label: "Memory review", count: "review" },
      { view: "memory-triage", icon: <Inbox size={16} />, label: "Memory triage" },
      { view: "lessons", icon: <BookMarked size={16} />, label: "Lessons" },
      { view: "activity", icon: <ScrollText size={16} />, label: "Activity" },
      { view: "live", icon: <Activity size={16} />, label: "Live" },
      { view: "harness", icon: <Gauge size={16} />, label: "Harness" },
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

type Tip = { group?: string; label: string; meta?: string; top: number };

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
  const { data: counts } = useCounts(activeId);
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
  // Any open menu (project switcher, section menu) suppresses rail labels.
  const [menus, setMenus] = React.useState(0);
  const asideRef = React.useRef<HTMLElement>(null);
  const reduce = prefersReducedMotion();

  const toggle = React.useCallback(() => {
    setTip(null);
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

  const badges: Record<CountKey, number | undefined> = {
    items: counts?.items,
    requests: counts?.requests,
    review: counts?.review || undefined,
  };
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
  const tipOn = !!tip && collapsed && menus === 0;

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
            "absolute inset-y-0 left-0 flex w-[60px] flex-col items-center gap-1 overflow-y-auto py-4",
            !collapsed && "pointer-events-none",
          )}
        >
          <ProjectSwitcher variant="tile" onOpenChange={onMenu} tipProps={tipProps({ label: "Switch project" })} />
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
                      meta: p.count && badges[p.count] != null ? String(badges[p.count]) : undefined,
                    })}
                  />
                ))}
                <RailDivider />
              </React.Fragment>
            ) : (
              <SectionMenu
                key={s.key}
                section={s}
                variant="icon"
                viewPath={viewPath}
                badges={badges}
                onOpenChange={onMenu}
                tipProps={tipProps({ label: s.label, meta: "›" })}
              />
            ),
          )}
          <div className="mt-auto" />
          <RailDivider />
          {canAdminister && (
            <RailLink href={consoleHref(adminPath())} label="Admin" icon={<UsersRound size={16} />} tipProps={tipProps({ label: "Admin" })} />
          )}
          <RailLink to="/usage" label="Usage" icon={<BarChart3 size={16} />} tipProps={tipProps({ label: "Usage" })} />
          <RailLink to={viewPath("feedback-kit")} label="Feedback Kit" icon={<Sparkles size={16} />} tipProps={tipProps({ label: "Feedback Kit" })} />
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
          <ProjectSwitcher variant="card" orgName={org?.name} onOpenChange={onMenu} />

          <nav aria-label="Project" className="mt-4 flex flex-col gap-0.5">
            {dashboardPath && (
              <NavItem to={dashboardPath} icon={<LayoutGrid size={16} />} label="Dashboard" end />
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
                  {inline && (
                    <div id={`nav-${s.key}`} className="flex flex-col gap-0.5 pl-2">
                      {s.pages.map((p) => (
                        <NavItem
                          key={p.view}
                          to={viewPath(p.view)}
                          icon={p.icon}
                          label={p.label}
                          count={p.count ? badges[p.count] : undefined}
                        />
                      ))}
                    </div>
                  )}
                </React.Fragment>
              ) : (
                <SectionMenu
                  key={s.key}
                  section={s}
                  variant="row"
                  viewPath={viewPath}
                  badges={badges}
                  onOpenChange={onMenu}
                />
              ),
            )}
          </nav>

          <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-3">
            {hosted ? (
              <NavItem href={consoleHref(orgPath())} icon={<Building2 size={16} />} label="Organization" />
            ) : (
              <OrgRailItem onOpenDialog={() => setOrgDialogOpen(true)} />
            )}
            {canAdminister && (
              <NavItem href={consoleHref(adminPath())} icon={<UsersRound size={16} />} label="Admin" />
            )}
            <NavItem to="/usage" icon={<BarChart3 size={16} />} label="Usage" />
            {isPlatformAdmin && <NavItem href={consoleHref("/operator")} icon={<ShieldCheck size={16} />} label="Operator" />}
            <NavItem to={viewPath("feedback-kit")} icon={<Sparkles size={16} />} label="Feedback Kit" />
            <NavItem to="/settings" icon={<Settings size={16} />} label="Settings" />
            <div className="my-1.5 border-t border-line" />
            <button
              type="button"
              onClick={toggle}
              title="Collapse to icons"
              className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
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
        className="pointer-events-none absolute left-full z-50 ml-2 flex items-center gap-1.5 whitespace-nowrap rounded-md border border-line-2 bg-surface-3 px-2 py-1 text-[12px] text-fg shadow-lg"
      >
        <span className="absolute -left-[5px] top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 border-b border-l border-line-2 bg-surface-3" />
        {tipOn && (
          <>
            {tip.group && <span className="text-[10px] font-medium text-faint">{tip.group}</span>}
            <span>{tip.label}</span>
            {tip.meta && <span className="font-mono text-[10px] text-faint">{tip.meta}</span>}
          </>
        )}
      </div>

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
  "relative flex h-9 w-9 flex-none items-center justify-center rounded-[9px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2";

function RailDivider() {
  return <div className="my-1 h-px w-6 flex-none bg-line" />;
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
 * A section other than the current one: one row (or rail icon) that opens its pages beside
 * the sidebar. Opening never navigates; choosing a page does, and that section becomes the
 * current one because the route says so.
 */
function SectionMenu({
  section, variant, viewPath, badges, onOpenChange, tipProps,
}: {
  section: { key: Section; label: string; icon: React.ReactNode; pages: Page[] };
  variant: "row" | "icon";
  viewPath: (v: string) => string;
  badges: Record<CountKey, number | undefined>;
  onOpenChange: (open: boolean) => void;
  tipProps?: TipHandlers;
}) {
  const navigate = useNavigate();
  return (
    <DropdownMenu modal={false} onOpenChange={onOpenChange}>
      <DropdownMenuTrigger asChild>
        {variant === "icon" ? (
          <button type="button" aria-label={section.label} {...tipProps} className={cn(RAIL_BTN, "data-[state=open]:bg-surface-3 data-[state=open]:text-fg")}>
            {section.icon}
          </button>
        ) : (
          <button
            type="button"
            className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2 data-[state=open]:bg-surface-3 data-[state=open]:text-fg"
          >
            <span className="flex-1 font-medium">{section.label}</span>
            <ChevronRight size={12} className="text-faint" />
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent side="right" align="start" sideOffset={8} className="w-[220px]">
        <DropdownMenuLabel>{section.label.toUpperCase()}</DropdownMenuLabel>
        {section.pages.map((p) => {
          const n = p.count ? badges[p.count] : undefined;
          return (
            <DropdownMenuItem key={p.view} onSelect={() => navigate(viewPath(p.view))} className="gap-2.5 text-body">
              <span className="flex-none text-muted">{p.icon}</span>
              <span className="flex-1 truncate">{p.label}</span>
              {n != null && <span className="font-mono text-[10px] text-faint">{n}</span>}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Project card (expanded) or initial tile (rail). Both open the same switcher. */
function ProjectSwitcher({
  variant, orgName, onOpenChange, tipProps,
}: {
  variant: "card" | "tile";
  orgName?: string;
  onOpenChange: (open: boolean) => void;
  tipProps?: TipHandlers;
}) {
  const { projects, active, setActiveId } = useProjectCtx();
  const [newProjectOpen, setNewProjectOpen] = React.useState(false);
  const accent = active?.accent ?? "#c6f24e";
  const initial = (active?.name ?? "?").slice(0, 1).toUpperCase();
  const tile = (
    <span
      className="flex h-6 w-6 flex-none items-center justify-center rounded-md border font-mono text-small font-semibold"
      style={{ color: accent, borderColor: `${accent}4d`, background: `${accent}29` }}
    >
      {initial}
    </span>
  );
  return (
    <>
      <DropdownMenu modal={false} onOpenChange={onOpenChange}>
        <DropdownMenuTrigger asChild>
          {variant === "tile" ? (
            <button type="button" aria-label="Switch project" {...tipProps} className={cn(RAIL_BTN, "mb-1")}>
              {tile}
            </button>
          ) : (
            <button
              type="button"
              className="flex h-11 w-full items-center gap-2.5 rounded-[10px] border border-control bg-surface-2 px-2.5 transition-colors hover:border-control-hover"
            >
              {tile}
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
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" side={variant === "tile" ? "right" : "bottom"} className="w-[220px]">
          <DropdownMenuLabel>Switch project</DropdownMenuLabel>
          {projects.map((p) => (
            <DropdownMenuItem
              key={p.id}
              onSelect={() => setActiveId(p.id)}
              className="gap-2.5 text-body"
            >
              <span className="h-2.5 w-2.5 flex-none rounded-[3px]" style={{ background: p.accent }} />
              <span className="flex-1 truncate text-left">{p.name}</span>
              {p.id === active?.id && <Check size={13} style={{ color: p.accent }} />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onSelect={(e) => {
              e.preventDefault();
              setNewProjectOpen(true);
            }}
            className="gap-2.5 text-body text-muted"
          >
            <Plus size={14} className="flex-none" />
            <span className="flex-1 truncate text-left">New project</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <NewProjectDialog open={newProjectOpen} onOpenChange={setNewProjectOpen} />
    </>
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
      className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-fg-2 transition-colors hover:bg-surface-3"
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
        className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
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
      className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
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
  end,
}: {
  to?: string;
  /** Console origin. Those screens are not routes in this app. */
  href?: string;
  icon: React.ReactNode;
  label: string;
  count?: number;
  /** Match this path exactly. A prefix would light the parent on every child page. */
  end?: boolean;
}) {
  const base =
    "relative flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] transition-colors";
  const badge = count != null && (
    <span className="rounded-md bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted">
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
