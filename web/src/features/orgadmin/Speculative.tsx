import * as React from "react";

/**
 * The gate for screens that are designed but not backed (PRD-21 D9).
 *
 * D9's rule is "design once, ship no promises": a shipped build renders neither the nav
 * item nor the route until the backing PRD lands, because two permanently-greyed entries
 * make the plane read as chrome rather than as a product. So these screens exist in the
 * codebase — reviewable, and the spec for their PRD — behind a build flag that is off in
 * every normal build.
 *
 * Turn them on with `VITE_SPECULATIVE=1 pnpm dev`.
 */
export const SPECULATIVE_ENABLED = import.meta.env.VITE_SPECULATIVE === "1";

/**
 * The header every speculative screen wears.
 *
 * `blocker` is not decoration: a mockup whose missing capability is unnamed becomes a
 * spec by default, and somebody eventually wires it to nothing. Naming what is absent is
 * what keeps it a question instead of a promise.
 */
export function SpeculativeHeader({
  title,
  blocker,
  children,
}: {
  title: string;
  blocker: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="flex flex-wrap items-center gap-2.5">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <span
          title={blocker}
          className="rounded-full border border-purple/30 bg-purple/[0.07] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.06em] text-purple"
        >
          Speculative — not backed
        </span>
      </div>
      <p className="mt-2 max-w-[74ch] text-[12px] leading-relaxed text-purple-2/70">{blocker}</p>
      {children && (
        <p className="mt-2 max-w-[74ch] text-[12.5px] leading-relaxed text-muted">{children}</p>
      )}
    </div>
  );
}

/** What a speculative route renders in a normal build: nothing that implies a feature. */
export function NotAvailable() {
  return (
    <div className="max-w-[1180px] px-6 py-8 text-[13px] text-muted">This area is not available.</div>
  );
}

/** Wraps a control that is drawn but wired to nothing. */
export function Inert({ children, why }: { children: React.ReactNode; why: string }) {
  return (
    <div
      title={why}
      aria-disabled
      className="pointer-events-none select-none opacity-60 [&_button]:cursor-not-allowed [&_input]:cursor-not-allowed"
    >
      {children}
    </div>
  );
}
