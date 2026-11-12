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

/**
 * The tag a project carries, or null for a local-only one (no tag, no "local" badge).
 * `status` undefined means the read failed: every tag is then unknown, never synced.
 * Pure so the four states are testable without a render.
 */
export function cloudTagFor(projectId: string, status: SyncStatus | undefined): CloudTagModel | null {
  if (!status) {
    return { state: "unknown", label: "☁ ?", tooltip: "Couldn't reach the console · state unknown" };
  }
  const p = status.projects.find((x) => x.project_id === projectId);
  if (!p || !p.mapped) return null;
  const cloud = (p.cloud_project_id ?? "").toUpperCase();
  const label = `☁ ${cloud}`;
  const org = status.org || "the org";
  const result = status.state === "refused" ? "refused" : p.last_push_result;
  if (result === "refused") {
    return { state: "refused", label, tooltip: `${org} refused the last push · no access to ${cloud}` };
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
  pending: { text: "text-faint", dot: "bg-faint-2" },
  unknown: { text: "text-faint", dot: "bg-muted" },
  refused: { text: "text-st-blocked", dot: "bg-st-blocked" },
};

export function CloudTagView({ model, className }: { model: CloudTagModel; className?: string }) {
  const tone = TONE[model.state];
  return (
    <span
      data-testid="cloud-tag"
      data-state={model.state}
      title={model.tooltip}
      aria-label={model.tooltip}
      className={cn("inline-flex flex-none items-center gap-1 font-mono text-[10px] uppercase", tone.text, className)}
    >
      <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", tone.dot)} />
      {model.label}
    </span>
  );
}

/** Read-only: it shows the last result and offers no action, so a non-admin sees the same tag. */
export function CloudTag({ projectId, className }: { projectId: string; className?: string }) {
  const { data, isError, isLoading } = useSyncStatus();
  if (isLoading) return null;
  const model = cloudTagFor(projectId, isError ? undefined : data);
  return model ? <CloudTagView model={model} className={className} /> : null;
}
