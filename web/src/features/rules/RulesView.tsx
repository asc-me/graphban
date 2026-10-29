/**
 * Rules (GRPH-P61 §5, GRPH-1162): the project's delivery process as one read-only map.
 *
 * Seven fixed stages with their rules hung underneath, each rule drawn at what actually holds
 * it. Everything on screen comes from `GET /api/rules/flow`; stage state and not-used are the
 * server's (`services/rules.py:flow`). Look: `docs/design/Rules.dc.html`, checklist:
 * `docs/design/rules-view-design.md`. Editing is UI 2/3; refusals and Recommended are UI 3/3.
 */
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { stepIndex } from "@/lib/graph/useGraphKeyboard";
import { useGraphViewport, zoomAbout } from "@/lib/graph/useGraphViewport";
import { useRulesFlow } from "@/lib/queries";
import type { RuleFlowRule, RuleStage, RulesFlow } from "@/lib/types";

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
  enforcerKindLabel,
  enforcerText,
  externalText,
  legendCounts,
  mapLayout,
  packOf,
  quietLine,
  ruleLevel,
  sourceLabel,
  stageLevel,
  stageRules,
  stageX,
  tally,
  tallyText,
  valueText,
  weakestFirst,
  type Level,
  type MapLayout,
} from "./levels";
import { BudgetCapEditor, GITOPS_RULE_FIELDS, GitopsRuleEditor } from "./RuleEditors";
import { RuleLevelIcon } from "./RuleLevelIcon";

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
  const flow = q.data;
  const narrow = useNarrow();
  const [params] = useSearchParams();
  const linked = params.get("stage");
  const [sel, setSel] = React.useState<string | null>(linked ? stageKey(linked) : null);

  // A ?stage= that names no stage on this payload selects nothing, rather than an empty inspector.
  React.useEffect(() => {
    if (!flow || !sel || sel === PACK_KEY) return;
    if (!flow.stages.some((s) => stageKey(s.id) === sel)) setSel(null);
  }, [flow, sel]);

  const fresh = !!flow && flow.model === null;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="rules-view">
      <PlaceHeader
        viewName={`Rules ${scope}`}
        purpose={`How work moves through ${scope}, and what holds each step.`}
        action={<ModelPill flow={flow} loading={q.isLoading} />}
      />
      {fresh && (
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
            onClick={() => setSel(stageKey("merge"))}
            className="h-[30px] rounded-[8px] bg-accent px-3 text-body font-semibold text-[#0a0c0e]"
          >
            Open Merge
          </button>
        </div>
      )}
      <div className="relative min-h-0 flex-1 overflow-hidden">
        {q.isError && !flow ? (
          <LoadError detail={errorDetail(q.error, "request failed")} onRetry={() => void q.refetch()} />
        ) : !flow ? (
          <LoadingMap />
        ) : narrow ? (
          <NarrowList flow={flow} sel={sel} onSelect={setSel} />
        ) : (
          <RulesCanvas flow={flow} sel={sel} onSelect={setSel} />
        )}
        {flow && sel && (
          <Inspector flow={flow} sel={sel} sheet={narrow} onClose={() => setSel(null)} />
        )}
      </div>
    </div>
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
}: {
  flow: RulesFlow;
  sel: string | null;
  onSelect: (key: string | null) => void;
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
  const keys = React.useMemo(
    () => [...flow.stages.map((s) => stageKey(s.id)), ...(pack ? [PACK_KEY] : [])],
    [flow.stages, pack],
  );
  const [focus, setFocus] = React.useState<string | null>(null);
  const [hover, setHover] = React.useState<string | null>(null);

  // Fit never enlarges past 1:1; the legend and zoom bar keep the bottom 120px.
  const { setViewport } = view;
  const fit = React.useCallback(() => {
    const k = Math.max(0.2, Math.min(1, (size.w - 48) / W, (size.h - 140) / H));
    setViewport({ k, x: (size.w - W * k) / 2, y: Math.max(16, (size.h - 120 - H * k) / 2) });
  }, [setViewport, size.w, size.h, W, H]);
  React.useEffect(() => {
    fit();
  }, [fit]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const at = focus ? keys.indexOf(focus) : -1;
    const go = (i: number) => {
      e.preventDefault();
      const k = keys[i];
      if (!k) return;
      setFocus(k);
      // The inspector follows focus once it is open, like the mock.
      if (sel) onSelect(k);
    };
    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
        return go(stepIndex(keys.length, at, 1));
      case "ArrowLeft":
      case "ArrowUp":
        return go(stepIndex(keys.length, at, -1));
      case "Home":
        return go(0);
      case "End":
        return go(keys.length - 1);
      case "Enter":
      case " ":
        if (!focus) return;
        e.preventDefault();
        onSelect(focus);
        return;
      case "Escape":
        e.preventDefault();
        if (sel) onSelect(null);
        else setFocus(null);
        return;
    }
  };

  const pick = (key: string) => {
    setFocus(key);
    onSelect(sel === key ? null : key);
  };

  const zoomBy = (factor: number) =>
    view.setViewport((v) => zoomAbout(v, factor, size.w / 2, size.h / 2));
  const counts = legendCounts(flow);

  return (
    <div ref={boxRef} className="absolute inset-0" data-testid="rules-canvas">
      <div
        role="listbox"
        tabIndex={0}
        aria-label="Rules map. Left and right move between stages, Enter opens, Escape closes."
        aria-activedescendant={focus ? optionId(focus) : undefined}
        data-testid="rules-listbox"
        onKeyDown={onKeyDown}
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

      <Legend counts={counts} />
      <ZoomControls
        pct={Math.round(view.viewport.k * 100)}
        outline={outline}
        onIn={() => zoomBy(1.2)}
        onOut={() => zoomBy(1 / 1.2)}
        onFit={fit}
        onOneToOne={() => view.setViewport((v) => zoomAbout(v, 1 / v.k, size.w / 2, size.h / 2))}
      />
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
          data-level={level ?? "none"}
          onClick={onPick}
          onMouseEnter={() => onHover(true)}
          onMouseLeave={() => onHover(false)}
          className="flex h-full w-full cursor-pointer flex-col gap-1.5 overflow-hidden rounded-[12px] px-3 py-2.5 text-fg transition-[box-shadow,transform] duration-200 hover:-translate-y-0.5"
          style={{
            border: meta ? meta.stageBorder : "1.5px dashed #626c75",
            background: dim ? "#0f1317" : "#12171b",
            boxShadow: ring(selected, focused),
            opacity: dim ? 0.5 : 1,
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
  return (
    <div
      onClick={onPick}
      title={`${rule.id} — ${rule.title}${level === "external" ? " · not verified" : ""}`}
      data-testid={`rules-chip-${rule.id}`}
      data-level={level}
      className="flex h-full w-full cursor-pointer flex-col gap-1 overflow-hidden rounded-[10px] px-2.5 py-[7px]"
      style={{ border: meta.border, background: meta.bg, opacity: rule.used ? 1 : 0.5 }}
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
      <div className="mt-4 flex flex-wrap gap-3" data-testid="rules-legend">
        {LEVEL_ORDER.map((l) => (
          <span key={l} data-testid={`rules-legend-${l}`} className="flex items-center gap-1.5">
            <LevelWord level={l} className="text-[10px]" />
            <span className={cn(MONO, "text-[10px] text-fg-2")}>{counts[l]}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Inspector (frames 2a–2e) ───────────────────────────────────────────────────

function Inspector({
  flow,
  sel,
  sheet,
  onClose,
}: {
  flow: RulesFlow;
  sel: string;
  sheet: boolean;
  onClose: () => void;
}) {
  const pack = sel === PACK_KEY ? packOf(flow) : null;
  const index = flow.stages.findIndex((s) => stageKey(s.id) === sel);
  const stage = index >= 0 ? flow.stages[index] : null;
  if (!pack && !stage) return null;

  const title = pack ? pack.title : stage!.title;
  const kicker = pack
    ? `OFF THE MAIN FLOW · LINKED TO ${pack.stage.toUpperCase()}`
    : `STAGE ${String(index + 1).padStart(2, "0")} · ${stage!.id.toUpperCase()}`;
  const level = pack ? ruleLevel(pack) : stageLevel(stage!);
  const rules = stage ? weakestFirst(stageRules(stage)) : [];

  return (
    <aside
      role="dialog"
      aria-label={`${title} ${pack ? "inspector" : "stage inspector"}`}
      data-testid="rules-inspector"
      className={cn(
        "z-10 flex flex-col overflow-hidden border border-line-2 bg-surface-2 shadow-2xl",
        sheet
          ? "absolute inset-x-0 bottom-0 max-h-[70%] rounded-t-[16px]"
          : "absolute right-4 top-4 bottom-4 w-[400px] rounded-[16px]",
      )}
    >
      <div className="flex gap-3 border-b border-line px-4 py-3.5">
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>{kicker}</span>
          <span className="text-[16px] font-semibold leading-tight">{title}</span>
          <div className="flex flex-wrap items-center gap-2">
            <StagePill level={level} />
            {stage && <TallyLine rules={stageRules(stage)} />}
            {pack && (
              <span className={cn(MONO, "text-[10.5px] text-muted")}>
                {pack.skills ? `${pack.skills.length} skills shipped with gban` : "skill list not readable on this server"}
              </span>
            )}
          </div>
          {stage && <TallyBar rules={stageRules(stage)} />}
          <span className="text-body leading-normal text-muted">{pack ? pack.summary : stage!.summary}</span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close inspector"
          className="h-7 w-7 flex-none rounded-[8px] border border-control text-[15px] leading-none text-muted hover:text-fg"
        >
          ×
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3">
        {pack && <PackBody pack={pack} />}
        {stage && level === "not_used" && (
          <div className="flex flex-col gap-1 rounded-[8px] px-3 py-2.5" style={{ border: "1.5px dashed #1e242a" }}>
            <LevelWord level="not_used" className="text-[10px]" />
            <span className="text-[12px] leading-snug text-muted">
              Every rule on this stage is not used by this project's model: {flow.model}. They stay drawn so you can see what would apply.
            </span>
          </div>
        )}
        {stage && rules.length === 0 && (
          <div className="flex flex-col gap-1.5 rounded-[12px] p-3.5" style={{ border: "1.5px dashed #626c75" }} data-testid="rules-stage-empty">
            <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.06em] text-fg-2")}>NOTHING REGISTERED</span>
            <span className="text-[13px] font-semibold">No rules registered for this stage yet</span>
            <span className="text-[12px] leading-normal text-muted">
              Nothing in the registry governs this stage yet. That isn't the same as nothing applying.
            </span>
          </div>
        )}
        {rules.map((r) => (
          <RuleRow key={r.id} rule={r} version={flow.version} model={flow.model} />
        ))}
      </div>
    </aside>
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
        <li key={s} className={cn(MONO, "text-[11.5px] text-fg-2")}>
          skills/{s}/SKILL.md
        </li>
      ))}
    </ul>
  );
}

function RuleRow({ rule, version, model }: { rule: RuleFlowRule; version: string; model: string | null }) {
  const level = ruleLevel(rule);
  const quiet = quietLine(rule);
  const enforcer = enforcerText(rule, version);
  const value = valueText(rule.value);
  return (
    <div
      data-testid={`rules-row-${rule.id}`}
      data-level={level}
      className="grid grid-cols-[100px_minmax(0,1fr)] gap-2.5 rounded-[12px] border border-[#2a333a] p-3"
    >
      <span className="pt-0.5" style={{ opacity: rule.used ? 1 : 0.55 }}>
        <LevelWord level={level} size={13} className="text-[10px]" />
      </span>
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex flex-col gap-1.5" style={{ opacity: rule.used ? 1 : 0.55 }}>
          <span className="text-[13px] font-semibold leading-snug text-fg">{rule.title}</span>
          <span className="text-[12px] leading-snug text-muted">{rule.summary}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={cn(MONO, "break-all text-[10.5px] text-fg-2")}>{rule.id}</span>
            <span
              className={cn(MONO, "rounded-[4px] px-1 text-[9px] tracking-[0.04em]")}
              style={{
                border: `1px solid ${rule.truth === "db" ? "#626c75" : "#2a333a"}`,
                color: rule.truth === "db" ? "#e6e9ec" : "#8b949e",
              }}
            >
              {rule.truth}
            </span>
          </div>
          <EnforcerLink kind={rule.enforcer.kind} text={enforcer} url={rule.enforcer.url} />
        </div>
        {!rule.used && (
          <span className="text-[11.5px] leading-snug text-muted" data-testid="rules-row-not-used">
            Not used by this project's model{model ? `: ${model}` : ""}.
          </span>
        )}
        {quiet && <span className="text-[11.5px] leading-snug text-muted-2">{quiet}</span>}
        {rule.used && rule.enforcement === "external" && (
          <div className="flex flex-col gap-1 rounded-[8px] px-2.5 py-2" style={{ border: "3px double rgba(167,139,250,.45)" }}>
            <span className={cn(MONO, "flex items-center gap-1.5 text-[10px] font-semibold tracking-[0.06em] text-[#c9b8ff]")}>
              <span className="h-[7px] w-[7px] rounded-full border-[1.5px] border-[#c9b8ff]" />
              NOT VERIFIED
            </span>
            <span className="text-[11.5px] leading-snug text-fg-2">{externalText(rule)}</span>
          </div>
        )}
        {rule.used && rule.enforcement === "checked" && (
          // The flow payload carries no attestation for a checked rule yet, so its result is
          // unknown — said as such, never drawn as a pass.
          <div
            data-testid="rules-no-result"
            className="flex flex-col gap-0.5 rounded-[8px] px-2.5 py-[7px]"
            style={{
              border: "1.5px dashed #626c75",
              background: "repeating-linear-gradient(45deg,transparent 0 6px,rgba(139,148,158,.1) 6px 7px)",
            }}
          >
            <span className={cn(MONO, "flex items-center gap-1.5 text-[10px] font-semibold tracking-[0.06em] text-fg-2")}>
              <span className="h-[7px] w-[7px] rounded-full border-[1.5px] border-muted" />
              NO RESULT RECORDED
            </span>
            <span className="text-[11.5px] text-muted">Nothing has reported yet. That is not a pass.</span>
          </div>
        )}
        {rule.truth === "db" && (
          <ValueBox rule={rule} value={value} />
        )}
        {rule.id === "budget.cap" && rule.used && <BudgetCapEditor />}
      </div>
    </div>
  );
}

function ValueBox({ rule, value }: { rule: RuleFlowRule; value: string | null }) {
  const box = (
          <div className="flex flex-col gap-2 rounded-[8px] border border-[#2a333a] bg-bg p-2.5">
            <div className="flex items-center gap-2">
              <span className={cn(MONO, "text-[10px] text-muted-2")}>{rule.id.split(".").pop()}</span>
              <span className="flex-1" />
              <span
                className={cn(MONO, "rounded-[4px] px-1.5 text-meta tracking-[0.04em]")}
                style={{
                  border: `1px ${rule.source === "project" || rule.source === "org" ? "solid" : "dashed"} ${
                    rule.source === "project" ? "rgba(167,139,250,.42)" : rule.source === "org" ? "#626c75" : "#ff6b6b"
                  }`,
                  color: rule.source === "project" ? "#c9b8ff" : rule.source === "org" ? "#dfe3e7" : "#ff6b6b",
                }}
              >
                {sourceLabel(rule.source)}
              </span>
            </div>
            <span
              className={cn(MONO, "text-[13px] font-semibold")}
              style={{ color: value === null ? "#ff6b6b" : "#e6e9ec" }}
            >
              {value ?? "— no value"}
            </span>
          </div>
  );
  const field = GITOPS_RULE_FIELDS[rule.id];
  return field ? <GitopsRuleEditor rule={rule} field={field} fallback={box} /> : box;
}

function EnforcerLink({ kind, text, url }: { kind: string; text: string; url: string }) {
  const body = (
    <>
      <span className="flex-none rounded-[3px] border border-[#2a333a] px-1 text-[9px] tracking-[0.05em] text-muted">
        {enforcerKindLabel(kind)}
      </span>
      <span>{text}</span>
    </>
  );
  const cls = cn(MONO, "flex items-center gap-1.5 break-all text-small leading-snug");
  // No URL from the server means no link — a guessed one would read as a verified source.
  if (!url) return <span className={cn(cls, "text-fg-2")} data-testid="rules-enforcer">{body}</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className={cn(cls, "text-accent hover:underline")} data-testid="rules-enforcer">
      {body}
      <span aria-hidden>↗</span>
    </a>
  );
}
