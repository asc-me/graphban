import { BarChart3, Building2, Check, ChevronDown, GitFork, Inbox, LayoutGrid, ListChecks, Map, Network, Plug, Plus, ScrollText, Settings, ShieldCheck, Sparkles, Star, Users } from "lucide-react";
import * as React from "react";
import { NavLink } from "react-router-dom";

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
import { cn } from "@/lib/cn";
import {
  useAutoActions,
  useCandidateShards,
  useConfig,
  useIsPlatformAdmin,
  useItems,
  useRequests,
} from "@/lib/queries";

export function LeftNav() {
  const { projects, active, activeId, setActiveId } = useProjectCtx();
  const { data: items } = useItems(activeId);
  const { data: requests } = useRequests(activeId);
  const { data: candidates } = useCandidateShards(activeId);
  // The reviewer's real backlog is candidates PLUS anything published without them
  // (AL-287). Counting only candidates reads as "no work" on a project whose agents
  // publish directly — which is exactly when there is most to look at.
  const { data: autoActions } = useAutoActions(activeId);
  const reviewCount =
    (candidates?.length ?? 0) +
    (autoActions ?? []).filter((s) => ["trusted", "agent"].includes(s.scoring_source)).length;
  const { data: config } = useConfig();
  // 404 (the non-admin case) resolves as an error, so this is false for tenants.
  const { data: adminMe } = useIsPlatformAdmin();
  const isPlatformAdmin = !!adminMe?.is_platform_admin;
  const [newProjectOpen, setNewProjectOpen] = React.useState(false);

  return (
    <aside className="flex w-[216px] flex-none flex-col border-r border-line bg-[rgba(9,11,13,0.5)] px-3 py-4">
      {/* Project switcher */}
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
        <DropdownMenuContent align="start" className="w-[204px]">
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

      <div className="mb-2 mt-5 px-2 font-mono text-[10px] uppercase tracking-wide text-faint">
        Workspace
      </div>
      <nav className="flex flex-col gap-0.5">
        <NavItem to="/tracker" icon={<ListChecks size={16} />} label="Tracker" count={items?.length} />
        <NavItem to="/requests" icon={<Star size={16} />} label="Requests" count={requests?.length} />
        <NavItem to="/dashboard" icon={<LayoutGrid size={16} />} label="Dashboard" />
        <NavItem to="/links" icon={<GitFork size={16} />} label="Links" />
        <NavItem to="/code" icon={<Network size={16} />} label="Code graph" />
        <NavItem to="/roadmap" icon={<Map size={16} />} label="Roadmap" />
        <NavItem to="/mcp-tools" icon={<Plug size={16} />} label="MCP Tools" />
        <NavItem to="/fleet" icon={<Users size={16} />} label="Fleet" />
        <NavItem to="/memory-review" icon={<Inbox size={16} />} label="Memory review" count={reviewCount || undefined} />
        <NavItem to="/activity" icon={<ScrollText size={16} />} label="Activity" />
        <NavItem to="/prds" icon={<BarChart3 size={16} />} label="PRDs" />
      </nav>

      <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-3">
        {config?.hosted_mode && (
          <NavItem to="/organization" icon={<Building2 size={16} />} label="Organization" />
        )}
        {isPlatformAdmin && (
          <NavItem to="/admin" icon={<ShieldCheck size={16} />} label="Operator" />
        )}
        <NavItem to="/feedback-kit" icon={<Sparkles size={16} />} label="Feedback Kit" />
        <NavItem to="/settings" icon={<Settings size={16} />} label="Settings" />
      </div>
    </aside>
  );
}

function NavItem({
  to,
  icon,
  label,
  count,
  soon,
}: {
  to?: string;
  icon: React.ReactNode;
  label: string;
  count?: number;
  soon?: boolean;
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
