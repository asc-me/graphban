import { Activity, BarChart3, BookMarked, Building2, Check, ChevronDown, CreditCard, GitBranch, GitFork, Inbox, LayoutGrid, ListChecks, Map, Network, Orbit, Palette, Plug, Plus, Radar, ScrollText, Server, Settings, ShieldCheck, Sparkles, Star, Users, UsersRound } from "lucide-react";
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
import { SPECULATIVE_ENABLED } from "@/features/orgadmin/Speculative";
import { cn } from "@/lib/cn";
import {
  useCounts,
  useIsPlatformAdmin,
  useOrgs,
  useSyncStatus,
} from "@/lib/queries";
import { ORG_BASE, adminPath, orgPath, projectPath, settingsPath, usableHttpUrl } from "@/lib/routes";

/** The project-scoped views, in rail order. */
const WORKSPACE = [
  { to: "tracker", icon: <ListChecks size={16} />, label: "Tracker", count: "items" },
  { to: "requests", icon: <Star size={16} />, label: "Requests", count: "requests" },
  { to: "triage", icon: <Radar size={16} />, label: "Triage" },
  { to: "dashboard", icon: <LayoutGrid size={16} />, label: "Dashboard" },
  { to: "links", icon: <GitFork size={16} />, label: "Links" },
  { to: "code", icon: <Network size={16} />, label: "Code graph" },
  { to: "roadmap", icon: <Map size={16} />, label: "Roadmap" },
  { to: "mcp-tools", icon: <Plug size={16} />, label: "MCP Tools" },
  { to: "fleet", icon: <Users size={16} />, label: "Fleet" },
  { to: "memory-review", icon: <Inbox size={16} />, label: "Memory review", count: "review" },
  { to: "lessons", icon: <BookMarked size={16} />, label: "Lessons" },
  { to: "activity", icon: <ScrollText size={16} />, label: "Activity" },
  { to: "live", icon: <Activity size={16} />, label: "Live" },
  { to: "prds", icon: <BarChart3 size={16} />, label: "PRDs" },
] as const;

/**
 * The rail, split into what you use and what you administer (PRD-21 rev2).
 *
 * The ADMIN group renders for `owner` and `admin` only, and for a `member` it is
 * **absent** — not greyed, not a permission error. A disabled row advertises a thing you
 * cannot have and invites a support ticket; an absent one is simply the truthful shape of
 * that person's product. `authz.require_org_admin` is the same gate server-side, so this
 * hides nothing that the API would have allowed.
 *
 * Self-host has neither group: no org exists to administer, and the workspace lives at
 * the root exactly as before.
 */
export function LeftNav({ hosted = false }: { hosted?: boolean }) {
  // GRPH-P28: the accordion is self-host only. Hosted keeps today's rail, including
  // Admin and Galaxy — hiding those here is the sabotage the suite is written to catch.
  if (!hosted) return <SelfHostLeftNav />;
  return <HostedLeftNav />;
}

function HostedLeftNav() {
  const hosted = true;
  const { projects, active, activeId, setActiveId } = useProjectCtx();
  // Badge numbers only — never the collections. Fetching items, requests, candidates and
  // auto-actions in full to call `.length` on them cost 2.1 MB on every route (GRPH-431).
  // The review count still means what it did: candidates PLUS anything auto-published
  // without them (AL-287), because counting only candidates reads as "no work" on a project
  // whose agents publish directly — exactly when there is most to look at. That sum now
  // happens server-side, next to the definition it depends on.
  const { data: counts } = useCounts(activeId);
  const { data: orgs = [] } = useOrgs(hosted);
  const org = orgs[0] ?? null;
  const canAdminister = org?.role === "owner" || org?.role === "admin";
  // 404 (the non-admin case) resolves as an error, so this is false for tenants.
  const { data: adminMe } = useIsPlatformAdmin();
  const isPlatformAdmin = !!adminMe?.is_platform_admin;
  const [newProjectOpen, setNewProjectOpen] = React.useState(false);

  const badges: Record<string, number | undefined> = {
    items: counts?.items,
    requests: counts?.requests,
    review: counts?.review || undefined,
  };
  const viewPath = (view: string) =>
    hosted && active?.tag ? projectPath(active.tag, view) : `/${view}`;

  return (
    <aside className="flex w-[216px] flex-none flex-col border-r border-line bg-[rgba(9,11,13,0.5)] px-3 py-4">
      {hosted ? (
        <NavLink
          to={ORG_BASE}
          className="flex h-10 w-full items-center gap-2.5 rounded-[10px] border border-line-2 bg-surface-2 px-3 transition-colors hover:border-line-hover"
        >
          <span className="flex h-5 w-5 flex-none items-center justify-center rounded-md border border-accent/30 bg-accent/[0.16] font-mono text-[10px] font-semibold text-accent">
            {(org?.name ?? "?").slice(0, 1).toUpperCase()}
          </span>
          <span className="min-w-0 flex-1 text-left">
            <span className="block truncate text-[12.5px] font-semibold leading-tight">
              {org?.name ?? "—"}
            </span>
            <span className="mt-0.5 block font-mono text-[9px] uppercase tracking-[0.05em] text-faint">
              {org?.plan ?? ""} plan
            </span>
          </span>
        </NavLink>
      ) : (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex h-10 w-full items-center gap-2.5 rounded-[10px] border border-line-2 bg-surface-2 px-3 transition-colors hover:border-line-hover">
              <span
                className="h-2.5 w-2.5 flex-none rounded-[3px]"
                style={{ background: active?.accent ?? "#c6f24e" }}
              />
              <span className="flex-1 truncate text-left text-[13px] font-semibold">
                {active?.name ?? "Core Platform"}
              </span>
              <ChevronDown size={12} className="text-faint" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-[220px]">
            <DropdownMenuLabel>Switch project</DropdownMenuLabel>
            {projects.map((p) => (
              <DropdownMenuItem
                key={p.id}
                onSelect={() => setActiveId(p.id)}
                className="gap-2.5 text-[12.5px]"
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
              className="gap-2.5 text-[12.5px] text-muted"
            >
              <Plus size={14} className="flex-none" />
              <span className="flex-1 truncate text-left">New project</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <NewProjectDialog open={newProjectOpen} onOpenChange={setNewProjectOpen} />

      <RailHeading>Workspace</RailHeading>
      <nav className="flex flex-col gap-0.5">
        {WORKSPACE.map((n) => (
          <NavItem
            key={n.to}
            to={viewPath(n.to)}
            icon={n.icon}
            label={n.label}
            count={"count" in n && n.count ? badges[n.count] : undefined}
          />
        ))}
      </nav>

      {hosted && canAdminister && (
        <>
          <RailHeading>Admin</RailHeading>
          <nav className="flex flex-col gap-0.5">
            <NavItem to={adminPath("users")} icon={<UsersRound size={16} />} label="Users & access" />
            <NavItem to={adminPath("teams")} icon={<Users size={16} />} label="Teams" />
            <NavItem to={adminPath("deployments")} icon={<Server size={16} />} label="Deployments" />
            {SPECULATIVE_ENABLED && (
              <NavItem to={adminPath("branding")} icon={<Palette size={16} />} label="Branding" />
            )}
            <NavItem to={adminPath("integrations")} icon={<Plug size={16} />} label="Integrations" />
            <NavItem to={adminPath("gitops")} icon={<GitBranch size={16} />} label="Gitops" />
            <NavItem to={adminPath("billing")} icon={<CreditCard size={16} />} label="Billing" />
          </nav>
        </>
      )}

      <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-3">
        {hosted && !canAdminister && (
          <div className="mb-2 rounded-[9px] border border-dashed border-line-2 px-2.5 py-2">
            <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
              no admin group
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              You are a <span className="font-mono text-fg-2">member</span>. Org administration
              belongs to an owner or admin — ask one of them if you need a change here.
            </p>
          </div>
        )}
        {hosted && (
          <>
            <NavItem to={orgPath("galaxy")} icon={<Orbit size={16} />} label="Galaxy" />
            <NavItem to={ORG_BASE} icon={<Building2 size={16} />} label="Organization" end />
          </>
        )}
        {!hosted && <NavItem to="/organization" icon={<Building2 size={16} />} label="Organization" />}
        {isPlatformAdmin && <NavItem to="/admin" icon={<ShieldCheck size={16} />} label="Operator" />}
        <NavItem to={viewPath("feedback-kit")} icon={<Sparkles size={16} />} label="Feedback Kit" />
        <NavItem to="/settings" icon={<Settings size={16} />} label="Settings" />
      </div>
    </aside>
  );
}

const PLAN = [
  { to: "/tracker", icon: <ListChecks size={16} />, label: "Tracker", count: "items" as const },
  { to: "/triage", icon: <Radar size={16} />, label: "Triage" },
  { to: "/requests", icon: <Star size={16} />, label: "Requests", count: "requests" as const },
  { to: "/prds", icon: <BarChart3 size={16} />, label: "PRDs" },
  { to: "/roadmap", icon: <Map size={16} />, label: "Roadmap" },
];
const BUILD = [
  { to: "/code", icon: <Network size={16} />, label: "Code graph" },
  { to: "/links", icon: <GitFork size={16} />, label: "Links" },
  { to: "/fleet", icon: <Users size={16} />, label: "Fleet" },
];
const OBSERVE = [
  { to: "/activity", icon: <ScrollText size={16} />, label: "Activity" },
  { to: "/live", icon: <Activity size={16} />, label: "Live" },
  { to: "/memory-review", icon: <Inbox size={16} />, label: "Memory", count: "review" as const },
  { to: "/lessons", icon: <BookMarked size={16} />, label: "Lessons" },
];

function ownsPath(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

function sectionFromPath(pathname: string): "home" | "plan" | "build" | "observe" | null {
  if (pathname === "/home" || pathname === "/") return "home";
  if (PLAN.some((n) => ownsPath(pathname, n.to))) return "plan";
  if (BUILD.some((n) => ownsPath(pathname, n.to))) return "build";
  if (OBSERVE.some((n) => ownsPath(pathname, n.to))) return "observe";
  return null;
}

function SelfHostLeftNav() {
  const { projects, active, activeId, setActiveId } = useProjectCtx();
  const { data: counts } = useCounts(activeId);
  const { data: adminMe } = useIsPlatformAdmin();
  const isPlatformAdmin = !!adminMe?.is_platform_admin;
  const [newProjectOpen, setNewProjectOpen] = React.useState(false);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const section = sectionFromPath(pathname);

  const badges: Record<string, number | undefined> = {
    items: counts?.items,
    requests: counts?.requests,
    review: counts?.review || undefined,
  };

  const observeDefault =
    (counts?.review ?? 0) > 0 ? "/memory-review" : "/activity";

  return (
    <aside className="flex w-[216px] flex-none flex-col border-r border-line bg-[rgba(9,11,13,0.5)] px-3 py-4">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button className="flex h-10 w-full items-center gap-2.5 rounded-[10px] border border-line-2 bg-surface-2 px-3 transition-colors hover:border-line-hover">
            <span
              className="h-2.5 w-2.5 flex-none rounded-[3px]"
              style={{ background: active?.accent ?? "#c6f24e" }}
            />
            <span className="flex-1 truncate text-left text-[13px] font-semibold">
              {active?.name ?? "Core Platform"}
            </span>
            <ChevronDown size={12} className="text-faint" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-[220px]">
          <DropdownMenuLabel>Switch project</DropdownMenuLabel>
          {projects.map((p) => (
            <DropdownMenuItem
              key={p.id}
              onSelect={() => setActiveId(p.id)}
              className="gap-2.5 text-[12.5px]"
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
            className="gap-2.5 text-[12.5px] text-muted"
          >
            <Plus size={14} className="flex-none" />
            <span className="flex-1 truncate text-left">New project</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <NewProjectDialog open={newProjectOpen} onOpenChange={setNewProjectOpen} />

      <nav className="mt-4 flex flex-col gap-0.5">
        <NavItem to="/home" icon={<LayoutGrid size={16} />} label="Home" />

        <SectionHeader
          label="Plan"
          expanded={section === "plan"}
          controls="nav-plan"
          onOpen={() => navigate("/tracker")}
        />
        {section === "plan" && (
          <div id="nav-plan" className="flex flex-col gap-0.5 pl-2">
            {PLAN.map((n) => (
              <NavItem
                key={n.to}
                to={n.to}
                icon={n.icon}
                label={n.label}
                count={"count" in n && n.count ? badges[n.count] : undefined}
              />
            ))}
          </div>
        )}

        <SectionHeader
          label="Build"
          expanded={section === "build"}
          controls="nav-build"
          onOpen={() => navigate("/code")}
        />
        {section === "build" && (
          <div id="nav-build" className="flex flex-col gap-0.5 pl-2">
            {BUILD.map((n) => (
              <NavItem key={n.to} to={n.to} icon={n.icon} label={n.label} />
            ))}
          </div>
        )}

        <SectionHeader
          label="Observe"
          expanded={section === "observe"}
          controls="nav-observe"
          onOpen={() => navigate(observeDefault)}
        />
        {section === "observe" && (
          <div id="nav-observe" className="flex flex-col gap-0.5 pl-2">
            {OBSERVE.map((n) => (
              <NavItem
                key={n.to}
                to={n.to}
                icon={n.icon}
                label={n.label}
                count={"count" in n && n.count ? badges[n.count] : undefined}
              />
            ))}
          </div>
        )}
      </nav>

      <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-3">
        <OrgRailItem />
        {isPlatformAdmin && <NavItem to="/admin" icon={<ShieldCheck size={16} />} label="Operator" />}
        <NavItem to="/settings" icon={<Settings size={16} />} label="Settings" />
      </div>
    </aside>
  );
}

function SectionHeader({
  label, expanded, controls, onOpen,
}: {
  label: string;
  expanded: boolean;
  controls: string;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={expanded}
      aria-controls={controls}
      onClick={onOpen}
      className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
    >
      <span className="flex-1 font-medium">{label}</span>
      <ChevronDown
        size={12}
        className={`text-faint transition-transform ${expanded ? "rotate-180" : ""}`}
      />
    </button>
  );
}

function OrgRailItem() {
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
  const reason = status && status.cloud_url.trim() ? "malformed" : "missing";
  return (
    <button
      type="button"
      onClick={() => navigate(`${settingsPath("deployment/sync")}?reason=${reason}`)}
      className="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-3 hover:text-fg-2"
    >
      <Building2 size={16} />
      <span className="flex-1">Organization</span>
    </button>
  );
}

function RailHeading({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-2 mt-5 px-2 font-mono text-[10px] uppercase tracking-wide text-faint">
      {children}
    </div>
  );
}

function NavItem({
  to,
  icon,
  label,
  count,
  soon,
  end,
}: {
  to?: string;
  icon: React.ReactNode;
  label: string;
  count?: number;
  soon?: boolean;
  /** Match this path exactly. `/org` would otherwise light up on every `/org/*` page. */
  end?: boolean;
}) {
  const base =
    "flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] transition-colors";
  if (soon || !to) {
    return (
      <div
        className={cn(base, "cursor-default text-faint-2")}
        title="Coming in a later pass"
      >
        {icon}
        <span className="flex-1">{label}</span>
        <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9px] uppercase text-faint-2">
          soon
        </span>
      </div>
    );
  }
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          base,
          isActive
            ? "bg-surface-3 text-fg"
            : "text-muted hover:bg-surface-3 hover:text-fg-2",
        )
      }
    >
      {icon}
      <span className="flex-1">{label}</span>
      {count != null && (
        <span className="rounded-md bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted">
          {count}
        </span>
      )}
    </NavLink>
  );
}
