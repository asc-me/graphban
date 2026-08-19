import * as React from "react";
import { Outlet, useLocation } from "react-router-dom";

import { ProjectProvider, useProjectCtx } from "@/features/ProjectContext";
import { DocsReader } from "@/features/docs/DocsReader";
import { CreateFirstOrg } from "@/features/onboarding/CreateFirstOrg";
import { CreateFirstProject } from "@/features/onboarding/CreateFirstProject";
import { useConfig, useOrgs } from "@/lib/queries";
import { ORG_BASE, clearOrgStateForSelfHost } from "@/lib/routes";

import { ProjectBar } from "./ProjectBar";

import { AgentSidebar } from "./AgentSidebar";
import { LeftNav } from "./LeftNav";
import { TopBar } from "./TopBar";

function Loading() {
  return (
    <div className="flex h-full items-center justify-center font-mono text-[12px] text-faint">
      loading…
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
  const [search, setSearch] = React.useState("");

  if (loading) return <Loading />;

  // The org plane is reachable without a project — indeed it is where you go to make one
  // — so the create-first-project gate applies only to the project plane.
  const onOrgPlane = hosted && pathname.startsWith(ORG_BASE);
  if (projects.length === 0 && !onOrgPlane) return <CreateFirstProject />;

  return (
    <div className="flex h-full flex-col">
      <TopBar
        agentOpen={agentOpen}
        onToggleAgent={() => setAgentOpen((v) => !v)}
        search={search}
        onSearch={setSearch}
      />
      <div className="flex min-h-0 flex-1">
        <LeftNav hosted={hosted} />
        <main className="relative flex min-w-0 flex-1 flex-col">
          {/* The project bar belongs to the project plane. On the org plane there is no
              active project in play, and showing one implies the page is scoped to it. */}
          {hosted && active && !onOrgPlane && <ProjectBar />}
          <div className="min-h-0 flex-1 overflow-auto">
            {notFound ? <ProjectNotFound /> : <Outlet context={search} />}
          </div>
        </main>
        <AgentSidebar open={agentOpen} onClose={() => setAgentOpen(false)} />
      </div>
      <DocsReader />
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
