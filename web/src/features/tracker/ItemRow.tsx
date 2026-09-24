import { ArrowDown, ArrowUp, Github, GitPullRequest, GripVertical, MoreHorizontal } from "lucide-react";
import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/cn";
import { CHECK_COLOR, PR_STATE_COLOR } from "@/lib/meta";
import type { Item, Status } from "@/lib/types";

import { StatusMenu } from "./StatusMenu";

/** "#42" for an issue/PR URL, else the hostname. */
function ghRef(url: string): string {
  const m = url.match(/\/(?:issues|pull)\/(\d+)/);
  if (m) return `#${m[1]}`;
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "link";
  }
}

export function ItemRow({
  item,
  selected,
  onSelect,
  onStatus,
  onMoveUp,
  onMoveDown,
  canMoveUp,
  canMoveDown,
  dragHandlers,
  dragging,
  dragOver,
}: {
  item: Item;
  selected: boolean;
  onSelect: () => void;
  onStatus: (s: Status) => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
  dragHandlers: {
    onDragStart: (e: React.DragEvent) => void;
    onDragEnter: (e: React.DragEvent) => void;
    onDragEnd: (e: React.DragEvent) => void;
    onDragOver: (e: React.DragEvent) => void;
  };
  dragging: boolean;
  dragOver: boolean;
}) {
  return (
    <div
      draggable
      {...dragHandlers}
      onClick={onSelect}
      aria-selected={selected}
      className={cn(
        "group relative flex cursor-pointer items-center gap-3 border-b border-line/70 px-4 py-3 transition-colors",
        selected
          ? "border-l-2 border-l-accent bg-surface-3 pl-[calc(1rem-2px)]"
          : "border-l-2 border-l-transparent hover:bg-surface/60",
        dragging && "opacity-40",
        dragOver && "border-t-2 border-t-accent",
      )}
    >
      <GripVertical
        size={14}
        className="flex-none text-faint-2 opacity-0 transition-opacity group-hover:opacity-100"
      />

      <div className="flex-none" onClick={(e) => e.stopPropagation()}>
        <StatusMenu status={item.status} onChange={onStatus} compact />
      </div>

      <span className="w-[52px] flex-none font-mono text-[11px] text-faint">{item.id}</span>

      <span className="min-w-0 flex-1 truncate text-[13.5px] text-fg-2">{item.title}</span>

      {item.fidelity === "high" && (
        <span
          className="flex-none rounded-md border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-[#e0b34a]"
          title="High fidelity — needs a prototype before it can be specced"
        >
          proto
        </span>
      )}

      <div className="flex flex-none items-center gap-1.5">
        {item.tags.slice(0, 3).map((t) => (
          <span
            key={t}
            className="rounded-md border border-line-2 bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-muted"
          >
            {t}
          </span>
        ))}
      </div>

      {item.effort > 0 && (
        <span
          className="flex-none rounded-md bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted-2"
          title="effort"
        >
          {item.effort}
        </span>
      )}

      {item.pr && (
        <span
          className="flex flex-none items-center gap-1 font-mono text-[10px]"
          style={{ color: PR_STATE_COLOR[item.pr.state] ?? "#8b949e" }}
          title={`PR #${item.pr.number} · ${item.pr.state} · checks ${item.pr.checks}`}
        >
          <GitPullRequest size={11} />#{item.pr.number}
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ background: CHECK_COLOR[item.pr.checks] ?? "#8b949e" }}
          />
        </span>
      )}

      {item.github_url && !item.pr && (
        <a
          href={item.github_url}
          target="_blank"
          rel="noreferrer noopener"
          onClick={(e) => e.stopPropagation()}
          className="flex flex-none items-center gap-1 font-mono text-[10px] text-muted hover:text-fg"
          title={`Linked: ${item.github_url}`}
        >
          <Github size={11} />
          {ghRef(item.github_url)}
        </a>
      )}

      {item.claimed_by ? (
        <span
          className="flex flex-none items-center gap-1 rounded-md border border-[rgba(198,242,78,0.3)] bg-[rgba(198,242,78,0.06)] px-1.5 py-0.5 font-mono text-[9.5px] text-accent"
          title={`Claimed by ${item.claimed_by}`}
        >
          <span className="blink h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="max-w-[80px] truncate">{item.claimed_by}</span>
        </span>
      ) : item.assignee ? (
        <span
          className="flex-none rounded-md border border-line-2 px-1.5 py-0.5 font-mono text-[9.5px] text-muted"
          title={`Assigned to ${item.assignee}`}
        >
          {item.assignee}
        </span>
      ) : null}

      <span className="w-[52px] flex-none text-right font-mono text-[10px] text-faint-2">
        {item.date}
      </span>

      {item.reporter?.avatar && (
        <Avatar
          initials={(item.reporter.name ?? "?").split(" ").map((p) => p[0]).slice(0, 2).join("")}
          color={item.reporter.avatar}
          size={22}
        />
      )}

      <div className="flex-none opacity-0 transition-opacity group-hover:opacity-100" onClick={(e) => e.stopPropagation()}>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label={`Actions for ${item.id}`}
              className="flex size-7 items-center justify-center rounded-md text-faint hover:bg-surface-3 hover:text-fg"
            >
              <MoreHorizontal size={14} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={!canMoveUp} onSelect={onMoveUp}>
              <ArrowUp size={14} className="text-muted" />
              Move up
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!canMoveDown} onSelect={onMoveDown}>
              <ArrowDown size={14} className="text-muted" />
              Move down
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
