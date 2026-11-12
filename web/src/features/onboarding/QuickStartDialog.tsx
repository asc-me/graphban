/**
 * The quick-start dialog (GRPH-P67 §4, GRPH-1361): steps ⓪ Where, ① Project, ② Model,
 * ③ Connect, ④ First job, then Finish (③ is GRPH-1362).
 *
 * **Every mark on the rail is server state.** Next stores a human decision through
 * `POST …/setup/confirm` (or creates the project) and the rail redraws from what the server
 * answers; nothing here marks a step ✓ because it was clicked through. See
 * `quickStartProgress.ts` for the derivation and its three states.
 *
 * Radix `Dialog` traps focus and maps Esc to close, which is Finish later. Choices are native
 * radio inputs inside labels, so arrow keys and VoiceOver work without hand-rolled roving
 * tabindex.
 */
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { AGENTS, ConnectStep, ROUTE_LABEL, type Route } from "@/features/onboarding/ConnectStep";
import { ConceptsPanel, conceptCaption } from "@/features/onboarding/ConceptsPanel";
import { InviteeLinkCard } from "@/features/onboarding/InviteeLinkCard";
import { StartupPromptRows } from "@/features/onboarding/StartupPrompts";
import { TagField, useTagField } from "@/features/onboarding/TagField";
import { postureView, type PostureWarning } from "@/features/onboarding/posture";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  keys,
  useConfirmSetup,
  useCredentials,
  useInstancePosture,
  useOnboarding,
  useOrgs,
  useProjectSetup,
} from "@/lib/queries";
import type { Credential, SetupStatus } from "@/lib/types";

import {
  STEP_META,
  chipCount,
  countFrom,
  deriveMarks,
  firstUnfinished,
  inferWhere,
  normalizeAddress,
  servingChat,
  stepsFor,
  storedAddress,
  storedWhere,
  type QsStep,
  type StepMark,
} from "./quickStartProgress";

const ACCENTS = ["#c6f24e", "#a78bfa", "#7ca2ff", "#e0b34a", "#5fd07a", "#ff8f8f"];

type Where = "this_computer" | "dedicated_server";
type ModelChoice = "offline" | "ollama" | "claude";
type Screen = QsStep | "finish" | "welcome" | "sections" | "concepts" | "prompts";

/** Guides Help can open the dialog on directly (GRPH-1512). */
export type QsGuide = "concepts" | "prompts" | "sections";

/** Steps, marks and chip count for one project — the dialog and the top-bar chip share it. */
export function useQuickStart(projectId: string, hosted: boolean, pollMs?: number) {
  const setupQ = useProjectSetup(projectId, pollMs);
  const credQ = useCredentials(projectId);
  const steps = stepsFor(hosted);
  const marks = deriveMarks({
    projectId,
    setup: setupQ.data,
    setupError: setupQ.error ?? undefined,
    credentials: credQ.data?.credentials,
    credentialsError: credQ.error ?? undefined,
  });
  return {
    steps,
    marks,
    setup: setupQ.data,
    /** The setup read has answered, with data or an error. The auto-open waits on this,
     *  not on data, so a failed read still opens the dialog (at ⓪). */
    setupSettled: !!setupQ.data || !!setupQ.error,
    credentials: credQ.data?.credentials ?? [],
    chip: chipCount(steps, marks),
    firstUnfinished: firstUnfinished(steps, marks),
  };
}

const GLYPH: Record<SetupStatus, { g: string; word: string; cls: string }> = {
  done: { g: "✓", word: "done", cls: "text-st-done" },
  pending: { g: "◌", word: "pending", cls: "text-muted" },
  unknown: { g: "?", word: "unknown", cls: "text-faint" },
};

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function QuickStartDialog({
  open,
  initialStep,
  initialGuide,
  hosted,
  onFinishLater,
  onFinish,
  onTakeTour,
}: {
  open: boolean;
  /** Where to open; the chip passes the first unfinished step. */
  initialStep?: QsStep;
  /** Help opens the dialog on a guide: Concepts (at concept 1), Start-up prompts, or Sections (the way back in after Finish). */
  initialGuide?: QsGuide;
  hosted: boolean;
  /** Esc, ✕ or "Finish later". */
  onFinishLater: () => void;
  /** Finish: records `quickstart_finished`; rejects to keep the dialog open with an error. */
  onFinish: () => Promise<void>;
  /** The app tour (GRPH-1363). Absent until it exists, so no button points at nothing. */
  onTakeTour?: () => void;
}) {
  const { active, projects } = useProjectCtx();
  // An invitee joined someone else's org (GRPH-1364): their Finish offers the link setup card
  // and the start-up prompts. The owner of the org, and every self-host user, get the plain one.
  const orgsQ = useOrgs(hosted);
  const invitee = hosted && (orgsQ.data ?? []).some((o) => o.role !== "owner");
  const [createdId, setCreatedId] = React.useState("");
  const projectId = active?.id ?? createdId;
  const project = projects.find((p) => p.id === projectId) ?? active;

  // First open shows Welcome (GRPH-P67 v1.1 R1); every later open, the chip included, lands
  // on Sections. A dialog that was ever closed has recorded `quickstart_dismissed`, so the
  // server fact and the in-session ref agree.
  const onboardingQ = useOnboarding();
  const welcomed = React.useRef(false);
  const landing = (): Screen => {
    if (initialGuide) return initialGuide;
    if (!welcomed.current && !onboardingQ.data?.onboarding?.quickstart_dismissed_at) return "welcome";
    return "sections";
  };
  const [screen, setScreen] = React.useState<Screen>(landing);
  const [concept, setConcept] = React.useState(0);
  const qs = useQuickStart(projectId, hosted, open && (screen === "first_job" || screen === "connect") ? 3000 : undefined);
  const steps = qs.steps;

  // Opening lands on Welcome the first time and on Sections after that.
  React.useEffect(() => {
    if (open) {
      const next = landing();
      if (next === "welcome") welcomed.current = true;
      setScreen(next);
      setConcept(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialStep, initialGuide]);

  // Step ⓪'s answer, held until a project exists to store it on (a brand-new user answers
  // ⓪ before ① creates the project). It is NOT a mark: the rail stays ◌ until stored.
  const [pendingWhere, setPendingWhere] = React.useState<{ where: Where; address: string } | null>(null);

  const [agent, setAgent] = React.useState("claude");
  const [route, setRoute] = React.useState<Route>("gban");
  const whereStep = qs.setup?.steps.find((s) => s.step === 0);

  const idx = screen === "finish" ? steps.length : steps.indexOf(screen as QsStep);
  const isLast = idx === steps.length - 1;
  const go = (s: Screen) => setScreen(s);
  const back = () => (idx > 0 ? go(steps[idx - 1]) : go("sections"));
  const takeTour = onTakeTour
    ? () => {
        onFinishLater();
        onTakeTour();
      }
    : undefined;
  const advance = () => go(isLast ? "finish" : steps[idx + 1]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onFinishLater()}>
      <DialogContent
        overlayClassName="bg-black/30 backdrop-blur-none"
        className="max-h-[92vh] max-w-[960px] overflow-y-auto p-0"
        aria-describedby="qs-desc"
        // With no project the page behind is an empty shell, so a stray focus or pointer move
        // outside must not dismiss the dialog (GRPH-1542); Esc, ✕ and Finish later still do.
        onFocusOutside={projects.length === 0 ? (e) => e.preventDefault() : undefined}
        onInteractOutside={projects.length === 0 ? (e) => e.preventDefault() : undefined}
      >
        <div className="flex min-h-0">
        <Hero steps={steps} marks={qs.marks} screen={screen} conceptIndex={concept} />
        <div className="min-w-0 flex-1">
        <div className="border-b border-line px-5 pb-3 pt-4">
          <DialogTitle className="font-mono text-small uppercase tracking-[0.08em] text-faint">
            Quick start
          </DialogTitle>
          <DialogDescription id="qs-desc" className="sr-only">
            Set up Graphban in a few steps. A step is marked done only when the server can show it.
            Press Escape to finish later.
          </DialogDescription>
          <Rail steps={steps} marks={qs.marks} current={screen} />
          <UnknownRows steps={steps} marks={qs.marks} />
        </div>

        <div className="px-5 py-4">
          {screen === "welcome" && <WelcomePanel onStart={() => go("sections")} onFinishLater={onFinishLater} />}
          {screen === "sections" && (
            <SectionsPanel steps={steps} marks={qs.marks} onOpen={go} onTour={takeTour} onConcepts={() => { setConcept(0); go("concepts"); }} onFinishLater={onFinishLater} />
          )}
          {screen === "concepts" && (
            <ConceptsPanel
              index={concept}
              onIndex={setConcept}
              onExit={() => go("sections")}
              onFinish={() => go("sections")}
              onFinishLater={onFinishLater}
            />
          )}
          {screen === "prompts" && (
            <div data-testid="qs-prompts">
              <h2 className="mb-2 text-body font-semibold">Start-up prompts</h2>
              <StartupPromptRows />
              <div className="mt-4 flex justify-between">
                <Button variant="outline" type="button" onClick={() => go("sections")}>Back</Button>
                <Button variant="ghost" type="button" onClick={onFinishLater}>Finish later</Button>
              </div>
            </div>
          )}
          {screen === "where" && (
            <WhereStep
              projectId={projectId}
              stored={storedWhere(qs.setup?.steps.find((s) => s.step === 0))}
              pending={pendingWhere}
              onHold={setPendingWhere}
              onDone={advance}
              onBack={back}
              onFinishLater={onFinishLater}
              first={idx === 0}
            />
          )}
          {screen === "project" && (
            <ProjectStep
              projectId={projectId}
              projectName={project?.name ?? ""}
              projectTag={project?.tag ?? ""}
              confirmed={qs.marks.project.status === "done"}
              pendingWhere={pendingWhere}
              where={pendingWhere?.where ?? storedWhere(qs.setup?.steps.find((s) => s.step === 0))}
              repoUrl={project?.repo_url ?? ""}
              onCreated={(id) => {
                setCreatedId(id);
                setPendingWhere(null);
              }}
              onDone={advance}
              onBack={back}
              onFinishLater={onFinishLater}
              first={idx === 0}
            />
          )}
          {screen === "model" && (
            <ModelStep
              projectId={projectId}
              offline={!!qs.setup?.offline}
              credentials={qs.credentials}
              where={pendingWhere?.where ?? storedWhere(qs.setup?.steps.find((s) => s.step === 0)) ?? "this_computer"}
              onDone={advance}
              onBack={back}
              onFinishLater={onFinishLater}
            />
          )}
          {screen === "connect" && (
            <ConnectStep
              projectId={projectId}
              address={storedAddress(whereStep) || (pendingWhere?.where === "dedicated_server" ? pendingWhere.address : "")}
              dedicated={(storedWhere(whereStep) ?? pendingWhere?.where) === "dedicated_server"}
              repoUrl={project?.repo_url ?? ""}
              mark={qs.marks.connect}
              agent={agent}
              onAgent={setAgent}
              route={route}
              onRoute={setRoute}
            >
              <Advanced>
                <Hint lead="Key scope.">This key is pinned to this project. Mint a global key under Settings → Keys.</Hint>
                <Hint lead="Tool tiers.">The key sees the core tools; add tiers (fleet, prd) when you set up delegation.</Hint>
                <Hint lead="More machines.">Repeat this step on each machine; every key proves its own first call.</Hint>
                <Hint lead="Cursor / GUI apps.">GUI apps don't inherit shell environment variables; put the key in the app's own config.</Hint>
                <Hint lead="Delegation and sandboxes.">Run gban setup on the machine that hosts the supervisor; sandboxed agents need the server reachable from inside the sandbox.</Hint>
              </Advanced>
              <Footer onFinishLater={onFinishLater} onBack={back} next={advance} />
            </ConnectStep>
          )}
          {screen === "first_job" && (
            <FirstJobStep
              hosted={hosted}
              agentLabel={AGENTS.find((a) => a.id === agent)?.label ?? "your agent"}
              mark={qs.marks.first_job}
              setupSteps={qs.setup?.steps ?? []}
              isLast={isLast}
              tourAvailable={!!onTakeTour}
              onDone={advance}
              onBack={back}
              onFinishLater={onFinishLater}
            />
          )}
          {screen === "finish" && (
            <FinishScreen
              steps={steps}
              marks={qs.marks}
              route={route}
              onFinish={onFinish}
              onTakeTour={onTakeTour}
              invitee={invitee}
            />
          )}
        </div>
        </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Hero ───────────────────────────────────────────────────────────────────────────────

export type HeroState = "done" | "pending" | "unknown";

/** The hero shows the same fact as the rail, for the current screen. Finish rolls the steps up. */
export function heroState(steps: QsStep[], marks: Record<QsStep, StepMark>, screen: Screen): HeroState {
  if (screen !== "finish" && screen !== "welcome" && screen !== "sections" && screen !== "concepts" && screen !== "prompts") return marks[screen].status;
  const all = steps.map((s) => marks[s].status);
  if (all.every((x) => x === "done")) return "done";
  return all.includes("unknown") ? "unknown" : "pending";
}

// Only a step that is actively waiting on something live may flow: Connect polling for the
// first call, First job counting. Unknown and done never move.
const LIVE_STEPS: Screen[] = ["connect", "first_job"];

// Theme tokens (index.css @theme): lit accent, muted line, static grey. No amber.
const HERO_STROKE: Record<HeroState, string> = {
  done: "var(--color-accent)",
  pending: "var(--color-line)",
  unknown: "var(--color-faint)",
};

export function Hero({
  steps,
  marks,
  screen,
  conceptIndex = 0,
}: {
  steps: QsStep[];
  marks: Record<QsStep, StepMark>;
  screen: Screen;
  conceptIndex?: number;
}) {
  const state = heroState(steps, marks, screen);
  const flowing = state === "pending" && LIVE_STEPS.includes(screen);
  const caption =
    screen === "concepts"
      ? conceptCaption(conceptIndex)
      : screen === "finish" || screen === "welcome" || screen === "sections" || screen === "prompts"
        ? "Setup"
        : STEP_META[screen].label;
  return (
    <aside
      aria-hidden
      data-testid="qs-hero"
      data-hero-state={state}
      data-flowing={flowing ? "true" : "false"}
      className="hidden w-[380px] flex-none flex-col justify-between border-r border-line bg-surface-2 p-6 min-[1000px]:flex"
    >
      <svg viewBox="0 0 320 120" className="w-full" fill="none">
        <path
          d="M10 100 C 90 100, 90 20, 160 20 S 230 100, 310 100"
          stroke={HERO_STROKE[state]}
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={state === "pending" ? "4 8" : undefined}
          className={flowing ? "qs-hero-flow" : undefined}
        />
      </svg>
      <p className="font-mono text-small uppercase tracking-[0.08em] text-faint">
        {caption}
        {screen === "concepts" ? "" : ` · ${state.toUpperCase()}`}
      </p>
    </aside>
  );
}

// ── Welcome and Sections ───────────────────────────────────────────────────────────────

function WelcomePanel({ onStart, onFinishLater }: { onStart: () => void; onFinishLater: () => void }) {
  return (
    <section aria-labelledby="qs-welcome-h">
      <h2 id="qs-welcome-h" className="text-[16px] font-semibold">
        Welcome to Graphban
      </h2>
      <p className="mt-2 text-[13px] text-muted">
        A few short sections get you set up, show you around and explain the ideas behind it. Do them in any order, and
        come back to any of them from Finish setup, or from Help once you&apos;re done.
      </p>
      <div className="mt-5 flex items-center gap-2 border-t border-line pt-4">
        <Button variant="ghost" size="sm" type="button" onClick={onFinishLater}>
          Finish later
        </Button>
        <div className="flex-1" />
        <Button size="sm" type="button" onClick={onStart}>
          Get started →
        </Button>
      </div>
    </section>
  );
}

function SectionsPanel({
  steps,
  marks,
  onOpen,
  onTour,
  onConcepts,
  onFinishLater,
}: {
  steps: QsStep[];
  marks: Record<QsStep, StepMark>;
  onOpen: (s: QsStep) => void;
  onConcepts: () => void;
  /** Absent until a project exists for the tour to point at. */
  onTour?: () => void;
  onFinishLater: () => void;
}) {
  const row = "flex w-full items-center gap-3 rounded-[10px] border border-control px-3 py-2 text-left";
  return (
    <section aria-labelledby="qs-sections-h">
      <h2 id="qs-sections-h" className="text-[16px] font-semibold">
        Sections
      </h2>
      <ul className="mt-3 space-y-2" aria-label="Sections">
        {steps.map((s) => {
          const g = GLYPH[marks[s].status];
          return (
            <li key={s}>
              <button
                type="button"
                data-section={s}
                data-status={marks[s].status}
                className={cn(row, "hover:bg-surface-2")}
                onClick={() => onOpen(s)}
              >
                <span className={g.cls} aria-hidden>
                  {g.g}
                </span>
                <span className="flex-1 text-[13px]">
                  <span className="font-mono text-faint">{STEP_META[s].num}</span> {STEP_META[s].label}
                  <span className="sr-only"> — {g.word}</span>
                </span>
                <span className="font-mono text-[10px] uppercase tracking-wide text-faint" aria-hidden>
                  {g.word}
                </span>
              </button>
            </li>
          );
        })}
        <li>
          <button
            type="button"
            data-section="tour"
            className={cn(row, onTour ? "hover:bg-surface-2" : "opacity-60")}
            disabled={!onTour}
            onClick={onTour}
          >
            <span className="flex-1 text-[13px]">
              <span className="font-semibold">Tour</span>
              <span className="block text-[12px] text-muted">
                Eight stops around the app, ending at Organization. About two minutes.
              </span>
            </span>
            <span className="font-mono text-[10px] uppercase tracking-wide text-faint">~2 MIN</span>
          </button>
        </li>
        <li>
          <button type="button" data-section="concepts" className={cn(row, "hover:bg-surface-2")} onClick={onConcepts}>
            <span className="flex-1 text-[13px]">
              <span className="font-semibold">Graphban concepts</span>
              <span className="block text-[12px] text-muted">
                The loop, the tracker, specs, memory, the code graph and rules.
              </span>
            </span>
            <span className="font-mono text-[10px] uppercase tracking-wide text-faint">6 CONCEPTS</span>
          </button>
        </li>
      </ul>
      <div className="mt-5 flex items-center border-t border-line pt-4">
        <Button variant="ghost" size="sm" type="button" onClick={onFinishLater}>
          Finish later
        </Button>
      </div>
    </section>
  );
}

// ── Rail ───────────────────────────────────────────────────────────────────────────────

function Rail({ steps, marks, current }: { steps: QsStep[]; marks: Record<QsStep, StepMark>; current: Screen }) {
  return (
    <ol aria-label="Setup steps" className="mt-3 flex gap-1.5">
      {steps.map((s) => {
        const g = GLYPH[marks[s].status];
        const cur = s === current;
        return (
          <li
            key={s}
            aria-current={cur ? "step" : undefined}
            data-step={s}
            data-status={marks[s].status}
            className={cn(
              "min-w-0 flex-1 border-t-2 pt-1.5",
              cur ? "border-accent" : "border-line-2",
            )}
          >
            <div className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wide">
              <span className={cur ? "text-accent" : "text-faint"}>{STEP_META[s].num}</span>
              <span className={g.cls} aria-hidden>
                {g.g}
              </span>
              <span className="text-faint" aria-hidden>
                {g.word}
              </span>
            </div>
            <div className={cn("truncate text-[12px]", cur ? "font-semibold text-fg" : "text-muted")}>
              {STEP_META[s].label}
              <span className="sr-only"> — {g.word}</span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function UnknownRows({ steps, marks }: { steps: QsStep[]; marks: Record<QsStep, StepMark> }) {
  const rows = steps.filter((s) => marks[s].status === "unknown");
  if (!rows.length) return null;
  // Steps that share one reason (a downed API makes every step unknown for the same cause) are
  // one line, not the same sentence five times (GRPH-1547).
  const groups: { steps: QsStep[]; reason: string }[] = [];
  for (const s of rows) {
    const reason = marks[s].reason ?? "";
    const g = groups.find((x) => x.reason === reason);
    if (g) g.steps.push(s);
    else groups.push({ steps: [s], reason });
  }
  return (
    <ul className="mt-2 space-y-0.5" aria-label="Steps that could not be checked">
      {groups.map((g) => (
        <li key={g.steps.join(",")} className="text-[11.5px] text-muted">
          <span className="font-mono text-faint">
            {g.steps.map((s) => STEP_META[s].num).join(" ")} ? UNKNOWN
          </span>{" "}
          {g.reason}
        </li>
      ))}
    </ul>
  );
}

// ── Shared bits ────────────────────────────────────────────────────────────────────────

function Footer({
  onFinishLater,
  onBack,
  backDisabled,
  next,
  nextLabel = "Next →",
  nextDisabled,
  nextWhy,
  busy,
}: {
  onFinishLater: () => void;
  onBack: () => void;
  backDisabled?: boolean;
  next: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  nextWhy?: string;
  busy?: boolean;
}) {
  return (
    <div className="mt-5 flex items-center gap-2 border-t border-line pt-4">
      <Button variant="ghost" size="sm" type="button" onClick={onFinishLater}>
        Finish later
      </Button>
      <div className="flex-1" />
      {nextDisabled && nextWhy && (
        <span id="qs-next-why" className="text-[11.5px] text-faint">
          {nextWhy}
        </span>
      )}
      <Button variant="outline" size="sm" type="button" onClick={onBack} disabled={backDisabled}>
        ← Back
      </Button>
      <Button
        size="sm"
        type="button"
        onClick={next}
        disabled={nextDisabled || busy}
        loading={busy}
        aria-describedby={nextDisabled && nextWhy ? "qs-next-why" : undefined}
      >
        {nextLabel}
      </Button>
    </div>
  );
}

function Advanced({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  const id = React.useId();
  return (
    <div className="mt-4">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 rounded text-[12px] text-muted outline-none hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        <ChevronRight size={13} aria-hidden className={cn("transition-transform", open && "rotate-90")} />
        Advanced
      </button>
      {open && (
        <div id={id} className="mt-2 space-y-2 rounded-[10px] border border-line bg-surface-2 p-3 text-[12px] text-muted">
          {children}
        </div>
      )}
    </div>
  );
}

function Hint({ lead, children }: { lead: string; children: React.ReactNode }) {
  return (
    <p>
      <span className="font-semibold text-fg">{lead}</span> {children}
    </p>
  );
}

function ErrorLine({ text }: { text: string }) {
  if (!text) return null;
  return (
    <p role="alert" className="mt-3 text-[12px] text-st-blocked">
      {text}
    </p>
  );
}

function Choice({
  name,
  value,
  checked,
  onChange,
  title,
  sub,
  children,
}: {
  name: string;
  value: string;
  checked: boolean;
  onChange: (v: string) => void;
  title: string;
  sub?: string;
  children?: React.ReactNode;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer gap-2.5 rounded-[10px] border p-3 transition-colors",
        checked ? "border-accent bg-[rgba(198,242,78,0.05)]" : "border-line-2 bg-surface-2",
      )}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        onChange={() => onChange(value)}
        className="mt-0.5 accent-[#c6f24e] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      />
      <span className="min-w-0">
        <span className="block text-[13px] font-semibold text-fg">{title}</span>
        {sub && <span className="block text-[12px] text-muted">{sub}</span>}
        {children}
      </span>
    </label>
  );
}

export function CopyLine({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-start gap-2 rounded-[8px] border border-line bg-bg p-2.5">
      <code className="min-w-0 flex-1 whitespace-pre-wrap break-words font-mono text-[12px] text-fg">{text}</code>
      <Button
        variant="outline"
        size="sm"
        type="button"
        aria-label={label}
        onClick={() => {
          void navigator.clipboard?.writeText(text).catch(() => undefined);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
      </Button>
    </div>
  );
}

function StatusLine({ status, text }: { status: SetupStatus; text: string }) {
  const g = GLYPH[status];
  return (
    <p className="mt-2 text-[12px] text-muted" role="status">
      <span className={cn("mr-1.5 font-mono", g.cls)} aria-hidden>
        {g.g}
      </span>
      {text}
    </p>
  );
}

// ── ⓪ Where ────────────────────────────────────────────────────────────────────────────

const WHERE_CHANGES: [string, string, string][] = [
  ["Commands in ③", "Use localhost.", 'Use the entered server address, with "run this curl from your dev machine to check".'],
  [
    "Safety check (①)",
    "Quiet; loopback-only boxes aren't exposed, and any warnings collapse into one line.",
    "Active; JWT_SECRET, SIGNUP_MODE=invite_only, SECRET_ENCRYPTION_KEY and REQUIRE_STRONG_SECRET become main-path warnings, and TRUSTED_HOPS/HTTPS go under Advanced.",
  ],
  [
    "Ollama URL (②)",
    "http://host.docker.internal:11434 under compose, localhost natively.",
    "Asks where Ollama runs (on the server, or elsewhere on the network).",
  ],
  [
    "Machines (③)",
    'One machine, this one; the "more machines" hint stays in Advanced.',
    'Multi-machine by design; the "run this on each dev machine" copy moves to the main path.',
  ],
  [
    "Code-graph pass (④)",
    "Runs locally, no notes.",
    "The agent runs on the dev machine and talks to the server over the network, and the prompt says so.",
  ],
];

function WhereStep({
  projectId,
  stored,
  pending,
  onHold,
  onDone,
  onBack,
  onFinishLater,
  first,
}: {
  projectId: string;
  stored: Where | null;
  pending: { where: Where; address: string } | null;
  onHold: (v: { where: Where; address: string }) => void;
  onDone: () => void;
  onBack: () => void;
  onFinishLater: () => void;
  first: boolean;
}) {
  const origin = window.location.origin;
  const inferred = inferWhere(origin);
  const [where, setWhere] = React.useState<Where>(pending?.where ?? stored ?? inferred.where);
  const [address, setAddress] = React.useState(pending?.address || inferred.address);
  const [error, setError] = React.useState("");
  const confirm = useConfirmSetup();

  const a = normalizeAddress(address);
  // The only reachability this browser can prove is the origin it loaded from. Anything else
  // is unchecked — and the copy says that rather than implying the dev machine can reach it.
  const reach: { status: SetupStatus; text: string } = !a
    ? { status: "unknown", text: "Unknown — no address yet." }
    : a === normalizeAddress(origin)
      ? { status: "done", text: "reachable from this browser" }
      : { status: "unknown", text: `Unknown — not checked. This page loaded from ${origin}, not this address.` };

  async function next() {
    setError("");
    if (where === "dedicated_server" && !a) {
      setError("Enter the server address your dev machines use.");
      return;
    }
    const answer = { where, address: where === "dedicated_server" ? a : "" };
    if (!projectId) {
      // No project yet: hold it, store it the moment ① creates one.
      onHold(answer);
      onDone();
      return;
    }
    try {
      await confirm.mutateAsync({ projectId, body: { decision: "server", ...answer } });
      onDone();
    } catch (e) {
      setError(`Could not save your answer: ${errText(e)}`);
    }
  }

  return (
    <section aria-labelledby="qs-where-h">
      <h2 id="qs-where-h" className="text-[16px] font-semibold">
        Where is Graphban running?
      </h2>
      <fieldset className="mt-3 space-y-2">
        <legend className="sr-only">Where is Graphban running?</legend>
        <Choice name="qs-where" value="this_computer" checked={where === "this_computer"} onChange={(v) => setWhere(v as Where)} title="On this computer" sub="the one I code on">
          {inferred.where === "this_computer" && (
            <span className="mt-1 block text-[11.5px] text-faint">Pre-selected: {inferred.evidence}.</span>
          )}
        </Choice>
        <Choice name="qs-where" value="dedicated_server" checked={where === "dedicated_server"} onChange={(v) => setWhere(v as Where)} title="On a dedicated server" sub="I code on other machines">
          {inferred.where === "dedicated_server" && (
            <span className="mt-1 block text-[11.5px] text-faint">Pre-selected: {inferred.evidence}.</span>
          )}
        </Choice>
      </fieldset>
      {where === "dedicated_server" && (
        <div className="mt-3">
          <label htmlFor="qs-server-address" className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Server address
          </label>
          <Input
            id="qs-server-address"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="http://ubuntu-srv:8080"
            aria-describedby="qs-reach"
          />
          <div id="qs-reach">
            <StatusLine status={reach.status} text={reach.text} />
            <p className="mt-1 text-[11.5px] text-faint">
              That proves the browser can reach it, not your dev machine. Step ③ gives you a curl to run there.
            </p>
          </div>
        </div>
      )}
      <p className="mt-3 text-[12px] text-faint">
        Your answer sets the commands and checks in later steps. You can change it any time.
      </p>
      <ErrorLine text={error} />
      <Advanced>
        <table className="w-full border-collapse text-left text-[11.5px]">
          <caption className="mb-1.5 text-left font-semibold text-fg">What changes</caption>
          <thead>
            <tr className="font-mono text-[10px] uppercase text-faint">
              <th scope="col" className="pb-1 pr-2" />
              <th scope="col" className={cn("pb-1 pr-2", where === "this_computer" && "text-accent")}>This computer</th>
              <th scope="col" className={cn("pb-1", where === "dedicated_server" && "text-accent")}>Dedicated server</th>
            </tr>
          </thead>
          <tbody>
            {WHERE_CHANGES.map(([k, a1, b1]) => (
              <tr key={k} className="border-t border-line align-top">
                <th scope="row" className="py-1.5 pr-2 font-semibold text-fg">{k}</th>
                <td className="py-1.5 pr-2">{a1}</td>
                <td className="py-1.5">{b1}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Advanced>
      <Footer onFinishLater={onFinishLater} onBack={onBack} backDisabled={first} next={next} busy={confirm.isPending} />
    </section>
  );
}

// ── ① Project ──────────────────────────────────────────────────────────────────────────

function ProjectStep({
  projectId,
  projectName,
  projectTag,
  confirmed,
  pendingWhere,
  where,
  repoUrl,
  onCreated,
  onDone,
  onBack,
  onFinishLater,
  first,
}: {
  projectId: string;
  projectName: string;
  projectTag: string;
  confirmed: boolean;
  pendingWhere: { where: Where; address: string } | null;
  where: Where | null;
  repoUrl: string;
  onCreated: (id: string) => void;
  onDone: () => void;
  onBack: () => void;
  onFinishLater: () => void;
  first: boolean;
}) {
  const qc = useQueryClient();
  const confirm = useConfirmSetup();
  const creating = !projectId;
  const [name, setName] = React.useState("");
  const [accent, setAccent] = React.useState(ACCENTS[0]);
  const [ticked, setTicked] = React.useState(confirmed);
  const [repo, setRepo] = React.useState(repoUrl);
  const posture = useInstancePosture();
  // An unreadable posture (403, offline) is hidden, never drawn as "all clear".
  const view = postureView(posture.isSuccess ? posture.data : null, where);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const tagField = useTagField(name);

  React.useEffect(() => {
    if (confirmed) setTicked(true);
  }, [confirmed]);

  async function next() {
    setError("");
    if (creating && !name.trim()) {
      setError("Name the project first.");
      return;
    }
    setBusy(true);
    try {
      let id = projectId;
      if (creating) {
        if (tagField.blocked) {
          setError("That tag is taken. Pick another under Advanced.");
          setBusy(false);
          return;
        }
        const p = await api.createProject({ name: name.trim(), tag: tagField.tag || undefined, accent });
        id = p.id;
        onCreated(id);
      }
      // The checkbox IS setup_project step 1: the human confirmation agents see as done too.
      if (!confirmed) await confirm.mutateAsync({ projectId: id, body: { decision: "workspace" } });
      if (pendingWhere) {
        await confirm.mutateAsync({
          projectId: id,
          body: { decision: "server", where: pendingWhere.where, address: pendingWhere.address },
        });
      }
      if (repo.trim() !== repoUrl.trim()) {
        await api.updateProject(id, { repo_url: repo.trim() });
        await qc.invalidateQueries({ queryKey: keys.projects });
      }
      if (creating) await qc.invalidateQueries({ queryKey: keys.projects });
      onDone();
    } catch (e) {
      setError(creating ? `Could not create the project: ${errText(e)}` : `Could not save the confirmation: ${errText(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="qs-project-h">
      <h2 id="qs-project-h" className="text-[16px] font-semibold">
        {creating ? "Create your project" : "Confirm your project"}
      </h2>
      {creating ? (
        <div className="mt-3">
          <label htmlFor="qs-project-name" className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Project name
          </label>
          <Input
            id="qs-project-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Core Platform"
            required
          />
          {tagField.tag && (
            <p className="mt-1 font-mono text-small text-faint">
              Tag {tagField.tag}
            </p>
          )}
        </div>
      ) : (
        <p className="mt-2 text-body text-muted">
          You're set to write into <span className="font-semibold text-fg">{projectName}</span>
          {projectTag && <span className="font-mono text-faint"> ({projectTag})</span>}.
        </p>
      )}

      <div className="mt-4 rounded-[10px] border border-line-2 bg-surface-2 p-3">
        <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-accent">Your step</p>
        <label className="flex cursor-pointer items-start gap-2.5 text-[13px]">
          <input
            type="checkbox"
            checked={ticked}
            onChange={(e) => setTicked(e.target.checked)}
            aria-describedby="qs-confirm-why"
            className="mt-0.5 accent-[#c6f24e]"
          />
          <span>
            <span className="block font-semibold text-fg">This is the workspace I want agents writing to</span>
            <span id="qs-confirm-why" className="block text-[12px] text-muted">
              Writing into the wrong project pollutes another one.
            </span>
          </span>
        </label>
      </div>
      <ErrorLine text={error} />
      {view && (view.main.length > 0 || view.collapsed) && (
        <div role="status" className="mt-4 rounded-[10px] border border-line-2 p-3 text-[12px]">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Is this box safe to expose?</p>
          {view.collapsed ? (
            <p className="text-muted">{view.collapsed} Details are under Advanced.</p>
          ) : (
            <ul className="space-y-2">
              {view.main.map((w) => (
                <li key={w.id}>
                  <p className="text-muted">{w.text}</p>
                  <CopyLine text={w.env} label={`Copy ${w.id} fix`} />
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1.5 text-faint">Warnings only; nothing here blocks you.</p>
        </div>
      )}

      <Advanced>
        <div>
          <label htmlFor="qs-repo" className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Repository
          </label>
          <Input
            id="qs-repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="https://github.com/acme/app"
            aria-describedby="qs-repo-why"
          />
          <p id="qs-repo-why" className="mt-1 text-[11.5px] text-faint">
            <code>gban setup</code> uses this to find the project from a checkout's git remote.
          </p>
        </div>
        {view?.collapsed && view.advanced.map((w: PostureWarning) => (
          <div key={w.id}>
            <p>{w.text}</p>
            <CopyLine text={w.env} label={`Copy ${w.id} fix`} />
          </div>
        ))}
        {view?.proxyHint && (
          <Hint lead="Behind a proxy or HTTPS?">
            Set <code>TRUSTED_HOPS</code> to the number of proxies in front of this box, and serve it over HTTPS.
          </Hint>
        )}
        {creating && (
          <>
            <TagField tag={tagField.tag} setTag={tagField.setTag} status={tagField.status} />
            <div>
              <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Accent</p>
              <div className="flex flex-wrap gap-2.5">
                {ACCENTS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => setAccent(c)}
                    aria-label={`accent ${c}`}
                    aria-pressed={accent === c}
                    className="h-6 w-6 rounded-[7px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                    style={{ background: c, outline: accent === c ? "2px solid var(--color-fg)" : "none", outlineOffset: 2 }}
                  />
                ))}
              </div>
            </div>
            <Hint lead="Tag and accent.">The tag prefixes every item id. It's derived from the name until you edit it.</Hint>
          </>
        )}
        <Hint lead="More projects.">This dialog sets up one. Switch or add projects later from the switcher at the top of the rail.</Hint>
      </Advanced>

      <Footer
        onFinishLater={onFinishLater}
        onBack={onBack}
        backDisabled={first}
        next={next}
        busy={busy}
        nextDisabled={!ticked}
        nextWhy="Tick the confirmation first."
      />
    </section>
  );
}

// ── ② Model ────────────────────────────────────────────────────────────────────────────

function ModelStep({
  projectId,
  offline,
  credentials,
  where,
  onDone,
  onBack,
  onFinishLater,
}: {
  projectId: string;
  offline: boolean;
  credentials: Credential[];
  where: Where;
  onDone: () => void;
  onBack: () => void;
  onFinishLater: () => void;
}) {
  const qc = useQueryClient();
  const confirm = useConfirmSetup();
  const serving = servingChat(credentials, projectId);
  const posture = useInstancePosture();
  const [choice, setChoice] = React.useState<ModelChoice>(
    offline ? "offline" : serving?.kind === "ollama" ? "ollama" : serving?.kind === "anthropic" ? "claude" : "offline",
  );
  const [ollamaUrl, setOllamaUrl] = React.useState(where === "this_computer" ? "http://host.docker.internal:11434" : "");
  const [claudeKey, setClaudeKey] = React.useState("");
  const [result, setResult] = React.useState<{ status: SetupStatus; text: string } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  function pick(v: string) {
    setChoice(v as ModelChoice);
    setResult(null);
    setError("");
  }

  /** Save the credential, point this project at it, and report what the server answered. */
  async function save(kind: "ollama" | "anthropic", body: { base_url?: string; api_key?: string }) {
    setBusy(true);
    setError("");
    setResult({ status: "pending", text: kind === "ollama" ? "Probing…" : "Testing…" });
    try {
      const created = await api.createCredential(projectId, {
        kind,
        label: kind === "ollama" ? "Ollama (quick start)" : "Claude (quick start)",
        ...body,
      });
      await api.setProjectCredential(projectId, { credential_id: created.id });
      await qc.invalidateQueries({ queryKey: ["credentials", projectId] });
      if (created.state === "valid") {
        setResult({ status: "done", text: "Answered. Saved and set for this project." });
      } else if (kind === "anthropic") {
        setResult({
          status: "unknown",
          text: "Unknown — saved, but Graphban has no test call for Anthropic keys yet, so the key is unchecked.",
        });
      } else {
        setResult({
          status: "unknown",
          text: "Unknown — the server could not reach it yet. Saved; Graphban keeps retrying.",
        });
      }
    } catch (e) {
      setResult(null);
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function next() {
    setError("");
    if (choice === "offline" && !offline) {
      try {
        // A recorded decision, not an unset default.
        await confirm.mutateAsync({ projectId, body: { decision: "offline" } });
      } catch (e) {
        setError(`Could not record "stay offline": ${errText(e)}`);
        return;
      }
    }
    onDone();
  }

  return (
    <section aria-labelledby="qs-model-h">
      <h2 id="qs-model-h" className="text-[16px] font-semibold">
        Pick a model
      </h2>
      <fieldset className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <legend className="sr-only">Model</legend>
        <Choice name="qs-model" value="offline" checked={choice === "offline"} onChange={pick} title="Offline" sub="Stub">
          <span className="mt-1 block text-[11.5px] text-faint">Runs as is. Chat is canned and nothing gets graded.</span>
        </Choice>
        <Choice name="qs-model" value="ollama" checked={choice === "ollama"} onChange={pick} title="Local" sub="Ollama">
          <span className="mt-1 block text-[11.5px] text-faint">A model server on your network.</span>
        </Choice>
        <Choice name="qs-model" value="claude" checked={choice === "claude"} onChange={pick} title="Cloud" sub="Claude">
          <span className="mt-1 block text-[11.5px] text-faint">Your Anthropic API key.</span>
        </Choice>
      </fieldset>

      {choice === "ollama" && (
        <div className="mt-3">
          <label htmlFor="qs-ollama-url" className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Ollama URL
          </label>
          <div className="flex gap-2">
            <Input
              id="qs-ollama-url"
              value={ollamaUrl}
              onChange={(e) => {
                setOllamaUrl(e.target.value);
                setResult(null);
              }}
              placeholder={where === "dedicated_server" ? "Where does Ollama run? e.g. http://gpu-box:11434" : "http://localhost:11434"}
            />
            <Button
              variant="outline"
              type="button"
              disabled={!ollamaUrl.trim() || busy || !projectId}
              onClick={() => save("ollama", { base_url: normalizeAddress(ollamaUrl) })}
            >
              Probe
            </Button>
          </div>
          {result && <StatusLine status={result.status} text={result.text} />}
        </div>
      )}

      {choice === "claude" && (
        <div className="mt-3">
          <label htmlFor="qs-claude-key" className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Anthropic API key
          </label>
          <div className="flex gap-2">
            <Input
              id="qs-claude-key"
              type="password"
              autoComplete="off"
              value={claudeKey}
              onChange={(e) => {
                setClaudeKey(e.target.value);
                setResult(null);
              }}
            />
            <Button
              variant="outline"
              type="button"
              disabled={!claudeKey.trim() || busy || !projectId}
              onClick={() => save("anthropic", { api_key: claudeKey.trim() })}
            >
              Test call
            </Button>
          </div>
          {/* G7 belongs beside the paste box. Shown only when the instance posture (GRPH-1365)
              reports the key unset; an unreadable posture (non-admin 403, offline) says so plainly. */}
          {posture.isSuccess && !posture.data.secret_encryption_key_set && (
            <p id="qs-key-warn" className="mt-1.5 text-[11.5px] text-st-review">
              SECRET_ENCRYPTION_KEY is unset on this box, so provider keys are stored unencrypted. Set it first.
            </p>
          )}
          {!posture.isSuccess && !posture.isPending && (
            <p id="qs-key-warn" className="mt-1.5 text-[11.5px] text-faint">
              Could not check whether SECRET_ENCRYPTION_KEY is set on this box (admins only).
            </p>
          )}
          {result ? (
            <StatusLine status={result.status} text={result.text} />
          ) : (
            <StatusLine status="pending" text={claudeKey.trim() ? "Pending — not tested." : "Pending — no key yet."} />
          )}
        </div>
      )}
      <ErrorLine text={error} />

      <Advanced>
        <Hint lead="Embeddings are fixed at deploy time.">
          The embedding model comes from server config, not this step. Changing it later means re-embedding memory.
        </Hint>
        <Hint lead="System One decider for memory judging.">
          Memory candidates are judged by a separate fast decider. Set it under Settings → AI providers.
        </Hint>
      </Advanced>

      <Footer onFinishLater={onFinishLater} onBack={onBack} next={next} busy={confirm.isPending || busy} />
    </section>
  );
}

// ── ④ First job ────────────────────────────────────────────────────────────────────────

const JOB_TAIL =
  "describe the code, load the durable facts from AGENTS.md/CLAUDE.md/docs as memory candidates, and propose a first handful of items.";

/** The agent counts as "working" only if it made a call within this many ms (the step ③ key's last_call). */
export const AGENT_ACTIVE_WINDOW_MS = 5 * 60 * 1000;

export function agentIsWorking(connect: { last_call?: string } | undefined, now: number = Date.now()): boolean {
  const t = connect?.last_call ? Date.parse(connect.last_call) : NaN;
  return Number.isFinite(t) && now - t <= AGENT_ACTIVE_WINDOW_MS;
}

export function firstJobPrompt(hosted: boolean): string {
  return hosted
    ? `First ask setup_project for the code-graph dry-run estimate and show it to me. Then run it for this repository and work through it: ${JOB_TAIL}`
    : `Run setup_project for this repository and work through it: ${JOB_TAIL}`;
}

function FirstJobStep({
  hosted,
  agentLabel,
  mark,
  setupSteps,
  isLast,
  tourAvailable,
  onDone,
  onBack,
  onFinishLater,
}: {
  hosted: boolean;
  agentLabel: string;
  mark: StepMark;
  setupSteps: { step: number; status: SetupStatus; detail: string; name: string; how: string; reason?: string; last_call?: string }[];
  isLast: boolean;
  tourAvailable: boolean;
  onDone: () => void;
  onBack: () => void;
  onFinishLater: () => void;
}) {
  const prompt = firstJobPrompt(hosted);
  const counters: [string, number, string][] = [
    ["Code nodes", 2, ""],
    ["Memories", 3, "candidates — you publish them"],
    ["Items", 4, ""],
  ];
  const values = counters.map(([, n]) => countFrom(setupSteps.find((s) => s.step === n)));
  const anyCounted = values.some((v) => (v ?? 0) > 0);
  const line =
    mark.status === "done"
      ? { status: "done" as const, text: "First job finished." }
      : mark.status === "unknown"
        ? { status: "unknown" as const, text: `Unknown — ${mark.reason}` }
        : anyCounted && agentIsWorking(setupSteps.find((s) => s.step === 5))
          ? { status: "pending" as const, text: "Your agent is working. Counts are live." }
          : anyCounted
            ? { status: "pending" as const, text: "Pending — no agent call in the last 5 minutes. Counts so far are shown." }
            : { status: "pending" as const, text: "Pending — nothing counted yet." };

  return (
    <section aria-labelledby="qs-job-h">
      <h2 id="qs-job-h" className="text-[16px] font-semibold">
        Give it a first job
      </h2>
      <p className="mb-1.5 mt-3 font-mono text-[10px] uppercase tracking-wide text-faint">Paste into {agentLabel}</p>
      <CopyLine text={prompt} label="Copy the first-job prompt" />
      <StatusLine status={line.status} text={line.text} />
      <p className="mt-2 text-[12px] text-muted" data-testid="qs-codegraph-note">
        The key from “Create a key for Claude Code” does not list <code>describe_code</code>, so your agent will not find
        it and the code graph stays at 0 on this route. To fill it, run <code>gban setup</code> on the machine with the checkout, or use a key that can describe.
      </p>
      <dl className="mt-3 grid grid-cols-3 gap-2">
        {counters.map(([label, , sub], i) => (
          <div key={label} className="rounded-[10px] border border-line bg-surface-2 p-2.5">
            <dt className="font-mono text-[10px] uppercase tracking-wide text-faint">{label}</dt>
            <dd className="text-title font-semibold tabular-nums text-fg">
              {values[i] === null ? <span title="could not be counted">?</span> : values[i]}
            </dd>
            {sub && <dd className="text-small text-faint">{sub}</dd>}
          </div>
        ))}
      </dl>
      <p className="mt-3 text-[12px] text-faint">
        {tourAvailable
          ? "You don't have to wait. Take the tour now; the counts keep updating."
          : "You don't have to wait. Finish whenever you like; nothing here is required."}
      </p>
      {!hosted && (
        <p className="mt-2 text-[12px] text-muted">
          Reviews need a second agent. Add a reviewer later via delegation, or allow labelled self-review in Settings → Project.
        </p>
      )}
      <Advanced>
        <Hint lead="Local-first code graph (no hosted quota).">
          The code-graph pass runs on the machine that has the checkout. Local indexing doesn't count against a hosted quota.
        </Hint>
        <Hint lead="Memory trust level (review / auto / trusted).">
          Sets how memory candidates get published: review (you publish each one), auto, or trusted. Change it under Settings → Project.
        </Hint>
      </Advanced>
      <Footer onFinishLater={onFinishLater} onBack={onBack} next={onDone} nextLabel={isLast ? "Finish" : "Next →"} />
    </section>
  );
}

// ── Finish ─────────────────────────────────────────────────────────────────────────────

export function FinishScreen({
  steps,
  marks,
  route,
  onFinish,
  onTakeTour,
  invitee = false,
}: {
  steps: QsStep[];
  marks: Record<QsStep, StepMark>;
  route: Route;
  onFinish: () => Promise<void>;
  onTakeTour?: () => void;
  invitee?: boolean;
}) {
  const [promptsOpen, setPromptsOpen] = React.useState(false);
  const open = steps.filter((s) => marks[s].status !== "done");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  async function finish(then?: () => void) {
    setBusy(true);
    setError("");
    try {
      await onFinish();
      then?.();
    } catch (e) {
      setError(`Could not record that setup is finished: ${errText(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="qs-finish-h">
      <h2 id="qs-finish-h" className="text-[16px] font-semibold">
        You're set up
      </h2>
      {open.length ? (
        <>
          <ul className="mt-3 space-y-1" aria-label="Steps still open">
            {open.map((s) => {
              const m = marks[s];
              const g = GLYPH[m.status];
              return (
                <li key={s} className="text-[13px] text-muted">
                  <span className={cn("mr-1.5 font-mono", g.cls)} aria-hidden>
                    {g.g}
                  </span>
                  {s === "connect" && m.status === "pending"
                    ? `Agent connection unverified — ${ROUTE_LABEL[route]}`
                    : `${STEP_META[s].label} — ${m.status === "unknown" ? m.reason : "pending"}`}
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-[12px] text-faint">
            These stay open until the server can show them. Finishing doesn't mark them done.
          </p>
        </>
      ) : (
        <p className="mt-3 text-[13px] text-muted">
          <span className="mr-1.5 font-mono text-st-done" aria-hidden>
            ✓
          </span>
          Every step has evidence behind it.
        </p>
      )}
      {marks.connect?.status === "done" && marks.connect.evidence && (
        <p className="mt-2 text-[12px] text-muted">Agent connection: {marks.connect.evidence}</p>
      )}
      {invitee && <InviteeLinkCard />}
      {invitee && promptsOpen && (
        <div className="mt-3">
          <StartupPromptRows />
        </div>
      )}
      <ErrorLine text={error} />
      <div className="mt-5 flex items-center justify-end gap-2 border-t border-line pt-4">
        {invitee ? (
          <>
            <Button variant="ghost" size="sm" type="button" disabled={busy} onClick={() => finish()}>
              Done
            </Button>
            <Button
              variant="outline"
              size="sm"
              type="button"
              aria-expanded={promptsOpen}
              onClick={() => setPromptsOpen((v) => !v)}
            >
              Start-up prompts
            </Button>
            <Button variant="outline" size="sm" type="button" disabled={busy || !onTakeTour} onClick={() => finish(onTakeTour)}>
              Take the tour
            </Button>
          </>
        ) : onTakeTour ? (
          <>
            <Button variant="outline" size="sm" type="button" disabled={busy} onClick={() => finish()}>
              Skip for now
            </Button>
            <Button size="sm" type="button" disabled={busy} onClick={() => finish(onTakeTour)}>
              Take the 2-minute tour
            </Button>
          </>
        ) : (
          <Button size="sm" type="button" disabled={busy} loading={busy} onClick={() => finish()}>
            Done
          </Button>
        )}
      </div>
    </section>
  );
}
