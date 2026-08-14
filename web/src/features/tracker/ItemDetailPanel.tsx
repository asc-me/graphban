import { BadgeCheck, ExternalLink, FlaskConical, GitPullRequest, X } from "lucide-react";

import { Avatar } from "@/components/ui/avatar";
import { AssistantPanel } from "@/features/assistant/AssistantPanel";
import { LinkedCode } from "@/features/code/LinkedCode";
import { useProjectCtx } from "@/features/ProjectContext";
import { CHECK_COLOR, PR_STATE_COLOR } from "@/lib/meta";
import { useItems, useLinks, useShards, useUpdateItem } from "@/lib/queries";
import type { Fidelity, Item, Status } from "@/lib/types";

import { StatusMenu } from "./StatusMenu";

export function ItemDetailPanel({
  item,
  onClose,
  onStatus,
}: {
  item: Item;
  onClose: () => void;
  onStatus: (s: Status) => void;
}) {
  const { activeId } = useProjectCtx();
  const { data: shards = [] } = useShards(activeId);
  const { data: links = [] } = useLinks(activeId);
  const { data: allItems = [] } = useItems(activeId);
  const updateItem = useUpdateItem();
  const linked = shards.filter((s) => s.item_id === item.id);
  const setFidelity = (f: Fidelity) => updateItem.mutate({ id: item.id, body: { fidelity: f } });

  const statusOf = (id: string) => allItems.find((i) => i.id === id)?.status;
  const deps = links.filter((l) => l.type === "dependency" && l.a === item.id).map((l) => l.b);
  const dependents = links.filter((l) => l.type === "dependency" && l.b === item.id).map((l) => l.a);
  const blockedBy = deps.filter((id) => statusOf(id) !== "done");

  return (
    <>
      <div className="absolute inset-0 z-20 bg-black/30" onClick={onClose} />
      <div className="absolute right-0 top-0 z-30 flex h-full w-[440px] max-w-full animate-fade flex-col border-l border-line-hover bg-surface-3 shadow-[-24px_0_60px_rgba(0,0,0,0.4)]">
        <div className="flex flex-none items-center justify-between border-b border-line px-5 py-3.5">
          <span className="font-mono text-[11px] text-faint">{item.id}</span>
          <button onClick={onClose} className="text-faint hover:text-fg">
            <X size={16} />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5">
          <div className="flex items-start gap-3">
            <StatusMenu status={item.status} onChange={onStatus} />
          </div>

          <h2 className="text-[17px] font-semibold leading-snug text-fg">{item.title}</h2>

          {item.prd_id && (
            <div className="font-mono text-[11px] text-purple-2">
              Implements {item.prd_id}
              {item.prd_section ? ` · ${item.prd_section}` : ""}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-1.5">
            {item.tags.map((t) => (
              <span
                key={t}
                className="rounded-md border border-line-2 bg-surface-2 px-2 py-0.5 font-mono text-[10px] text-muted"
              >
                {t}
              </span>
            ))}
            {item.effort > 0 && (
              <span className="rounded-md bg-surface-4 px-2 py-0.5 font-mono text-[10px] text-muted-2">
                {item.effort} pts
              </span>
            )}
          </div>

          {/* Fidelity (AL-68): high = needs a prototype before it can be specced in words. */}
          <div className="flex items-center gap-2">
            <FlaskConical size={13} className="text-faint" />
            <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Fidelity</span>
            <div className="flex items-center gap-1 rounded-lg border border-line-2 bg-surface-2 p-0.5">
              {(["low", "high"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFidelity(f)}
                  className={
                    "rounded-md px-2 py-0.5 font-mono text-[10.5px] transition-colors " +
                    (item.fidelity === f
                      ? f === "high"
                        ? "bg-[rgba(224,179,74,0.14)] text-[#e0b34a]"
                        : "bg-surface-4 text-fg"
                      : "text-muted hover:text-fg-2")
                  }
                >
                  {f}
                </button>
              ))}
            </div>
            {item.fidelity === "high" && (
              <span className="text-[11px] text-[#e0b34a]">needs a prototype</span>
            )}
          </div>

          {item.blocker && (
            <div className="rounded-[10px] border border-[rgba(255,107,107,0.25)] bg-[rgba(255,107,107,0.06)] px-3 py-2.5 text-[12.5px] text-st-blocked">
              <span className="font-mono text-[10px] uppercase tracking-wide">Blocked · </span>
              {item.blocker}
            </div>
          )}

          {item.bounce_reason && (
            <div className="rounded-[10px] border border-[rgba(224,179,74,0.3)] bg-[rgba(224,179,74,0.07)] px-3 py-2.5 text-[12.5px] text-[#e0b34a]">
              <span className="font-mono text-[10px] uppercase tracking-wide">Bounced · </span>
              {item.bounce_reason}
            </div>
          )}

          <Section label="Description">
            <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-fg-2">
              {item.description || "No description."}
            </p>
          </Section>

          {(item.touchpoints?.length > 0 || item.claimed_by) && (
            <Section label="Code neighborhood">
              {item.claimed_by && (
                <div className="mb-2 flex items-center gap-1.5 font-mono text-[11px] text-accent">
                  <span className="blink h-1.5 w-1.5 rounded-full bg-accent" />
                  claimed by {item.claimed_by}
                </div>
              )}
              {item.touchpoints?.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {item.touchpoints.map((tp) => (
                    <span
                      key={tp}
                      className="rounded-md border border-line-2 bg-surface-2 px-1.5 py-0.5 font-mono text-[10.5px] text-muted-2"
                    >
                      {tp}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-[12px] text-faint">No touchpoints declared.</p>
              )}
            </Section>
          )}

          <Section label="Linked code">
            <LinkedCode refId={item.id} projectId={activeId} />
          </Section>

          {(deps.length > 0 || dependents.length > 0) && (
            <Section label="Dependencies">
              {blockedBy.length > 0 && (
                <div className="mb-2 flex flex-wrap items-center gap-1.5 text-[12px] text-st-blocked">
                  <span className="font-mono text-[10px] uppercase tracking-wide">Blocked by ·</span>
                  {blockedBy.map((id) => (
                    <span key={id} className="rounded border border-[rgba(255,107,107,0.3)] px-1.5 py-px font-mono text-[10px]">
                      {id}
                    </span>
                  ))}
                </div>
              )}
              {deps.length > blockedBy.length && (
                <p className="mb-1.5 text-[11px] text-faint">
                  Depends on {deps.length} item{deps.length > 1 ? "s" : ""} · {deps.length - blockedBy.length} done.
                </p>
              )}
              {dependents.length > 0 && (
                <p className="text-[12px] text-accent">
                  Unblocks {dependents.length} item{dependents.length > 1 ? "s" : ""} when done.
                </p>
              )}
            </Section>
          )}

          {item.pr && (
            <Section label="Pull request">
              <div className="rounded-[11px] border border-line-2 bg-surface-2 p-3">
                <div className="flex items-center gap-2">
                  <GitPullRequest size={14} style={{ color: PR_STATE_COLOR[item.pr.state] }} />
                  <span className="font-mono text-[11px]" style={{ color: PR_STATE_COLOR[item.pr.state] }}>
                    #{item.pr.number}
                  </span>
                  <span className="truncate text-[12.5px] text-fg-2">{item.pr.title}</span>
                </div>
                <div className="mt-2 flex items-center gap-3 font-mono text-[10.5px] text-faint">
                  <span className="text-st-done">+{item.pr.additions}</span>
                  <span className="text-st-blocked">−{item.pr.deletions}</span>
                  <span className="flex items-center gap-1">
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: CHECK_COLOR[item.pr.checks] }} />
                    {item.pr.checks}
                  </span>
                  <span className="ml-auto">{item.pr.ago}</span>
                </div>
              </div>
            </Section>
          )}

          {item.evidence?.length > 0 && (
            <Section label="Proof of done">
              <div className="space-y-2">
                {item.evidence.map((e, i) => (
                  <div key={i} className="rounded-[10px] border border-[rgba(95,208,122,0.25)] bg-[rgba(95,208,122,0.05)] p-2.5">
                    <div className="mb-1 flex items-center gap-1.5">
                      <BadgeCheck size={12} className="text-st-done" />
                      <span className="font-mono text-[9px] uppercase tracking-wide text-st-done">{e.kind}</span>
                    </div>
                    {e.detail && <p className="text-[12px] leading-relaxed text-fg-2">{e.detail}</p>}
                    {e.url && (
                      <a
                        href={e.url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 font-mono text-[10.5px] text-accent hover:underline"
                      >
                        <ExternalLink size={10} /> {e.url}
                      </a>
                    )}
                  </div>
                ))}
              </div>
            </Section>
          )}

          <Section label={`Linked memory · ${linked.length}`}>
            {linked.length === 0 ? (
              <p className="text-[12.5px] text-faint">No shards linked to this item.</p>
            ) : (
              <div className="space-y-2">
                {linked.map((s) => (
                  <div key={s.id} className="rounded-[10px] border border-line-2 bg-surface-2 p-2.5">
                    <div className="mb-1 font-mono text-[10px] text-faint">{s.source}</div>
                    <p className="text-[12px] leading-relaxed text-fg-2">{s.text}</p>
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section label="Assistant">
            <div className="h-[420px] rounded-[11px] border border-line-2 bg-surface-2 p-2.5">
              <AssistantPanel entityType="item" entityId={item.id} projectId={item.project_id} entityTags={item.tags} />
            </div>
          </Section>

          {item.reporter?.name && (
            <div className="flex items-center gap-2.5 border-t border-line pt-4">
              <Avatar
                initials={item.reporter.name.split(" ").map((p) => p[0]).slice(0, 2).join("")}
                color={item.reporter.avatar ?? "#a78bfa"}
                size={26}
              />
              <div className="leading-tight">
                <div className="text-[12.5px] text-fg-2">{item.reporter.name}</div>
                <div className="font-mono text-[10px] text-faint">@{item.reporter.handle}</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">{label}</div>
      {children}
    </div>
  );
}
