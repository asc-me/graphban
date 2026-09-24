import { Check, ChevronDown } from "lucide-react";

import { Dot } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { STATUS_META, STATUS_ORDER } from "@/lib/meta";
import type { Status } from "@/lib/types";

export function StatusMenu({
  status,
  onChange,
  compact = false,
}: {
  status: Status;
  onChange: (s: Status) => void;
  compact?: boolean;
}) {
  const meta = STATUS_META[status];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          onClick={(e) => e.stopPropagation()}
          aria-label={`Status: ${meta.label}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-transparent px-1.5 py-1 transition-colors hover:border-line-2 hover:bg-surface-3"
        >
          <Dot color={meta.color} glow={status === "in_progress"} />
          {!compact && (
            <span className="font-mono text-[10.5px] uppercase tracking-wide" style={{ color: meta.color }}>
              {meta.label}
            </span>
          )}
          <ChevronDown size={11} className="text-faint" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[160px]">
        {STATUS_ORDER.map((s) => (
          <DropdownMenuItem
            key={s}
            onSelect={() => onChange(s)}
            className="justify-start"
          >
            <Dot color={STATUS_META[s].color} />
            <span className="font-mono text-[11px] uppercase tracking-wide" style={{ color: STATUS_META[s].color }}>
              {STATUS_META[s].label}
            </span>
            {s === status && <Check size={12} className="ml-auto text-fg" aria-hidden />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
