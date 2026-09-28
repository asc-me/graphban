import { Link } from "react-router-dom";

import { cn } from "@/lib/cn";
import type { LiveBoard } from "@/lib/types";

function Chip({
  to, active, label, count,
}: { to: string; active: boolean; label: string; count: number }) {
  return (
    <Link
      to={to}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors",
        active
          ? "border-line-hover bg-surface-3 text-fg"
          : "border-line-2 bg-surface-2 text-muted hover:border-line-3 hover:text-fg-2",
      )}
    >
      {label}
      <span className="font-mono text-[10px] text-faint">{count}</span>
    </Link>
  );
}

export function CensusChips({
  board,
  pathname,
  user,
  loading,
}: {
  board: LiveBoard | undefined;
  pathname: string;
  user: string | null;
  loading: boolean;
}) {
  const censusTotal = board?.user_counts.reduce((n, c) => n + c.total, 0) ?? 0;

  return (
    <div className="flex flex-none flex-wrap items-center gap-1.5 border-b border-line px-5 py-2.5">
      {loading || !board ? (
        <>
          <div className="h-7 w-16 animate-pulse rounded-lg bg-surface-3" />
          <div className="h-7 w-20 animate-pulse rounded-lg bg-surface-3" />
        </>
      ) : (
        <>
          <Chip to={pathname} active={!user} label="All" count={censusTotal} />
          {board.user_counts.map((c) => {
            const id = c.user_id ?? "unattributed";
            return (
              <Chip
                key={id}
                to={`${pathname}?user=${encodeURIComponent(id)}`}
                active={user === id}
                label={c.label}
                count={c.total}
              />
            );
          })}
        </>
      )}
    </div>
  );
}
