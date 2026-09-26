import * as React from "react";
import { Outlet, useLocation } from "react-router-dom";

import { ProjectProvider, useProjectCtx } from "@/features/ProjectContext";
import { DocsReader } from "@/features/docs/DocsReader";
import { CreateFirstOrg } from "@/features/onboarding/CreateFirstOrg";
import { CreateFirstProject } from "@/features/onboarding/CreateFirstProject";
import { useConfig, useOrgs } from "@/lib/queries";
import { ORG_BASE, clearOrgStateForSelfHost } from "@/lib/routes";

import { cn } from "@/lib/cn";
import { useInputModality } from "@/lib/input-modality";

import { ProjectBar } from "./ProjectBar";

import { AgentSidebar } from "./AgentSidebar";
import { CommandPalette } from "./CommandPalette";
import { LeftNav } from "./LeftNav";
import { TopBar } from "./TopBar";

function Pulse({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-surface-3", className)} />;
}

function Loading() {
  return (
    <div className="flex h-full items-center justify-center text-[12px] text-faint">
      loading…
    </div>
  );
}

/** Auth/config rehydrate — chrome stays, main pane pulses (GRPH-917). */
export function ShellBootSkeleton() {
  return (
    <div className="flex h-full flex-col" aria-busy="true" aria-label="Loading session">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-[100] focus:rounded-lg focus:border focus:border-line-hover focus:bg-surface-3 focus:px-3 focus:py-2 focus:text-[13px] focus:text-fg"
      >
        Skip to main content
      </a>
      <div className="flex h-12 flex-none items-center gap-3 border-b border-line px-4">
        <Pulse className="h-6 w-28" />
        <Pulse className="ml-auto h-6 w-20" />
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="flex w-[216px] flex-none flex-col gap-1.5 border-r border-line px-3 py-4">
          {Array.from({ length: 9 }, (_, i) => (
            <Pulse key={i} className="h-8 w-full rounded-[10px]" />
          ))}
        </div>
        <main id="main-content" className="flex min-w-0 flex-1 flex-col p-5">
          <Pulse className="mb-2 h-7 w-40" />
          <Pulse className="mb-6 h-4 w-72 max-w-full" />
          <div className="space-y-2">
            {Array.from({ length: 6 }, (_, i) => (
              <Pulse key={i} className="h-12 w-full rounded-[12px]" />
            ))}
          </div>
        </main>
      </div>
    </div>
  );
}

export function AppFrame() {
  // In hosted mode a user must belong to an org before any project can exist (a
  // project is created under an org). Gate on that first, ahead of the project gate.
  const { data: config, isLoading: configLoading } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  const { data: orgs = [], isLoading: orgsLoading } = useOrgs(hosted);

  // A self-host build has no way to serve an org, so it must not resurrect an org
  // context left in storage by a hosted session (PRD-21 D1.2).
  React.useEffect(() => {
    if (config && !hosted) clearOrgStateForSelfHost();
  }, [config, hosted]);

  if (configLoading || (hosted && orgsLoading)) return <Loading />;
  if (hosted && orgs.length === 0) return <CreateFirstOrg />;

  return (
    <ProjectProvider>
      <FrameBody hosted={hosted} />
    </ProjectProvider>
  );
}

function FrameBody({ hosted }: { hosted: boolean }) {
  const { projects, loading, active, notFound } = useProjectCtx();
  const { pathname } = useLocation();
  const [agentOpen, setAgentOpen] = React.useState(true);
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  // Sticky modality at the moment of the toggle — keyboard Agent must not inherit a
  // later pointer event mid-slide (PRD-46 §6).
  const liveModality = useInputModality();
  const [agentMotion, setAgentMotion] = React.useState(liveModality);

  if (loading) return <Loading />;

  // The org plane is reachable without a project — indeed it is where you go to make one
  // — so the create-first-project gate applies only to the project plane.
  const onOrgPlane = hosted && pathname.startsWith(ORG_BASE);
  if (projects.length === 0 && !onOrgPlane) return <CreateFirstProject />;

  function toggleAgent() {
    setAgentMotion(liveModality);
    setAgentOpen((v) => !v);
  }

  return (
    <div className="flex h-full flex-col">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-[100] focus:rounded-lg focus:border focus:border-line-hover focus:bg-surface-3 focus:px-3 focus:py-2 focus:text-[13px] focus:text-fg"
      >
        Skip to main content
      </a>
      <TopBar
        agentOpen={agentOpen}
        onToggleAgent={toggleAgent}
        onOpenPalette={() => setPaletteOpen(true)}
      />
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <div className="flex min-h-0 flex-1">
        <LeftNav hosted={hosted} />
        <main id="main-content" className="relative flex min-w-0 flex-1 flex-col">
          {/* The project bar belongs to the project plane. On the org plane there is no
              active project in play, and showing one implies the page is scoped to it. */}
          {hosted && active && !onOrgPlane && <ProjectBar />}
          <div className="min-h-0 flex-1 overflow-auto">
            {notFound ? <ProjectNotFound /> : <Outlet />}
          </div>
        </main>
        <AgentSidebar
          open={agentOpen}
          onClose={() => {
            setAgentMotion(liveModality);
            setAgentOpen(false);
          }}
          modality={agentMotion}
        />
      </div>
      <DocsReader agentOpen={agentOpen} />
    </div>
  );
}

/**
 * A tag that resolves to nothing.
 *
 * Indistinguishable from a project that never existed, and deliberately so: a tag is a
 * lookup against what this caller can already read, never a grant. One belonging to
 * another org simply is not in the list, so guessing a short tag reveals nothing.
 */
function ProjectNotFound() {
  return (
    <div className="mx-auto max-w-[520px] px-6 py-16 text-center">
      <h1 className="text-[17px] font-semibold">No such project</h1>
      <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
        Nothing in your organizations answers to that tag. If a teammate sent you this link,
        they may need to give you access to the project first.
      </p>
    </div>
  );
}
