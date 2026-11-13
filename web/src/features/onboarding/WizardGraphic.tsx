/**
 * The New project wizard's left panel (GRPH-1587): the hub-and-spoke graphic from the
 * Claude Design "Project Wizard" (local edition), its logo mark and wordmark, and the caption
 * with the project's accent square.
 *
 * One drawing, five shapes:
 *  - pulse    — identity: the project tile pulsing in the middle.
 *  - single   — repo / review: a hub on the left feeding one target tile. `lit` once there is
 *               something real to connect (a valid remote, a created project).
 *  - fan      — model: the hub fans out to OFFLINE / OLLAMA / CLAUDE, the chosen one drawn.
 *  - broken   — a failed create: the line amber, dashed, stalled halfway.
 *
 * Everything moving carries `pw-anim`; under prefers-reduced-motion nothing gets an animation
 * at all (not just a CSS override), so the panel is a still frame.
 */
import * as React from "react";

export type GraphicSpec =
  | { kind: "pulse"; label: string }
  | { kind: "single"; from: string; to: string; lit?: boolean; broken?: boolean }
  | { kind: "fan"; from: string; to: string[]; sel: number };

const W = 380;
const H = 640;
const MONO = "var(--font-mono)";
const DUR = 3.6;

// Illustration palette. The accent and status colours are tokens; the graph greys below are
// the design's own and have no token (they sit between --color-bg and --color-line).
const C = {
  accent: "var(--color-accent)",
  amber: "var(--color-st-review)",
  amberLine: "rgba(224,179,74,.5)",
  amberTile: "rgba(224,179,74,.45)",
  muted: "var(--color-muted)",
  tileBg: "#0a0c0e",
  bgEdge: "#1c2227",
  bgNode: "#2a3137",
  edge: "#262d33",
  ring: "#252c32",
  node: "#4a535c",
  label: "#7b858e",
};

const KEYFRAMES = `
@keyframes pwDraw{0%{stroke-dashoffset:1;opacity:1}55%{stroke-dashoffset:0;opacity:1}85%{stroke-dashoffset:0;opacity:1}100%{stroke-dashoffset:0;opacity:0}}
@keyframes pwRide{0%{offset-distance:0%;opacity:0}6%{opacity:1}55%{offset-distance:100%;opacity:1}62%,100%{offset-distance:100%;opacity:0}}
@keyframes pwHot{0%,100%{stroke:${C.node}}20%,70%{stroke:#c6f24e}}
@keyframes pwHub{0%,100%{fill:${C.node}}10%,80%{fill:#c6f24e}}
@keyframes pwLand{0%,52%{stroke:${C.ring}}60%,85%{stroke:#c6f24e}100%{stroke:${C.ring}}}
@keyframes pwRot{to{transform:rotate(360deg)}}
@keyframes pwRing{0%{transform:scale(.7);opacity:.9}100%{transform:scale(2.1);opacity:0}}
@keyframes pwBlink{0%,100%{opacity:.25}50%{opacity:1}}
@keyframes pwEnter{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes pwReveal{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
@keyframes pwSpin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.pw-anim{animation:none!important}}
`;

/** The wizard's keyframes, mounted once inside the dialog. */
export function WizardKeyframes() {
  return <style>{KEYFRAMES}</style>;
}

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function LogoMark({ size = 18, color = C.accent }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <GbGlyphPaths color={color} />
    </svg>
  );
}

function GbGlyphPaths({ color }: { color: string }) {
  return (
    <>
      <path d="M5 4.25V19.75M5 6H19M5 12H15M5 18H11" fill="none" stroke={color} strokeWidth={2.5} strokeLinecap="square" />
      <path d="M19 6L11 18" fill="none" stroke={color} strokeWidth={1.25} strokeLinecap="round" />
      <circle cx={19} cy={6} r={2.5} fill={color} />
      <circle cx={15} cy={12} r={2.5} fill={color} />
      <circle cx={11} cy={18} r={2.5} fill={color} />
    </>
  );
}

function Glyph({ cx, cy, size, color }: { cx: number; cy: number; size: number; color: string }) {
  return (
    <g transform={`translate(${cx - size / 2} ${cy - size / 2}) scale(${size / 24})`}>
      <GbGlyphPaths color={color} />
    </g>
  );
}

// The background constellation is seeded, so every render draws the same field.
const BG: [number, number][] = (() => {
  let seed = 11;
  const r = () => {
    seed = (seed * 9301 + 49297) % 233280;
    return seed / 233280;
  };
  const pts: [number, number][] = [];
  for (let i = 0; i < Math.round(14 + (W * H) / 40000); i++) pts.push([r() * W, r() * H]);
  return pts;
})();

export function HeroDrawing({ spec, still }: { spec: GraphicSpec; still: boolean }) {
  // `anim` returns the style only when motion is allowed: a reduced-motion user gets no
  // animation property on any element, not one a stylesheet has to cancel.
  const anim = (s: React.CSSProperties): React.CSSProperties | undefined => (still ? undefined : s);
  const kids: React.ReactNode[] = [];
  const label = (key: string, x: number, y: number, txt: string, anchor: "start" | "middle" | "end" = "start", color?: string) =>
    kids.push(
      <text key={key} x={x} y={y} fill={color || C.label} fontFamily={MONO} fontSize={10.5} letterSpacing={0.7} textAnchor={anchor}>
        {txt}
      </text>,
    );

  BG.forEach((p, i) => {
    const q = BG[(i + 3) % BG.length];
    if (i % 2 === 0 && Math.hypot(p[0] - q[0], p[1] - q[1]) < 200)
      kids.push(<line key={`be${i}`} x1={p[0]} y1={p[1]} x2={q[0]} y2={q[1]} stroke={C.bgEdge} strokeWidth={1} />);
  });
  BG.forEach((p, i) => kids.push(<circle key={`b${i}`} cx={p[0]} cy={p[1]} r={2.5} fill={C.tileBg} stroke={C.bgNode} strokeWidth={1.25} />));

  if (spec.kind === "pulse") {
    const cx = W / 2;
    const cy = H / 2;
    const ts = 52;
    [0, 1, 2].forEach((i) =>
      kids.push(
        <circle
          key={`pr${i}`}
          className="pw-anim"
          cx={cx}
          cy={cy}
          r={ts * 0.62}
          fill="none"
          stroke={C.accent}
          strokeWidth={1}
          style={{ transformOrigin: `${cx}px ${cy}px`, opacity: 0, ...anim({ animation: `pwRing 2.4s ease-out ${i * 0.8}s infinite` }) }}
        />,
      ),
    );
    kids.push(<circle key="ring" cx={cx} cy={cy} r={ts * 1.15} fill="none" stroke={C.ring} strokeDasharray="2 6" />);
    kids.push(<rect key="tile" x={cx - ts / 2} y={cy - ts / 2} width={ts} height={ts} rx={13} fill={C.tileBg} stroke={C.bgNode} />);
    kids.push(<Glyph key="g" cx={cx} cy={cy} size={ts * 0.55} color={C.accent} />);
    label("pl", cx, cy + ts * 1.15 + 18, spec.label, "middle");
  } else {
    const to = spec.kind === "fan" ? spec.to : [spec.to];
    const lit = spec.kind === "single" && !!spec.lit;
    const broken = spec.kind === "single" && !!spec.broken;
    const sel = spec.kind === "fan" ? spec.sel : undefined;
    const hub: [number, number] = [W * 0.27, H / 2];
    const n = to.length;
    const cr = 58;
    const L: [number, number][] = [[-30, -16], [-24, 20], [2, -32], [12, 28], [-40, 4], [28, -14], [-6, 8]].map(([x, y]) => [hub[0] + x, hub[1] + y]);
    kids.push(<circle key="rl" cx={hub[0]} cy={hub[1]} r={cr} fill="none" stroke={C.ring} strokeDasharray="2 6" />);
    label("lf", hub[0] - cr * 0.76, hub[1] - cr * 0.86, spec.from, "end");
    L.forEach((p, i) => kids.push(<line key={`s${i}`} x1={hub[0]} y1={hub[1]} x2={p[0]} y2={p[1]} stroke={C.edge} strokeWidth={1.25} />));
    [[0, 4], [1, 4], [2, 5], [3, 1], [6, 0], [6, 3]].forEach(([i, j], m) =>
      kids.push(<line key={`le${m}`} x1={L[i][0]} y1={L[i][1]} x2={L[j][0]} y2={L[j][1]} stroke={C.edge} strokeWidth={1.25} />),
    );
    const tx = n > 1 ? W * 0.62 : W * 0.73;
    const spread = n > 1 ? Math.min(H * 0.66, 70 * (n - 1) + 160) : 0;
    const targets = to.map((_, i) => [tx, n > 1 ? H / 2 - spread / 2 + (i * spread) / (n - 1) : H / 2] as [number, number]);
    targets.forEach((t, i) => {
      const single = n === 1;
      const end = [t[0] - (single ? 30 : 8), t[1]];
      const bend = (single ? 52 : 30) * (i % 2 ? -1 : 1);
      const d = `M${hub[0]} ${hub[1]} C ${hub[0] + (end[0] - hub[0]) * 0.38} ${hub[1] - bend - (t[1] - hub[1]) * 0.1}, ${hub[0] + (end[0] - hub[0]) * 0.62} ${end[1] + bend}, ${end[0]} ${end[1]}`;
      const active = sel === undefined || sel === -1 || sel === i;
      kids.push(
        <path key={`base${i}`} d={d} fill="none" stroke={broken ? C.amberLine : C.edge} strokeWidth={1.25} strokeDasharray={broken ? "4 6" : undefined} />,
      );
      if (broken) {
        kids.push(
          <circle
            key="stall"
            className="pw-anim"
            r={4}
            fill={C.amber}
            style={{ offsetPath: `path('${d}')`, offsetDistance: "48%", offsetRotate: "0deg", ...anim({ animation: "pwBlink 1.6s ease-in-out infinite" }) }}
          />,
        );
      } else if (lit) {
        kids.push(<path key={`lit${i}`} data-lit="" d={d} fill="none" stroke={C.accent} strokeWidth={1.6} strokeLinecap="round" />);
      } else if (active && !still) {
        // Under reduced motion the travelling stroke and its rider are dropped: their resting
        // frame is invisible anyway (dashoffset 1, opacity 0).
        kids.push(
          <path
            key={`hot${i}`}
            className="pw-anim"
            d={d}
            pathLength={1}
            fill="none"
            stroke={C.accent}
            strokeWidth={1.6}
            strokeLinecap="round"
            strokeDasharray="1 1"
            style={{ strokeDashoffset: 1, animation: `pwDraw ${DUR}s cubic-bezier(.45,0,.25,1) 0s infinite` }}
          />,
        );
        kids.push(
          <circle
            key={`ride${i}`}
            className="pw-anim"
            r={4}
            fill={C.accent}
            style={{ offsetPath: `path('${d}')`, offsetRotate: "0deg", opacity: 0, animation: `pwRide ${DUR}s cubic-bezier(.45,0,.25,1) 0s infinite` }}
          />,
        );
      }
      if (single) {
        const ts = 52;
        const calm = lit || broken;
        kids.push(
          <circle
            key="rr"
            className={calm ? undefined : "pw-anim"}
            cx={t[0]}
            cy={t[1]}
            r={cr}
            fill="none"
            stroke={lit ? C.accent : C.ring}
            strokeDasharray="2 6"
            style={{ transformOrigin: `${t[0]}px ${t[1]}px`, ...(calm ? {} : anim({ animation: `pwRot 40s linear infinite, pwLand ${DUR}s ease-out infinite` })) }}
          />,
        );
        [[-30, -26], [32, -22], [34, 26], [-28, 30]].forEach(([x, y], m) => {
          const p = [t[0] + x, t[1] + y];
          kids.push(<line key={`rs${m}`} x1={t[0]} y1={t[1]} x2={p[0]} y2={p[1]} stroke={C.edge} strokeWidth={1.25} />);
          kids.push(<circle key={`rn${m}`} cx={p[0]} cy={p[1]} r={3} fill={C.tileBg} stroke={lit ? C.accent : C.node} strokeWidth={1.5} />);
        });
        kids.push(
          <rect key="tile" x={t[0] - ts / 2} y={t[1] - ts / 2} width={ts} height={ts} rx={13} fill={C.tileBg} stroke={broken ? C.amberTile : C.bgNode} />,
        );
        kids.push(<Glyph key="g" cx={t[0]} cy={t[1]} size={ts * 0.6} color={broken ? C.muted : C.accent} />);
        label("rt", t[0] + cr * 0.76, t[1] - cr * 0.86, to[0], "start", lit ? C.accent : undefined);
      } else {
        const on = sel === i;
        kids.push(<circle key={`tn${i}`} data-on={on ? "" : undefined} cx={t[0]} cy={t[1]} r={5.5} fill={on ? C.accent : C.node} />);
        label(`tl${i}`, t[0] + 14, t[1] + 4, to[i], "start", on ? C.accent : undefined);
      }
    });
    L.forEach((p, i) =>
      kids.push(
        <circle
          key={`ln${i}`}
          className="pw-anim"
          cx={p[0]}
          cy={p[1]}
          r={3}
          fill={C.tileBg}
          stroke={lit ? C.accent : C.node}
          strokeWidth={1.5}
          style={!lit && !broken && i % 2 === 0 ? anim({ animation: `pwHot ${DUR}s ease-in-out ${(i * 0.12).toFixed(2)}s infinite` }) : undefined}
        />,
      ),
    );
    kids.push(
      <circle
        key="hub"
        className="pw-anim"
        cx={hub[0]}
        cy={hub[1]}
        r={5.5}
        fill={lit ? C.accent : C.node}
        style={!lit && !broken ? anim({ animation: `pwHub ${DUR}s ease-in-out infinite` }) : undefined}
      />,
    );
  }

  return (
    <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice" fill="none" className="block" data-testid="pw-hero">
      {kids}
    </svg>
  );
}
