import { cn } from "@/lib/cn";

import { ROLE_TONE } from "./liveUtils";

export function RoleCounts({ byRole, roles }: { byRole: Record<string, number>; roles: string[] }) {
  const shown = [...roles, "all-in-one"].filter((r) => byRole[r]);
  if (shown.length === 0) return null;
  return (
    <span className="flex items-center gap-1.5">
      {shown.map((r) => (
        <span
          key={r}
          className={cn(
            "rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
            ROLE_TONE[r] ?? "text-muted border-line-2",
          )}
        >
          {byRole[r]} {r}
        </span>
      ))}
    </span>
  );
}
