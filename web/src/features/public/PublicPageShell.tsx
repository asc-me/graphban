import type { ReactNode } from "react";

/**
 * Public chrome for unauthenticated pages (boards, tracking, roadmap).
 * Deliberately NOT the signed-in shell — these are reachable by anyone with the link.
 */
export function PublicPageShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-surface text-fg">
      <header className="border-b border-line px-6 py-3">
        <div className="flex items-center gap-2.5">
          <div
            className="flex h-7 w-7 items-center justify-center rounded-[7px]"
            style={{ background: "linear-gradient(150deg,#c6f24e,#8fd12e)" }}
          >
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
              <path d="M3 2v12M3 4h8M3 8h6M3 12h9" stroke="#0a0c0e" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </div>
          <div>
            <div className="text-[14px] font-bold tracking-tight">Graphban</div>
            <div className="font-mono text-[9px] tracking-[0.5px] text-faint">PUBLIC</div>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-8">{children}</main>
    </div>
  );
}
