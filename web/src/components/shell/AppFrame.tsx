import * as React from "react";
import { Outlet, useLocation } from "react-router-dom";

import { ProjectProvider, useProjectCtx } from "@/features/ProjectContext";
import { DocsReader } from "@/features/docs/DocsReader";
import { CreateFirstOrg } from "@/features/onboarding/CreateFirstOrg";
import { CreateFirstProject } from "@/features/onboarding/CreateFirstProject";
import { QuickStartDialog, useQuickStart } from "@/features/onboarding/QuickStartDialog";
import type { QsStep } from "@/features/onboarding/quickStartProgress";
import { AppTour } from "@/features/tour/AppTour";
import { onConnectStepRequested } from "@/features/home/SetupStatusCard";
import { onTourRequested } from "@/features/tour/replay";
import { useCompleteTour, useConfig, useMarkOnboarding, useOnboarding, useOrgs } from "@/lib/queries";
import { ORG_BASE, clearOrgStateForSelfHost } from "@/lib/routes";

import { cn } from "@/lib/cn";
import { useInputModality } from "@/lib/input-modality";

import { ProjectBar } from "./ProjectBar";

import { AgentSidebar } from "./AgentSidebar";
import { useAgentRail } from "./agentRail";
import { CommandPalette } from "./CommandPalette";
import { LeftNav, readSidebarCollapsed } from "./LeftNav";
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
        {/* The width the sidebar will open at, so a collapsed rail does not jump on boot. */}
        <div
          style={{ width: readSidebarCollapsed() ? 60 : 216 }}
          className="flex flex-none flex-col gap-1.5 border-r border-line px-3 py-4"
        >
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
  const [agentOpen, setAgentOpen] = useAgentRail(pathname);
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  // Sticky modality at the moment of the toggle — keyboard Agent must not inherit a
  // later pointer event mid-slide (PRD-46 §6).
  const liveModality = useInputModality();
  const [agentMotion, setAgentMotion] = React.useState(liveModality);

  // ── Quick start (GRPH-P67 D6/D8) ──
  // It opens by itself for a user whose onboarding is unset — a new user — never for one
  // backfilled as finished, and never again once they chose Finish later. The chip stays
  // until Finish; its count and reopen step come from server state.
  const onboardingQ = useOnboarding();
  const onboarding = onboardingQ.data?.onboarding ?? null;
  const finished = !!onboarding?.quickstart_finished_at;
  const shouldAutoOpen = !!onboardingQ.data && !finished && !onboarding?.quickstart_dismissed_at;
  const markOnboarding = useMarkOnboarding();
  const [qsOpen, setQsOpen] = React.useState(false);
  const [qsStep, setQsStep] = React.useState<QsStep | undefined>(undefined);
  const autoOpened = React.useRef(false);
  // ── App tour (GRPH-P67 §5) ── started from the quick start's Finish.
  const [tourOpen, setTourOpen] = React.useState(false);
  const { mutate: recordTour } = useCompleteTour();
  const exitTour = React.useCallback(() => setTourOpen(false), []);
  const finishTour = React.useCallback(
    (skipped: string[]) => {
      setTourOpen(false);
      recordTour(skipped);
    },
    [recordTour],
  );
  // Replay (GRPH-1364): Help ▸ Take the tour and the palette's "Take the tour". The tour
  // points at the nav, which only exists once there is a project.
  const hasProject = projects.length > 0;
  React.useEffect(
    () => (hasProject ? onTourRequested(() => { setPaletteOpen(false); setTourOpen(true); }) : undefined),
    [hasProject],
  );
  React.useEffect(
    () => onConnectStepRequested(() => { setQsStep("connect"); setQsOpen(true); }),
    [],
  );
  // ── One-time "Take the tour" toast (GRPH-1444, P67 G5/D6) ──
  // A user backfilled as finished never saw the tour. Offer it once; `tour_offered` is
  // stored in users.onboarding so it never repeats, whichever way the toast is closed.
  // Only a user already finished on the FIRST onboarding load of this mount is offered it:
  // a new user who finishes the quick start here (and maybe just started the tour from it)
  // flips `finished` mid-session and must not get the toast on top.
  const [tourToast, setTourToast] = React.useState(false);
  const toastShown = React.useRef(false);
  const finishedAtLoad = React.useRef<boolean | null>(null);
  if (onboardingQ.data && finishedAtLoad.current === null) finishedAtLoad.current = finished;
  const offerTour =
    !!onboardingQ.data &&
    finishedAtLoad.current === true &&
    !onboarding?.tour_completed_at &&
    !onboarding?.tour_offered_at &&
    hasProject;
  React.useEffect(() => {
    if (offerTour && !toastShown.current) {
      toastShown.current = true;
      setTourToast(true);
      markOnboarding.mutate("tour_offered");
    }
  }, [offerTour, markOnboarding]);
  const qs = useQuickStart(finished || !onboardingQ.data ? "" : active?.id ?? "", hosted);
  React.useEffect(() => {
    // With a project, wait for its setup read so the dialog opens at the first unfinished
    // step (as the chip does), not at ⓪. No project: nothing to read, open at the start.
    if (shouldAutoOpen && !loading && !autoOpened.current && (!active?.id || qs.setup)) {
      autoOpened.current = true;
      if (active?.id) setQsStep(qs.firstUnfinished);
      setQsOpen(true);
    }
  }, [shouldAutoOpen, loading, active?.id, qs.setup, qs.firstUnfinished]);

  if (loading || (projects.length === 0 && onboardingQ.isLoading)) return <Loading />;

  // The org plane is reachable without a project — indeed it is where you go to make one
  // — so the create-first-project gate applies only to the project plane.
  const onOrgPlane = hosted && pathname.startsWith(ORG_BASE);

  const quickStart = (
    <QuickStartDialog
      open={qsOpen}
      initialStep={qsStep}
      hosted={hosted}
      // The tour needs the nav it points at, which only exists once there is a project.
      onTakeTour={projects.length ? () => setTourOpen(true) : undefined}
      onFinishLater={() => {
        setQsOpen(false);
        if (!onboarding?.quickstart_dismissed_at && !finished) markOnboarding.mutate("quickstart_dismissed");
      }}
      onFinish={async () => {
        await markOnboarding.mutateAsync("quickstart_finished");
        setQsOpen(false);
      }}
    />
  );

  const setupChip =
    onboardingQ.data && !finished
      ? {
          ...qs.chip,
          onOpen: () => {
            setQsStep(qs.firstUnfinished);
            setQsOpen(true);
          },
        }
      : undefined;

  // A new user meets the quick start, which creates the project in step ① (with its
  // workspace confirmation). Finish later / Esc leaves an empty shell with the "Finish
  // setup" chip, never the old create form: that page would create a project without
  // recording the confirmation. CreateFirstProject stays only for a user whose onboarding
  // is finished (or unreadable) and who has no project.
  // The dialog sits at the same position in both branches, so creating the first project in
  // step ① swaps what is behind it without remounting it mid-flow.
  if (projects.length === 0 && !onOrgPlane) {
    return (
      <>
        {setupChip ? (
          <div className="flex h-full flex-col" data-testid="empty-shell">
            <TopBar
              agentOpen={false}
              onToggleAgent={() => {}}
              onOpenPalette={() => {}}
              setupChip={setupChip}
            />
            <main id="main-content" className="min-h-0 flex-1" />
          </div>
        ) : (
          <CreateFirstProject />
        )}
        {quickStart}
      </>
    );
  }

  function toggleAgent() {
    setAgentMotion(liveModality);
    setAgentOpen((v) => !v);
  }

  return (
    <>
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
          setupChip={setupChip}
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
      {quickStart}
      <AppTour open={tourOpen} onExit={exitTour} onComplete={finishTour} />
      {tourToast && (
        <div
          role="status"
          data-testid="tour-toast"
          className="fixed bottom-4 right-4 z-[60] flex items-center gap-3 rounded-lg border border-line-hover bg-surface-3 px-3 py-2 text-[13px] text-fg shadow-lg"
        >
          <span>New here? A short tour of the app.</span>
          <button
            type="button"
            className="rounded-md bg-accent px-2 py-1 text-[12px] font-medium text-black"
            onClick={() => { setTourToast(false); setTourOpen(true); }}
          >
            Take the tour
          </button>
          <button type="button" aria-label="Dismiss" className="text-faint" onClick={() => setTourToast(false)}>
            ×
          </button>
        </div>
      )}
    </>
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
      <p className="mt-2 text-body leading-relaxed text-muted">
        Nothing in your organizations answers to that tag. If a teammate sent you this link,
        they may need to give you access to the project first.
      </p>
    </div>
  );
}
