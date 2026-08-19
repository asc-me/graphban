import { Inert, NotAvailable, SPECULATIVE_ENABLED, SpeculativeHeader } from "./Speculative";

/**
 * Screens D + E — Branding, and the custom domain that hangs off it. Speculative.
 *
 * Two things worth keeping when this is built for real:
 *
 * 1. **Extraction proposes; it never applies.** A palette that changed itself when
 *    somebody uploaded a logo is a surprise, not a feature.
 * 2. **A candidate colour carries its contrast ratio.** An accent lifted from a logo
 *    usually fails against the canvas, and the screen should catch that rather than
 *    shipping an unreadable UI and letting the customer discover it.
 *
 * The custom domain is drawn as four states because the states *are* the design — and
 * because the screen is the small half. Behind it sit DNS the customer controls, a
 * certificate somebody issues and renews, a proxy that routes by Host, and tenant
 * resolution that today comes from the session and never from the hostname.
 */
export function OrgBranding() {
  if (!SPECULATIVE_ENABLED) return <NotAvailable />;
  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <SpeculativeHeader
        title="Branding & theme"
        blocker="`Organization` has no logo, colour or domain columns. Byte storage has a precedent — `Attachment` keeps image bytes in the DB served by unguessable id — but nothing is wired to an org."
      >
        A logo, a palette, and the hostname the console answers on.
      </SpeculativeHeader>

      <Inert why="Drawn to be reviewed. No column stores any of this yet.">
        <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
          <Card title="1 · Logo">
            <div className="flex h-[104px] items-center justify-center rounded-[10px] border border-dashed border-line-2 bg-surface text-[12px] text-faint">
              Drop a PNG or SVG — transparent reads best on the dark canvas
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">
              Previewed at both sizes it actually appears in — 20px in the rail, 40px on the
              sign-in card. A logo that works at one and dies at the other is the normal
              outcome, so the screen shows both rather than one flattering crop.
            </p>
          </Card>

          <Card title="2 · Palette">
            <div className="flex flex-wrap gap-2">
              {[
                ["accent", "#c6f24e", "8.9"],
                ["done", "#5fd07a", "7.4"],
                ["review", "#e0b34a", "8.1"],
                ["next", "#7ca2ff", "5.9"],
                ["blocked", "#ff6b6b", "4.6"],
              ].map(([name, hex, ratio]) => (
                <span
                  key={name}
                  className="inline-flex items-center gap-2 rounded-[7px] border border-line px-2 py-1"
                >
                  <span className="h-3.5 w-3.5 rounded" style={{ background: hex }} />
                  <span className="font-mono text-[10.5px] text-fg-2">{name}</span>
                  <span className="font-mono text-[9px] text-faint">{hex}</span>
                  <span className="font-mono text-[9px] text-st-done">{ratio}:1</span>
                </span>
              ))}
            </div>
            <div className="mt-3 rounded-[9px] border border-st-blocked/25 bg-st-blocked/[0.06] px-2.5 py-2">
              <div className="font-mono text-[9px] uppercase tracking-[0.06em] text-st-blocked">
                candidate rejected
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-muted">
                <span className="font-mono text-st-blocked">#3b4a2e</span> was pulled from the
                logo but reaches 1.9:1 against the canvas. It cannot be accepted as the accent,
                and the reason is stated rather than left as a red border.
              </p>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">
              Extraction proposes; you accept. Nothing changes because a logo was uploaded.
            </p>
          </Card>
        </div>

        <Card title="3 · Custom domain" className="mt-4">
          <div className="grid gap-2 md:grid-cols-4">
            {[
              ["1 · NONE", "You are on cloud.graphban.dev. Add a hostname to start.", "text-faint"],
              ["2 · PENDING DNS", "Create the records shown, then verify. The last failure says which record was missing, not just “failed”.", "text-st-review"],
              ["3 · ISSUING CERT", "DNS verified, TLS not yet. Minutes, and neither an error nor success.", "text-st-next"],
              ["4 · LIVE", "Serving, with its certificate expiry. Removing it breaks every bookmark and agent config pointing at it.", "text-st-done"],
            ].map(([label, body, tone]) => (
              <div key={label} className="rounded-[10px] border border-line bg-surface px-3 py-2.5">
                <div className={`font-mono text-[9px] uppercase tracking-[0.06em] ${tone}`}>
                  {label}
                </div>
                <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{body}</p>
              </div>
            ))}
          </div>
          <p className="mt-3 text-[11.5px] leading-relaxed text-muted">
            The default hostname never stops working — a custom domain is added, not swapped.
            That is the question every customer asks second, so the screen answers it without
            being asked.
          </p>
        </Card>
      </Inert>
    </div>
  );
}

function Card({
  title,
  children,
  className = "",
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-[13px] border border-line bg-surface-2 p-4 ${className}`}>
      <h3 className="mb-3 text-[14px] font-semibold">{title}</h3>
      {children}
    </section>
  );
}
