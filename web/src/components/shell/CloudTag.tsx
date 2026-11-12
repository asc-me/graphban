import * as React from "react";

import { cn } from "@/lib/cn";
import { useSyncStatus } from "@/lib/queries";
import type { SyncStatus } from "@/lib/types";

export type CloudTagState = "synced" | "pending" | "unknown" | "refused";

export interface CloudTagModel {
  state: CloudTagState;
  label: string;
  tooltip: string;
}

function consoleName(url: string): string {
  if (!url) return "the console";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function clock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Shown when a mapped project's cloud tag is not known yet (GRPH-1562). Never the id. */
export const UNKNOWN_CLOUD_TAG = "?";
export const UNKNOWN_TAG_TOOLTIP = "Couldn't read the cloud project's tag";

/** The cloud project's tag for display, uppercased, or `?` when unknown — never its id. */
export function cloudTagText(tag: string | null | undefined): string {
  const t = (tag ?? "").trim();
  return t ? t.toUpperCase() : UNKNOWN_CLOUD_TAG;
}

/**
 * The tag a project carries, or null for a local-only one (no tag, no "local" badge).
 * `status` is the last answer read; `unreachable` says the latest read failed, so a mapped
 * project's tag is then unknown, never synced. With no answer at all nothing is known about
 * which projects sync, so no tag is drawn: `☁ ?` never lands on a local-only project.
 * Pure so the four states are testable without a render.
 */
export function cloudTagFor(
  projectId: string,
  status: SyncStatus | undefined,
  unreachable = false,
): CloudTagModel | null {
  if (!status) return null;
  const p = status.projects.find((x) => x.project_id === projectId);
  if (!p || !p.mapped) return null;
  if (unreachable) {
    const label = `☁ ${cloudTagText(p.cloud_project_tag)}`;
    return { state: "unknown", label, tooltip: `Couldn't reach ${consoleName(status.cloud_url)} · state unknown` };
  }
  const cloud = cloudTagText(p.cloud_project_tag);
  const label = `☁ ${cloud}`;
  const org = status.org || "the org";
  const result = status.state === "refused" ? "refused" : p.last_push_result;
  const known = cloud !== UNKNOWN_CLOUD_TAG;
  if (result === "refused") {
    if (!known) return { state: "refused", label, tooltip: UNKNOWN_TAG_TOOLTIP };
    return { state: "refused", label, tooltip: `${org} refused the last push · no access to ${cloud}` };
  }
  if (!known) {
    const state = result === "ok" && p.last_push_at ? "synced"
      : result === "unknown" || status.state === "unknown" ? "unknown" : "pending";
    return { state, label, tooltip: UNKNOWN_TAG_TOOLTIP };
  }
  if (result === "ok" && p.last_push_at) {
    return { state: "synced", label, tooltip: `Syncs to ${cloud} in ${org} · last push ${clock(p.last_push_at)}` };
  }
  if (result === "unknown" || status.state === "unknown") {
    return { state: "unknown", label, tooltip: `Couldn't reach ${consoleName(status.cloud_url)} · state unknown` };
  }
  return { state: "pending", label, tooltip: `Linked to ${cloud} in ${org} · not pushed yet` };
}

const TONE: Record<CloudTagState, { text: string; dot: string }> = {
  synced: { text: "text-fg-2", dot: "bg-[#5fd07a]" },
  // Pending is a hollow ring with a dashed border (Org Link.dc.html, the A6 tag states).
  pending: { text: "text-faint", dot: "border border-dashed border-muted bg-transparent" },
  unknown: { text: "text-faint", dot: "bg-muted" },
  refused: { text: "text-st-blocked", dot: "bg-st-blocked" },
};

export function CloudTagView({ model, className }: { model: CloudTagModel; className?: string }) {
  const tone = TONE[model.state];
  const tipId = React.useId();
  // The one-line tooltip shows on keyboard focus as well as on hover.
  const [open, setOpen] = React.useState(false);
  return (
    <span className="relative inline-flex flex-none">
      <span
        data-testid="cloud-tag"
        data-state={model.state}
        data-tooltip={model.tooltip}
        tabIndex={0}
        aria-describedby={tipId}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className={cn(
          "inline-flex items-center gap-1 rounded-[3px] font-mono text-[10px] uppercase outline-none focus-visible:ring-1 focus-visible:ring-accent",
          tone.text,
          className,
        )}
      >
        <span aria-hidden data-dot className={cn("box-border h-1.5 w-1.5 rounded-full", tone.dot)} />
        {model.label}
      </span>
      <span
        id={tipId}
        role="tooltip"
        hidden={!open}
        className="pointer-events-none absolute left-0 top-full z-50 mt-1 whitespace-nowrap rounded-[6px] border border-line-2 bg-surface-3 px-2 py-1 font-sans text-small normal-case text-fg-2 shadow-lg"
      >
        {model.tooltip}
      </span>
    </span>
  );
}

/** Read-only: it shows the last result and offers no action, so a non-admin sees the same tag. */
export function CloudTag({ projectId, className }: { projectId: string; className?: string }) {
  // A failed refetch keeps the last answer (react-query holds `data`), so mapped projects
  // turn unknown and local-only ones stay bare.
  const { data, isError, isLoading } = useSyncStatus();
  if (isLoading) return null;
  const model = cloudTagFor(projectId, data, isError);
  return model ? <CloudTagView model={model} className={className} /> : null;
}
