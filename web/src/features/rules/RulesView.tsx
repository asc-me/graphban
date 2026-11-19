/**
 * Rules (GRPH-P61 §5, GRPH-1162): the project's delivery process as one read-only map.
 *
 * Seven fixed stages with their rules hung underneath, each rule drawn at what actually holds
 * it. Everything on screen comes from `GET /api/rules/flow`; stage state and not-used are the
 * server's (`services/rules.py:flow`). Look: `docs/design/Rules.dc.html`, checklist:
 * `docs/design/rules-view-design.md`. Editing is UI 2/3; refusals and Recommended are UI 3/3.
 */
import * as React from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useCanvasListbox, type CanvasListNode } from "@/lib/graph/useGraphKeyboard";
import { useGraphViewport, zoomAbout } from "@/lib/graph/useGraphViewport";
import { useGitops, useRulePack, useRulePackAvailable, useRulesFlow, useUnmappedRefusals } from "@/lib/queries";
import { projectPath, settingsPath, tagFromPath } from "@/lib/routes";
import type { RuleFlowRule, RuleStage, RuleSuggestion, RulesFlow } from "@/lib/types";

import {
  CHIP_H,
  CHIP_STEP,
  CHIP_W,
  CHIP_Y,
  CHIPS_SHOWN,
  LEVEL_ORDER,
  LEVELS,
  OUTLINE_ZOOM,
  PACK_H,
  PACK_W,
  STAGE_H,
  STAGE_W,
  STAGE_Y,
  legendCounts,
  mapLayout,
  packOf,
  ruleLevel,
  stageLevel,
  stageRules,
  stageX,
  tally,
  tallyText,
  weakestFirst,
  type Level,
  type MapLayout,
} from "./levels";
import { RecommendedSheet, UnmappedSheet } from "./RefusalPanels";
import { AdminGitopsLink, useOrgName } from "./RuleEditors";
import { RuleLevelIcon } from "./RuleLevelIcon";
import { QuickConfigure, QuickConfigureBar, type QuickScreen } from "./QuickConfigure";
import { ChangedRulesContext, SetupWizard } from "./SetupWizard";
import { DockEditor } from "./DockEditor";

const PACK_KEY = "pack";
const stageKey = (id: string) => `s:${id}`;
const optionId = (key: string) => `rules-opt-${key.replace(/[^a-z0-9]/gi, "-")}`;
/** Frame 6e: below this the map becomes a vertical stage list and the inspector a bottom sheet. */
const NARROW_PX = 900;

const MONO = "font-mono";

function useNarrow(): boolean {
  const read = () => typeof window !== "undefined" && window.innerWidth < NARROW_PX;
  const [narrow, setNarrow] = React.useState(read);
  React.useEffect(() => {
    const on = () => setNarrow(read());
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return narrow;
}

function stageAria(stage: RuleStage): string {
  const level = stageLevel(stage);
  const rules = stageRules(stage);
  const drawn =
    level === null ? "no rules registered" : level === "not_used" ? "not used" : `drawn as ${level}`;
  const counts = tally(rules).map(tallyText).join(", ");
  return `Stage ${stage.title}, ${drawn}.${counts ? ` ${counts}` : ""}`;
}

export function RulesView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const q = useRulesFlow(activeId);
  const unmapped = useUnmappedRefusals(activeId);
  const unmappedCount = unmapped.data?.groups.reduce((n, g) => n + g.count, 0);
  const flow = q.data;
  const narrow = useNarrow();
  const [params] = useSearchParams();
  const linked = params.get("stage");
  const [sel, setSel] = React.useState<string | null>(linked ? stageKey(linked) : null);
  // A dirty draft in the dock holds a stage switch (or a close) behind an in-page confirm.
  const dirtyRef = React.useRef(false);
  const onDirty = React.useCallback((d: boolean) => {
    dirtyRef.current = d;
  }, []);
  const [pendingSel, setPendingSel] = React.useState<{ to: string | null; after?: () => void } | null>(null);
  // `sel` is read through a ref so selectStage keeps one identity: effects that call it (the
  // ?rule= focus) must re-run only when their own inputs change, not on every selection.
  const selRef = React.useRef(sel);
  selRef.current = sel;
  const selectStage = React.useCallback((to: string | null, after?: () => void) => {
    if (to !== selRef.current && dirtyRef.current) setPendingSel({ to, after });
    else {
      setSel(to);
      after?.();
    }
  }, []);
  const { pathname } = useLocation();
  // Following a refusal (frames 4a1–4b2): `?rule=` focuses one rule, `?from=` is the event to
  // go back to, `?pulse=unmapped` is "see all rules" for a refusal that names no rule.
  const ruleParam = params.get("rule");
  const fromEvent = params.get("from");
  const [focusRule, setFocusRule] = React.useState<string | null>(ruleParam);
  // A second link to this route while mounted must refocus (GRPH-1199).
  React.useEffect(() => {
    setFocusRule(ruleParam);
  }, [ruleParam]);
  const bodyRef = React.useRef<HTMLDivElement | null>(null);
  const [mapPct, setMapPct] = React.useState(readDockPct);
  const setPct = React.useCallback((pct: number) => {
    setMapPct(pct);
    try {
      window.localStorage.setItem(DOCK_KEY, String(pct));
    } catch {
      /* storage refused: the height holds for this visit only */
    }
  }, []);
  const [wizard, setWizard] = React.useState(false);
  const [quick, setQuick] = React.useState<QuickScreen | null>(null);
  const [changed, setChanged] = React.useState<ReadonlySet<string>>(new Set());
  const [prefill, setPrefill] = React.useState<{ rule: string; value: string } | null>(null);
  const [sheet, setSheet] = React.useState<"unmapped" | "recommended" | null>(null);
  const unmappedBtn = React.useRef<HTMLButtonElement | null>(null);
  const recommendedBtn = React.useRef<HTMLButtonElement | null>(null);
  const [pulse, setPulse] = React.useState(params.get("pulse") === "unmapped");
  const focusedRule = flow && focusRule ? flow.stages.flatMap((s) => s.rules).find((r) => r.id === focusRule) ?? null : null;
  const focusedStage = focusedRule?.stage ?? null;
  React.useEffect(() => {
    if (focusedStage) selectStage(stageKey(focusedStage));
  }, [focusedStage, selectStage]);

  function openValueSuggestion(s: RuleSuggestion) {
    if (s.rule_id && s.value !== null) setPrefill({ rule: s.rule_id, value: s.value });
    setFocusRule(s.rule_id);
    if (!s.rule_id) selectStage(stageKey(s.stage), () => setSheet(null));
    else setSheet(null);
  }

  // A ?stage= that names no stage on this payload selects nothing, rather than an empty inspector.
  React.useEffect(() => {
    if (!flow || !sel || sel === PACK_KEY) return;
    if (!flow.stages.some((s) => stageKey(s.id) === sel)) selectStage(null);
  }, [flow, sel, selectStage]);

  // A null model is also what linked_unset and linked_unreachable look like; only a local
  // project with no model is new.
  const { data: gitops } = useGitops(activeId);
  const controlState = flow?.control.state ?? null;
  const fresh = !!flow && flow.model === null && controlState === "local";
  const orgName = useOrgName(gitops);
  const wizardDirty = React.useRef(false);
  const setWizardDirty = React.useCallback((d: boolean) => { wizardDirty.current = d; }, []);
  const openQuick = (s: QuickScreen) => {
    if (wizard && wizardDirty.current && !window.confirm("Leave setup? Your answers are not saved and will be discarded.")) return;
    selectStage(null, () => { setWizard(false); setQuick(s); });
  };

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="rules-view">
      <PlaceHeader
        viewName={`Rules ${scope}`}
        purpose={`How work moves through ${scope}, and what holds each step.`}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              ref={unmappedBtn}
              aria-haspopup="dialog"
              aria-expanded={sheet === "unmapped"}
              onClick={() => selectStage(null, () => { setPulse(false); setSheet("unmapped"); })}
              onAnimationEnd={() => setPulse(false)}
              data-testid="rules-open-unmapped"
              data-pulse={pulse ? "true" : "false"}
              className={cn(
                "h-7 rounded-[8px] border border-control px-2.5 text-small text-fg-2 hover:text-fg",
                pulse && "animate-[pulse_1s_ease-in-out_1] border-accent text-accent",
              )}
            >
              Unmapped refusals{unmappedCount !== undefined && <span data-testid="rules-unmapped-count"> · {unmappedCount}</span>}
            </button>
            <button
              type="button"
              ref={recommendedBtn}
              aria-haspopup="dialog"
              aria-expanded={sheet === "recommended"}
              onClick={() => selectStage(null, () => setSheet("recommended"))}
              data-testid="rules-open-recommended"
              className="h-7 rounded-[8px] border border-control px-2.5 text-small text-fg-2 hover:text-fg"
            >
              Recommended
            </button>
            <button
              type="button"
              onClick={() => selectStage(null, () => { setQuick(null); setWizard(true); })}
              data-testid="rules-open-wizard"
              className="h-7 rounded-[8px] border border-control px-2.5 text-small text-fg-2 hover:text-fg"
            >
              Set up rules
            </button>
            <RulePackPills />
            <ModelPill flow={flow} loading={q.isLoading} />
          </div>
        }
      />
      {fromEvent && (
        <nav aria-label="Breadcrumb" data-testid="rules-breadcrumb" className="mx-5 mt-3 flex items-center gap-2 text-small text-muted">
          <Link
            to={`${tagFromPath(pathname) ? projectPath(tagFromPath(pathname)!, "activity") : "/activity"}?event=${encodeURIComponent(fromEvent)}`}
            className="text-accent hover:underline"
          >
            ← Refusal (event {fromEvent})
          </Link>
          <span aria-hidden>/</span>
          <span className={MONO}>{focusRule ?? "all rules"}</span>
        </nav>
      )}
      {focusRule && flow && !focusedRule && (
        <div role="status" data-testid="rules-focus-missing" className="mx-5 mt-3 rounded-[8px] px-3 py-2 text-body text-fg-2" style={{ border: "1.5px dashed #ff6b6b" }}>
          The refusal names {focusRule}, which is not in this project's registry on this server. The rule exists in the refusal's record; the map can't show it.
        </div>
      )}
      {/* With the dock open these collapse to one line each, so the presets divide the space under the header (GRPH-1725). */}
      <ControlBanner state={controlState} gitops={gitops} compact={!!sel} />
      <QuickConfigureBar onOpen={openQuick} compact={!!sel} />
      {fresh && sel && (
        <div
          role="status"
          data-testid="rules-fresh-banner"
          data-collapsed="true"
          className="mx-5 mt-1 flex flex-none items-center gap-2 overflow-hidden whitespace-nowrap text-small"
        >
          <span className={cn(MONO, "text-meta font-semibold tracking-[0.07em] text-[#ff6b6b]")}>NEW PROJECT</span>
          <span className="truncate text-fg-2">Your delivery contract is unmeasured.</span>
          <button type="button" onClick={() => openQuick({ screen: "picker" })} data-testid="rules-fresh-quick" className="flex-none text-accent underline">
            Quick configure
          </button>
        </div>
      )}
      {fresh && !sel && (
        <div
          role="status"
          data-testid="rules-fresh-banner"
          className="mx-5 mt-3 flex flex-wrap items-center gap-3 rounded-[12px] bg-surface-2 px-3 py-2.5"
          style={{ border: "1px dotted #ff6b6b" }}
        >
          <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.07em] text-[#ff6b6b]")}>NEW PROJECT</span>
          <span className="flex-1 text-body text-fg-2">
            Your delivery contract is unmeasured. The server's own gates still apply.
          </span>
          <button
            type="button"
            onClick={() => openQuick({ screen: "picker" })}
            data-testid="rules-fresh-quick"
            className="h-[30px] rounded-[8px] bg-accent px-3 text-body font-semibold text-[#0a0c0e]"
          >
            Quick configure
          </button>
          <button
            type="button"
            onClick={() => selectStage(null, () => setWizard(true))}
            data-testid="rules-fresh-setup"
            className="h-[30px] rounded-[8px] bg-accent px-3 text-body font-semibold text-[#0a0c0e]"
          >
            Set up rules
          </button>
          <button
            type="button"
            onClick={() => selectStage(stageKey("merge"))}
            className="h-[30px] rounded-[8px] border border-control px-3 text-body text-fg-2"
          >
            Set it up in Merge
          </button>
        </div>
      )}
      <ChangedRulesContext.Provider value={changed}>
      <div ref={bodyRef} className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        {wizard && flow && (
          <SetupWizard
            flow={flow}
            narrow={narrow}
            onExit={() => setWizard(false)}
            onDirtyChange={setWizardDirty}
            onApplied={(ids) => { setChanged(new Set(ids)); setWizard(false); setSel(null); }}
          />
        )}
        {quick && flow && (
          <QuickConfigure
            flow={flow}
            narrow={narrow}
            orgName={orgName}
            start={quick}
            onExit={() => setQuick(null)}
            onApplied={(ids) => { setChanged(new Set(ids)); setQuick(null); setSel(null); }}
            onSetup={() => { setQuick(null); setWizard(true); }}
          />
        )}
        {q.isError && !flow ? (
          <LoadError detail={errorDetail(q.error, "request failed")} onRetry={() => void q.refetch()} />
        ) : !flow ? (
          <LoadingMap />
        ) : (
          <div className="relative min-h-0" style={{ height: sel ? `${mapPct}%` : "100%" }} data-testid="rules-map-area" data-map-pct={sel ? mapPct : 100}>
            {narrow ? (
              <NarrowList flow={flow} sel={sel} onSelect={selectStage} />
            ) : (
              <RulesCanvas flow={flow} sel={sel} onSelect={selectStage} fitKey={sel ? mapPct : null} />
            )}
          </div>
        )}
        {flow && sel && (
          <StageDock
            flow={flow}
            sel={sel}
            focusRule={focusRule}
            prefill={prefill}
            mapPct={mapPct}
            onMapPct={setPct}
            containerRef={bodyRef}
            onClose={() => selectStage(null)}
            onDirty={onDirty}
            leave={
              pendingSel && {
                closing: pendingSel.to === null,
                onLeave: () => {
                  dirtyRef.current = false;
                  const { to, after } = pendingSel;
                  setSel(to);
                  setPendingSel(null);
                  after?.();
                },
                onStay: () => setPendingSel(null),
              }
            }
          />
        )}
        {sheet === "unmapped" && <UnmappedSheet returnTo={unmappedBtn} onClose={() => setSheet(null)} />}
        {sheet === "recommended" && <RecommendedSheet returnTo={recommendedBtn} onClose={() => setSheet(null)} onOpenValue={openValueSuggestion} />}
      </div>
      </ChangedRulesContext.Provider>
    </div>
  );
}

/**
 * Pills near the header: a newer pack is on the feed, or the installed pack is not in force.
 * Current, loading and unknown (feed down, or a hosted non-admin's 403) show nothing: an
 * unknown state must not read as "current", and must not shout either.
 */
function RulePackPills() {
  const avail = useRulePackAvailable();
  const pack = useRulePack();
  const notLoaded = pack.data?.installed === true && !pack.data.loaded;
  const available = avail.data?.state === "available";
  if (!notLoaded && !available) return null;
  const to = settingsPath("deployment/updates");
  const cls = "inline-flex h-7 items-center rounded-full border px-2.5 text-small";
  return (
    <>
      {notLoaded && (
        <Link to={to} data-testid="rules-pack-not-loaded" className={cn(cls, "border-danger text-danger")}>
          Rule pack not loaded
        </Link>
      )}
      {available && (
        <Link to={to} data-testid="rules-pack-update" className={cn(cls, "border-accent text-accent")}>
          Update available{avail.data?.version ? ` · ${avail.data.version}` : ""}
        </Link>
      )}
    </>
  );
}

function ModelPill({ flow, loading }: { flow: RulesFlow | undefined; loading: boolean }) {
  const model = flow?.model ?? null;
  const known = !!model;
  return (
    <span
      title="Gitops model"
      data-testid="rules-model"
      className={cn(MONO, "inline-flex items-center gap-2 rounded-full px-2.5 py-0.5 text-small")}
      style={{ border: `1px ${known || loading ? "solid #2a333a" : "dotted #ff6b6b"}` }}
    >
      <span className="text-meta tracking-[0.07em] text-muted-2">GITOPS</span>
      <span className="font-semibold" style={{ color: known ? "#e6e9ec" : loading ? "#8b949e" : "#ff6b6b" }}>
        {loading ? "loading…" : model ?? "unmeasured"}
      </span>
    </span>
  );
}

function LevelWord({ level, size = 12, className }: { level: Level | null; size?: number; className?: string }) {
  const meta = level ? LEVELS[level] : null;
  return (
    <span
      className={cn(MONO, "inline-flex items-center gap-1.5 font-semibold tracking-[0.05em]", className)}
      style={{ color: meta?.color ?? "#dfe3e7" }}
    >
      <RuleLevelIcon level={level} size={size} />
      {meta?.word ?? "NO RULES"}
    </span>
  );
}

function StagePill({ level, big = false }: { level: Level | null; big?: boolean }) {
  const meta = level ? LEVELS[level] : null;
  return (
    <span
      data-testid="rules-stage-pill"
      data-level={level ?? "none"}
      className={cn(
        MONO,
        "inline-flex items-center gap-1.5 self-start whitespace-nowrap rounded-full px-2 py-0.5 font-semibold tracking-[0.06em]",
        big ? "text-lead" : "text-[10px]",
      )}
      style={{
        border: meta?.pillBorder ?? "1px dashed #626c75",
        background: meta?.pillBg ?? "transparent",
        color: meta?.color ?? "#dfe3e7",
      }}
    >
      <RuleLevelIcon level={level} size={big ? 16 : 12} />
      {meta?.word ?? "NO RULES"}
    </span>
  );
}

function TallyLine({ rules }: { rules: RuleFlowRule[] }) {
  const t = tally(rules);
  if (!t.length) return <span className={cn(MONO, "text-[10.5px] text-muted")}>nothing registered yet</span>;
  return (
    <span className={cn(MONO, "flex flex-wrap gap-x-1.5 text-[10.5px]")}>
      {t.map((x, i) => (
        <span key={x.level} style={{ color: x.level === "enforced" ? "#8b949e" : LEVELS[x.level].color }}>
          {i ? "· " : ""}
          {tallyText(x)}
        </span>
      ))}
    </span>
  );
}

function TallyBar({ rules }: { rules: RuleFlowRule[] }) {
  const t = tally(rules);
  if (!t.length) return null;
  return (
    <span className="flex h-1 gap-0.5" aria-hidden>
      {t.map((x) => (
        <span key={x.level} className="rounded-sm" style={{ flex: x.n, background: LEVELS[x.level].bar }} />
      ))}
    </span>
  );
}

// ── Canvas ─────────────────────────────────────────────────────────────────────

function RulesCanvas({
  flow,
  sel,
  onSelect,
  fitKey = null,
}: {
  flow: RulesFlow;
  sel: string | null;
  onSelect: (key: string | null) => void;
  /** The dock's height preset while the dock is open: the map is then a strip, refitted on every change. */
  fitKey?: number | null;
}) {
  const layout = React.useMemo(() => mapLayout(flow), [flow]);
  const { width: W, height: H } = layout;
  // The viewBox is the canvas's own pixel size, so `k` is the real screen scale: 1:1 is
  // actual size and the outline threshold means what the mock means by it.
  const boxRef = React.useRef<HTMLDivElement | null>(null);
  const [size, setSize] = React.useState({ w: 1200, h: 700 });
  React.useEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => {
      const r = entry.contentRect;
      if (r.width && r.height) setSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const view = useGraphViewport(size.w, size.h, { panAnywhere: true });
  const outline = view.viewport.k < OUTLINE_ZOOM;
  const pack = packOf(flow);
  const [hover, setHover] = React.useState<string | null>(null);

  // Fit never enlarges past 1:1; the legend and zoom bar keep the bottom 120px. Docked, the map is
  // a strip with no legend or zoom bar (GRPH-1725): every stage card fits the strip's width and
  // height, the row centred, and the fit is recomputed whenever the dock opens or changes height.
  const { setViewport } = view;
  const docked = fitKey !== null;
  const fits = React.useRef(0);
  const fit = React.useCallback(() => {
    fits.current += 1;
    if (docked) {
      const k = Math.max(0.2, Math.min(1, (size.w - 48) / W, (size.h - 16) / STAGE_H));
      setViewport({ k, x: (size.w - W * k) / 2, y: (size.h - STAGE_H * k) / 2 - STAGE_Y * k });
      return;
    }
    const k = Math.max(0.2, Math.min(1, (size.w - 48) / W, (size.h - 140) / H));
    setViewport({ k, x: (size.w - W * k) / 2, y: Math.max(16, (size.h - 120 - H * k) / 2) });
  }, [setViewport, size.w, size.h, W, H, docked]);
  React.useEffect(() => {
    fit();
  }, [fit, fitKey]);

  // The stages and the pack node ride the same listbox hook as the code graph (GRPH-1076):
  // live-region announcements, typeahead by stage name, Enter opens, Escape closes. The order is
  // the flow's own, not degree order.
  const listNodes = React.useMemo<CanvasListNode[]>(
    () => [
      ...flow.stages.map((s) => ({ id: stageKey(s.id), path: s.title, kind: "stage", name: s.title, degree: 0 })),
      ...(pack ? [{ id: PACK_KEY, path: pack.title, kind: "pack", name: pack.title, degree: 0 }] : []),
    ],
    [flow.stages, pack],
  );
  const say = React.useCallback(
    (key: string) => {
      if (key === PACK_KEY) return pack ? `${pack.title}, ${pack.enforcement}` : "";
      const stage = flow.stages.find((s) => stageKey(s.id) === key);
      if (!stage) return "";
      const level = stageLevel(stage);
      const n = stageRules(stage).length;
      const word = level === null ? "no rules registered" : level.replace("_", " ");
      return `${stage.title}, ${word}, ${n} ${n === 1 ? "rule" : "rules"}`;
    },
    [flow.stages, pack],
  );
  const selRef = React.useRef(sel);
  selRef.current = sel;
  const sizeRef = React.useRef(size);
  sizeRef.current = size;
  const follow = (key: string) => {
    // The inspector follows focus once it is open, like the mock.
    if (selRef.current) onSelect(key);
    // Camera follow: slide the map only when the focused node has left the screen.
    const i = flow.stages.findIndex((s) => stageKey(s.id) === key);
    const x = key === PACK_KEY ? layout.pack?.x : i >= 0 ? stageX(i) : undefined;
    if (x === undefined) return;
    const w = key === PACK_KEY ? PACK_W : STAGE_W;
    view.setViewport((v) => {
      const left = x * v.k + v.x;
      const right = (x + w) * v.k + v.x;
      if (left >= 24 && right <= sizeRef.current.w - 24) return v;
      return { ...v, x: sizeRef.current.w / 2 - (x + w / 2) * v.k };
    });
  };
  const kb = useCanvasListbox({
    nodes: listNodes,
    selectedId: sel,
    onSelect: (id) => onSelect(id),
    onClearSelection: () => onSelect(null),
    onFollow: follow,
    idPrefix: "rules",
    keepOrder: true,
    announce: say,
  });
  const focus = kb.activeId;

  const pick = (key: string) => {
    onSelect(sel === key ? null : key);
  };

  const zoomBy = (factor: number) =>
    view.setViewport((v) => zoomAbout(v, factor, size.w / 2, size.h / 2));
  const counts = legendCounts(flow);

  return (
    <div
      ref={boxRef}
      className="absolute inset-0"
      data-testid="rules-canvas"
      data-docked={docked ? "true" : undefined}
      data-zoom={view.viewport.k.toFixed(3)}
      data-fits={fits.current}
    >
      <div
        role="listbox"
        tabIndex={0}
        aria-label="Rules map. Left and right move between stages, Enter opens, Escape closes."
        aria-activedescendant={focus ? optionId(focus) : undefined}
        data-testid="rules-listbox"
        onKeyDown={kb.onKeyDown}
        className="h-full w-full outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
      >
        <svg
          ref={view.svgRef}
          viewBox={`0 0 ${size.w} ${size.h}`}
          preserveAspectRatio="xMidYMid meet"
          className={cn("h-full w-full touch-none select-none", view.panning ? "cursor-grabbing" : "cursor-grab")}
          onWheel={view.svgHandlers.onWheel}
          onPointerDown={view.svgHandlers.onPointerDown}
          onPointerMove={view.svgHandlers.onPointerMove}
          onPointerUp={view.svgHandlers.onPointerUp}
          onPointerCancel={view.svgHandlers.onPointerUp}
          onClickCapture={view.svgHandlers.onClickCapture}
        >
          <defs>
            <marker id="rl-ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto">
              <path d="M0,0 L8,4 L0,8 z" fill="#626c75" />
            </marker>
            <marker id="rl-ahs" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto">
              <path d="M0,0 L8,4 L0,8 z" fill="#c6f24e" />
            </marker>
          </defs>
          <g transform={view.transform} style={{ transition: view.panning ? undefined : "transform 220ms ease" }}>
            <Edges flow={flow} layout={layout} sel={sel} hover={hover} outline={outline} />
            {flow.stages.map((s, i) => (
              <StageColumn
                key={s.id}
                stage={s}
                index={i}
                outline={outline}
                selected={sel === stageKey(s.id)}
                focused={focus === stageKey(s.id)}
                onPick={() => pick(stageKey(s.id))}
                onHover={(on) => setHover(on ? s.id : null)}
              />
            ))}
            {pack && layout.pack && (
              <foreignObject x={layout.pack.x} y={layout.pack.y} width={PACK_W} height={PACK_H}>
                <PackNode
                  pack={pack}
                  selected={sel === PACK_KEY}
                  focused={focus === PACK_KEY}
                  onPick={() => pick(PACK_KEY)}
                />
              </foreignObject>
            )}
          </g>
        </svg>
      </div>

      <div aria-live="polite" aria-atomic="true" data-testid="rules-live" className="sr-only">
        {kb.live}
      </div>
      {!docked && (
        <>
          <Legend counts={counts} />
          <ZoomControls
            pct={Math.round(view.viewport.k * 100)}
            outline={outline}
            onIn={() => zoomBy(1.2)}
            onOut={() => zoomBy(1 / 1.2)}
            onFit={fit}
            onOneToOne={() => view.setViewport((v) => zoomAbout(v, 1 / v.k, size.w / 2, size.h / 2))}
          />
        </>
      )}
    </div>
  );
}

function Edges({
  flow,
  layout,
  sel,
  hover,
  outline,
}: {
  flow: RulesFlow;
  layout: MapLayout;
  sel: string | null;
  hover: string | null;
  outline: boolean;
}) {
  const index = new Map(flow.stages.map((s, i) => [s.id, i]));
  const midY = STAGE_Y + STAGE_H / 2;
  const out: React.ReactNode[] = [];
  flow.edges.forEach(([a, b]) => {
    const i = index.get(a);
    const j = index.get(b);
    if (i === undefined || j === undefined) return;
    const on = sel === stageKey(a) || sel === stageKey(b);
    const lit = !on && (hover === a || hover === b);
    const hs = flow.stages[hover === a ? i : j];
    const hl = lit ? stageLevel(hs) : null;
    out.push(
      <path
        key={`${a}-${b}`}
        data-testid="rules-edge"
        d={`M${stageX(i) + STAGE_W} ${midY} L${stageX(j) - 2} ${midY}`}
        fill="none"
        stroke={on ? "#c6f24e" : hl ? LEVELS[hl].color : "#626c75"}
        strokeWidth={on || lit ? 2 : 1.5}
        markerEnd={on ? "url(#rl-ahs)" : "url(#rl-ah)"}
      />,
    );
  });
  if (!outline) {
    flow.stages.forEach((s, i) => {
      const n = Math.min(CHIPS_SHOWN, stageRules(s).length);
      if (!n) return;
      const x = stageX(i) + 8;
      const level = stageLevel(s);
      const on = sel === stageKey(s.id) || hover === s.id;
      const c = sel === stageKey(s.id) ? "#c6f24e" : on && level ? LEVELS[level].color : "#2a333a";
      const last = CHIP_Y + (n - 1) * CHIP_STEP + CHIP_H / 2;
      out.push(
        <path key={`spine-${s.id}`} d={`M${x} ${STAGE_Y + STAGE_H} V${last}`} fill="none" stroke={c}
          strokeWidth={on ? 2 : 1.5} strokeDasharray={level === "not_used" ? "3 4" : undefined} />,
      );
      for (let j = 0; j < n; j++) {
        const y = CHIP_Y + j * CHIP_STEP + CHIP_H / 2;
        out.push(<path key={`tick-${s.id}-${j}`} d={`M${x} ${y} H${x + 8}`} fill="none" stroke={c} strokeWidth={on ? 2 : 1.5} />);
      }
    });
    if (layout.pack) {
      const s = flow.stages[layout.pack.stageIndex];
      const x = stageX(layout.pack.stageIndex) + STAGE_W / 2;
      const top = s ? Math.max(STAGE_Y + STAGE_H, CHIP_Y + Math.min(CHIPS_SHOWN, stageRules(s).length) * CHIP_STEP + 34) : 0;
      out.push(
        <path key="pack-link" d={`M${x} ${layout.pack.y} V${top}`} fill="none"
          stroke={sel === PACK_KEY ? "#c6f24e" : "#626c75"} strokeWidth={1} strokeDasharray="2 4" />,
      );
    }
  }
  return <g aria-hidden>{out}</g>;
}

function ring(selected: boolean, focused: boolean): string | undefined {
  return selected || focused ? "0 0 0 2px #0a0c0e, 0 0 0 4px #c6f24e" : undefined;
}

function StageColumn({
  stage,
  index,
  outline,
  selected,
  focused,
  onPick,
  onHover,
}: {
  stage: RuleStage;
  index: number;
  outline: boolean;
  selected: boolean;
  focused: boolean;
  onPick: () => void;
  onHover: (on: boolean) => void;
}) {
  const level = stageLevel(stage);
  const meta = level ? LEVELS[level] : null;
  const rules = stageRules(stage);
  const shown = weakestFirst(rules).slice(0, CHIPS_SHOWN);
  const x = stageX(index);
  const key = stageKey(stage.id);
  const dim = level === "not_used";
  return (
    <>
      <foreignObject x={x} y={STAGE_Y} width={STAGE_W} height={STAGE_H}>
        <div
          id={optionId(key)}
          role="option"
          aria-selected={selected}
          aria-label={stageAria(stage)}
          data-testid={`rules-stage-${stage.id}`}
          data-selected={selected ? "true" : undefined}
          data-level={level ?? "none"}
          onClick={onPick}
          onMouseEnter={() => onHover(true)}
          onMouseLeave={() => onHover(false)}
          className="flex h-full w-full cursor-pointer flex-col gap-1.5 overflow-hidden rounded-[12px] px-3 py-2.5 text-fg transition-[box-shadow,transform] duration-200 hover:-translate-y-0.5"
          style={{
            // The opened stage: an accent border and glow, readable at the strip's scale.
            border: selected ? "2px solid #c6f24e" : meta ? meta.stageBorder : "1.5px dashed #626c75",
            background: dim ? "#0f1317" : "#12171b",
            boxShadow: selected ? "0 0 0 3px rgba(198,242,78,.22), 0 0 24px rgba(198,242,78,.35)" : ring(false, focused),
            opacity: dim && !selected ? 0.5 : 1,
          }}
        >
          {!outline && (
            <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>
              {String(index + 1).padStart(2, "0")} · {stage.id.toUpperCase()}
            </span>
          )}
          <span className={cn("font-semibold leading-tight tracking-[-0.01em]", outline ? "text-[24px]" : "text-[15px]")}>
            {stage.title}
          </span>
          <StagePill level={level} big={outline} />
          {!outline && (
            <>
              <span className="mt-auto">
                <TallyLine rules={rules} />
              </span>
              <TallyBar rules={rules} />
            </>
          )}
        </div>
      </foreignObject>
      {!outline &&
        shown.map((r, j) => (
          <foreignObject key={r.id} x={x + 16} y={CHIP_Y + j * CHIP_STEP} width={CHIP_W} height={CHIP_H}>
            <RuleChip rule={r} onPick={onPick} />
          </foreignObject>
        ))}
      {!outline && rules.length > CHIPS_SHOWN && (
        <foreignObject x={x + 16} y={CHIP_Y + CHIPS_SHOWN * CHIP_STEP} width={CHIP_W} height={26}>
          <button
            type="button"
            tabIndex={-1}
            onClick={onPick}
            data-testid={`rules-more-${stage.id}`}
            className={cn(MONO, "h-[26px] rounded-[8px] border border-control bg-surface-2 px-2.5 text-[10.5px] text-fg-2 hover:border-control")}
            style={{ opacity: dim ? 0.5 : 1 }}
          >
            +{rules.length - CHIPS_SHOWN} more
          </button>
        </foreignObject>
      )}
    </>
  );
}

const TRUTH_LABEL: Record<string, string> = { db: "DB", code: "CODE", repo_file: "FILE", derived: "DERIVED" };

function RuleChip({ rule, onPick }: { rule: RuleFlowRule; onPick: () => void }) {
  const level = ruleLevel(rule);
  const meta = LEVELS[level];
  const changed = React.useContext(ChangedRulesContext).has(rule.id);
  return (
    <div
      onClick={onPick}
      data-changed={changed ? "true" : undefined}
      title={`${rule.id} — ${rule.title}${level === "external" ? " · not verified" : ""}`}
      data-testid={`rules-chip-${rule.id}`}
      data-level={level}
      className="flex h-full w-full cursor-pointer flex-col gap-1 overflow-hidden rounded-[10px] px-2.5 py-[7px]"
      style={{ border: meta.border, background: meta.bg, opacity: rule.used ? 1 : 0.5, boxShadow: changed ? "0 0 0 2px #5ad1a0" : undefined }}
    >
      <div className="flex items-center gap-1.5" style={{ color: meta.color }}>
        <RuleLevelIcon level={level} size={12} />
        <span className={cn(MONO, "text-meta font-semibold tracking-[0.06em]")}>{meta.word}</span>
        <span className="flex-1" />
        <span
          className={cn(MONO, "rounded-[4px] px-1 text-micro tracking-[0.04em]")}
          style={{
            border: `1px solid ${rule.truth === "db" ? "#626c75" : "#2a333a"}`,
            color: rule.truth === "db" ? "#e6e9ec" : "#8b949e",
          }}
        >
          {TRUTH_LABEL[rule.truth] ?? rule.truth.toUpperCase()}
        </span>
      </div>
      <span className="truncate text-[12px] leading-4" style={{ color: rule.used ? "#e6e9ec" : "#8b949e" }}>
        {rule.title}
      </span>
    </div>
  );
}

function PackNode({
  pack,
  selected,
  focused,
  onPick,
}: {
  pack: RuleFlowRule;
  selected: boolean;
  focused: boolean;
  onPick: () => void;
}) {
  const n = pack.skills?.length;
  return (
    <div
      id={optionId(PACK_KEY)}
      role="option"
      aria-selected={selected}
      aria-label={`${pack.title}, ${pack.enforcement}`}
      data-testid="rules-pack"
      onClick={onPick}
      className="flex h-full w-full cursor-pointer items-center gap-3 rounded-[12px] px-3 py-2"
      style={{
        border: LEVELS.advisory.border,
        background: LEVELS.advisory.bg,
        boxShadow: ring(selected, focused),
      }}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13px] font-semibold text-fg-2">
          Agent instructions <span className="font-normal text-muted">(Graphban pack)</span>
        </span>
        <span className="flex items-center gap-1.5">
          <LevelWord level={ruleLevel(pack)} className="text-meta" />
          <span className={cn(MONO, "text-meta text-muted")}>
            {n === undefined || n === null ? "· skill list not readable here" : `· ${n} skills`}
          </span>
        </span>
      </div>
    </div>
  );
}

function Legend({ counts }: { counts: Record<Level, number> | null }) {
  return (
    <div
      data-testid="rules-legend"
      className="absolute bottom-[60px] left-4 z-[6] flex flex-col gap-[5px] rounded-[12px] border border-line-2 bg-surface-2 px-3 py-2.5 shadow-lg"
    >
      <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>ENFORCEMENT</span>
      {LEVEL_ORDER.map((l) => (
        <span key={l} data-testid={`rules-legend-${l}`} className="flex items-center gap-2">
          <LevelWord level={l} className="text-[10px]" />
          {l === "not_used" && <span className="text-[10px] text-muted">not used by this project's model</span>}
          <span className="flex-1" />
          <span className={cn(MONO, "min-w-4 text-right text-[10px] text-fg-2")}>
            {counts ? counts[l] : ""}
          </span>
        </span>
      ))}
    </div>
  );
}

function ZoomControls({
  pct,
  outline,
  onIn,
  onOut,
  onFit,
  onOneToOne,
}: {
  pct: number;
  outline: boolean;
  onIn: () => void;
  onOut: () => void;
  onFit: () => void;
  onOneToOne: () => void;
}) {
  const btn = "h-7 rounded-[7px] px-2 text-fg hover:bg-surface-3";
  return (
    <div className="absolute bottom-4 left-4 z-[6] flex items-center gap-2">
      <div className="flex items-center gap-0.5 rounded-[10px] border border-line-2 bg-surface-2 p-[3px] shadow-lg">
        <button type="button" aria-label="Zoom out" onClick={onOut} className={cn(btn, "w-7 text-[16px]")}>−</button>
        <span className={cn(MONO, "min-w-[46px] text-center text-[10.5px]")} data-testid="rules-zoom-pct">{pct}%</span>
        <button type="button" aria-label="Zoom in" onClick={onIn} className={cn(btn, "w-7 text-[16px]")}>+</button>
        <span className="mx-[3px] h-[18px] w-px bg-line-2" />
        <button type="button" onClick={onFit} className={cn(btn, MONO, "text-[10.5px]")}>Fit</button>
        <button type="button" onClick={onOneToOne} className={cn(btn, MONO, "text-[10.5px]")}>1:1</button>
      </div>
      <span className={cn(MONO, "rounded-md border border-line bg-surface-2/90 px-2 py-1 text-[10px] text-muted-2")} data-testid="rules-lod">
        {outline ? "stages only" : "full detail"} · scroll to zoom, drag to pan
      </span>
    </div>
  );
}

// ── Edge states ────────────────────────────────────────────────────────────────

function LoadingMap() {
  return (
    <div className="absolute inset-0 p-6" aria-busy="true" aria-label="Loading the rules map" data-testid="rules-loading">
      <div className="flex gap-8">
        {Array.from({ length: 7 }, (_, i) => (
          <div key={i} className="flex h-32 w-[208px] flex-none flex-col justify-end gap-2 rounded-[12px] border border-line-2 bg-surface-2 p-3">
            <span className="h-[9px] w-[70%] rounded bg-surface-3" />
            <span className="h-1 w-full rounded bg-surface-3" />
          </div>
        ))}
      </div>
      <Legend counts={null} />
    </div>
  );
}

function LoadError({ detail, onRetry }: { detail: string; onRetry: () => void }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div
        role="alert"
        data-testid="rules-error"
        className="flex max-w-[520px] flex-col gap-2 rounded-[12px] p-4"
        style={{ border: "1.5px solid #ff6b6b", background: "rgba(255,107,107,.05)" }}
      >
        <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-[#ff6b6b]")}>COULD NOT LOAD</span>
        <span className="text-[15px] font-semibold">The rules map didn't load</span>
        <span className={cn(MONO, "text-[11.5px] text-fg-2")}>GET /api/rules/flow → {detail}</span>
        <span className="text-body text-muted">
          Nothing is known, so no map is drawn. This isn't an empty contract — the server still refuses what it refuses.
        </span>
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 h-8 self-start rounded-[8px] bg-accent px-3.5 text-body font-semibold text-[#0a0c0e]"
        >
          Retry
        </button>
      </div>
    </div>
  );
}

/** Frame 6e: the same stages, pills and order, as a list. */
function NarrowList({
  flow,
  sel,
  onSelect,
}: {
  flow: RulesFlow;
  sel: string | null;
  onSelect: (key: string | null) => void;
}) {
  const pack = packOf(flow);
  const counts = legendCounts(flow);
  return (
    <div className="absolute inset-0 overflow-y-auto p-4 pb-40" data-testid="rules-narrow">
      <ol className="flex flex-col gap-2" aria-label="Rules stages">
        {flow.stages.map((s, i) => {
          const level = stageLevel(s);
          const key = stageKey(s.id);
          return (
            <li key={s.id}>
              <button
                type="button"
                aria-pressed={sel === key}
                aria-label={stageAria(s)}
                data-testid={`rules-stage-${s.id}`}
                onClick={() => onSelect(sel === key ? null : key)}
                className="flex w-full flex-col gap-1.5 rounded-[12px] px-3 py-2.5 text-left"
                style={{
                  border: level ? LEVELS[level].stageBorder : "1.5px dashed #626c75",
                  background: "#12171b",
                  opacity: level === "not_used" ? 0.5 : 1,
                  boxShadow: ring(sel === key, false),
                }}
              >
                <span className="flex items-center gap-2">
                  <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="text-[15px] font-semibold">{s.title}</span>
                  <span className="flex-1" />
                  <StagePill level={level} />
                </span>
                <TallyLine rules={stageRules(s)} />
                <TallyBar rules={stageRules(s)} />
              </button>
            </li>
          );
        })}
      </ol>
      {pack && (
        <button
          type="button"
          onClick={() => onSelect(sel === PACK_KEY ? null : PACK_KEY)}
          className="mt-3 flex w-full items-center gap-2 rounded-[12px] px-3 py-2.5 text-left"
          style={{ border: LEVELS.advisory.border }}
        >
          <span className="text-[13px] font-semibold">Agent instructions (Graphban pack)</span>
          <span className="flex-1" />
          <LevelWord level="advisory" className="text-meta" />
        </button>
      )}
      {/* Docked, the list is a strip over the editor: no legend (GRPH-1725). */}
      {!sel && (
        <div className="mt-4 flex flex-wrap gap-3" data-testid="rules-legend">
          {LEVEL_ORDER.map((l) => (
            <span key={l} data-testid={`rules-legend-${l}`} className="flex items-center gap-1.5">
              <LevelWord level={l} className="text-[10px]" />
              {l === "not_used" && <span className="text-[10px] text-muted">not used by this project's model</span>}
              <span className={cn(MONO, "text-[10px] text-fg-2")}>{counts[l]}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Stage view (replaces the 400px inspector panel; GRPH-1659) ─────────────────

// ── Docked editor (GRPH-1718, layout C) ────────────────────────────────────────

const DOCK_KEY = "rules.dock.mapPct";
/** The map's share of the height with the dock open; the dock takes the rest. */
const DOCK_PRESETS = [["HALF", 46], ["TALL", 34], ["FULL", 18]] as const;
const DOCK_DEFAULT = 34;
const DOCK_MIN = 12;
const DOCK_MAX = 70;

function readDockPct(): number {
  try {
    const v = Number(window.localStorage.getItem(DOCK_KEY));
    return v >= DOCK_MIN && v <= DOCK_MAX ? v : DOCK_DEFAULT;
  } catch {
    return DOCK_DEFAULT;
  }
}

function StageDock({
  flow,
  sel,
  focusRule,
  prefill,
  mapPct,
  onMapPct,
  containerRef,
  onClose,
  onDirty,
  leave,
}: {
  flow: RulesFlow;
  sel: string;
  focusRule: string | null;
  prefill: { rule: string; value: string } | null;
  mapPct: number;
  onMapPct: (pct: number) => void;
  containerRef: React.RefObject<HTMLDivElement | null>;
  onClose: () => void;
  onDirty: (dirty: boolean) => void;
  /** A stage switch or close is waiting on a dirty draft: ask before throwing it away. */
  leave: { closing: boolean; onLeave: () => void; onStay: () => void } | null;
}) {
  const pack = sel === PACK_KEY ? packOf(flow) : null;
  const index = flow.stages.findIndex((s) => stageKey(s.id) === sel);
  const stage = index >= 0 ? flow.stages[index] : null;
  const ref = React.useRef<HTMLElement | null>(null);
  const [ruleTitle, setRuleTitle] = React.useState<string | null>(null);
  const onRule = React.useCallback((r: RuleFlowRule | null) => setRuleTitle(r?.title ?? null), []);
  // Opening takes focus (Escape then closes it); closing hands focus back to what opened it.
  // A refusal's ?rule= focus belongs to the rule's own row, so the dock leaves it there.
  React.useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    if (!focusRule) ref.current?.focus({ preventScroll: true });
    return () => {
      if (opener && opener !== document.body) opener.focus?.({ preventScroll: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!pack && !stage) return null;
  const title = pack ? pack.title : stage!.title;

  function drag(e: React.PointerEvent<HTMLDivElement>) {
    const box = containerRef.current?.getBoundingClientRect();
    if (!box || box.height === 0) return;
    e.preventDefault();
    const move = (ev: PointerEvent) => {
      const pct = ((ev.clientY - box.top) / box.height) * 100;
      onMapPct(Math.min(DOCK_MAX, Math.max(DOCK_MIN, Math.round(pct))));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  return (
    <section
      ref={ref}
      tabIndex={-1}
      aria-label={`${title} ${pack ? "inspector" : "stage inspector"}`}
      data-testid="rules-inspector"
      onKeyDown={(e) => {
        if (e.key === "Escape" && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement).tagName)) onClose();
      }}
      className="flex min-h-0 flex-1 flex-col border-t border-line-2 outline-none"
    >
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize the editor"
        data-testid="rules-dock-handle"
        onPointerDown={drag}
        className="h-1.5 flex-none cursor-row-resize bg-surface-2 hover:bg-accent/40"
      />
      <div className="flex flex-none flex-wrap items-center gap-2 border-b border-line px-4 py-1.5">
        <span className={cn(MONO, "flex-none text-meta tracking-[0.07em] text-muted-2")}>
          {pack ? "AGENT INSTRUCTIONS" : `STAGE ${String(index + 1).padStart(2, "0")} · ${title.toUpperCase()}`}
        </span>
        {!pack && ruleTitle && (
          <span className="min-w-0 truncate text-body font-semibold text-fg" data-testid="rules-dock-rule-title">
            {ruleTitle}
          </span>
        )}
        <span className="flex-1" />
        <div role="group" aria-label="Editor height" className="flex gap-1">
          {DOCK_PRESETS.map(([label, pct]) => (
            <button
              key={label}
              type="button"
              aria-pressed={mapPct === pct}
              data-testid={`rules-dock-height-${label.toLowerCase()}`}
              onClick={() => onMapPct(pct)}
              className={cn(
                MONO,
                "h-6 rounded-[8px] border px-2 text-meta tracking-[0.05em]",
                mapPct === pct ? "border-accent text-accent" : "border-control text-fg-2 hover:text-fg",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close editor"
          data-testid="rules-dock-close"
          className="h-6 w-6 rounded-[8px] border border-control text-small leading-none text-muted hover:text-fg"
        >
          ×
        </button>
      </div>
      {leave && (
        <div
          role="alertdialog"
          aria-label="Unsaved changes"
          data-testid="rules-stage-leave"
          className="flex flex-none flex-wrap items-center gap-2 border-b border-accent bg-surface-2 px-4 py-2"
        >
          <span className="text-small text-fg">
            You have unsaved changes on this stage. {leave.closing ? "Close the editor" : "Switch stage"} without saving?
          </span>
          <button
            type="button"
            data-testid="rules-stage-leave-confirm"
            onClick={leave.onLeave}
            className="h-6 rounded-[8px] border border-control px-2 text-small text-fg"
          >
            Discard and leave
          </button>
          <button
            type="button"
            data-testid="rules-stage-leave-stay"
            onClick={leave.onStay}
            className="h-6 rounded-[8px] border border-control px-2 text-small text-fg-2"
          >
            Keep editing
          </button>
        </div>
      )}
      {pack ? (
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <p className="mb-2 text-body leading-normal text-muted">{pack.summary}</p>
          <PackBody pack={pack} />
        </div>
      ) : (
        <DockEditor
          key={stage!.id}
          stage={stage!}
          version={flow.version}
          model={flow.model}
          focusRule={focusRule}
          prefill={prefill}
          onClose={onClose}
          onDirty={onDirty}
          onRule={onRule}
        />
      )}
    </section>
  );
}

function PackBody({ pack }: { pack: RuleFlowRule }) {
  if (!pack.skills) {
    return (
      <div className="rounded-[8px] px-3 py-2.5 text-[12px] text-muted" style={{ border: "1.5px dashed #626c75" }}>
        The gban package isn't installed next to this server, so the skill list can't be read. That is not an empty pack.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-1" data-testid="rules-pack-skills">
      {pack.skills.map((s) => (
        <li key={s.name} className={cn(MONO, "text-[11.5px] text-fg-2")}>
          <a href={s.url} target="_blank" rel="noreferrer" className="underline">
            skills/{s.name}/SKILL.md
          </a>
        </li>
      ))}
    </ul>
  );
}

const CONTROL_BANNERS = {
  linked_set: { label: "LINKED", color: "#c9b8ff", text: "Gitops values come from your org." },
  linked_unset: { label: "LINKED · NOT SET", color: "#dfe3e7", text: "Your org has not set a gitops contract for this project." },
  linked_unreachable: { label: "ORG UNREACHABLE", color: "#ff6b6b", text: "The org's gitops could not be reached, so no values are shown." },
} as const;

/** Frames 3b–3d: which plane holds the gitops contract, and the pill that says so. */
function ControlBanner({ state, gitops, compact = false }: { state: string | null; gitops: ReturnType<typeof useGitops>["data"]; compact?: boolean }) {
  const orgName = useOrgName(gitops);
  if (state !== "linked_set" && state !== "linked_unset" && state !== "linked_unreachable") return null;
  const b = CONTROL_BANNERS[state];
  if (compact) {
    return (
      <div
        role="status"
        data-testid={`rules-control-banner-${state}`}
        data-collapsed="true"
        className="mx-5 mt-1 flex flex-none items-center gap-2 overflow-hidden whitespace-nowrap text-small"
      >
        <span className={cn(MONO, "text-meta font-semibold tracking-[0.07em]")} style={{ color: b.color }}>{b.label}</span>
        <span className="truncate text-fg-2">{gitops?.control.message || b.text}</span>
      </div>
    );
  }
  return (
    <div
      role="status"
      data-testid={`rules-control-banner-${state}`}
      className="mx-5 mt-3 flex flex-wrap items-center gap-3 rounded-[12px] bg-surface-2 px-3 py-2.5"
      style={{ border: `1px ${state === "linked_unreachable" ? "dotted" : "solid"} ${b.color}` }}
    >
      <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.07em]")} style={{ color: b.color }}>{b.label}</span>
      <span className="flex-1 text-body text-fg-2">
        {gitops?.control.message || b.text}
      </span>
      <span
        data-testid="rules-control-pill"
        className={cn(MONO, "rounded-[4px] border border-control px-1.5 text-meta tracking-[0.04em] text-fg-2")}
      >
        ORG · {orgName}
      </span>
      <AdminGitopsLink />
    </div>
  );
}

