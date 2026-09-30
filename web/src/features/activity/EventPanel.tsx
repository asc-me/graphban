import { X } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";
import type { Event } from "@/lib/types";

import { VerdictChip } from "./EventRow";
import { absTime, relTime, summarizeMeta, verdictOf } from "./model";

/**
 * The event panel (PRD-47 S10): the actor chain, the record's own metadata, and the two
 * pivots the data can answer.
 *
 * The design's third pivot (copy trace id) and its field-level diff are NOT here, and
 * the space they would occupy is a labelled seam rather than a blank — see
 * {@link NotRecorded}. An empty area where a diff should be reads as "nothing changed";
 * an empty area where a trace id should be reads as "no trace". Both are claims the
 * record cannot support (GRPH-979).
 */
export function EventPanel({
  event: e,
  refusals,
  onClose,
  onPivotTarget,
  onPivotActor,
}: {
  event: Event;
  refusals: string[] | null;
  onClose: () => void;
  onPivotTarget: () => void;
  onPivotActor: () => void;
}) {
  const chain = actorChain(e);
  return (
    <aside
      className="flex h-full min-h-0 w-[22rem] flex-none flex-col border-l border-line bg-surface-2"
      aria-label={`Event ${e.id}`}
    >
      <div className="flex flex-none items-start gap-2 border-b border-line px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-body text-accent">{e.action}</p>
          <p className="mt-0.5 font-mono text-[10.5px] text-faint">event {e.id}</p>
        </div>
        <VerdictChip verdict={verdictOf(e, refusals)} />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close event panel"
          className="flex-none rounded-md p-1 text-faint transition-colors hover:bg-surface-3 hover:text-fg"
        >
          <X size={14} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <Section title="Actor chain">
          <ol className="flex flex-col gap-1.5 border-l border-line-2 pl-2.5">
            {chain.map((link) => (
              <li key={link.slot} className="flex items-start gap-2">
                <span className="w-12 flex-none pt-px text-small text-faint">{link.slot}</span>
                {link.value ? (
                  <span className="min-w-0 flex-1 font-mono text-[12px] text-fg">{link.value}</span>
                ) : (
                  // Named, not omitted: a chain that skipped the key slot would look like
                  // a two-link chain that was always two links.
                  <span className="min-w-0 flex-1 text-[11.5px] leading-snug text-faint-2">
                    {link.absent}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </Section>

        <Section title="Record">
          <dl className="flex flex-col gap-1">
            <Field label="Time" value={`${absTime(e.ts)} · ${relTime(e.ts) || "no relative time"}`} />
            <Field label="Surface" value={e.surface} mono />
            <Field label="Actor type" value={e.actor_type} mono />
            <Field label="Project" value={e.project_id ?? "none recorded"} mono />
            <Field
              label="Object"
              value={e.target_id ? `${e.target_type || "untyped"} · ${e.target_id}` : e.target_type || "no target recorded"}
              mono
            />
          </dl>
        </Section>

        <Section title="Pivot">
          <div className="flex flex-col gap-1.5">
            <PivotButton
              onClick={onPivotTarget}
              disabled={!e.target_id}
              title={
                e.target_id
                  ? undefined
                  : "This event recorded no target id — most MCP writes do not — so there is nothing to pivot on."
              }
            >
              All events on this target
            </PivotButton>
            <PivotButton
              onClick={onPivotActor}
              disabled={!e.actor_id}
              title={
                e.actor_id
                  ? undefined
                  : "This event recorded no actor id, so there is nothing to pivot on."
              }
            >
              Everything by this actor
            </PivotButton>
            <p className="text-small leading-snug text-faint-2">
              Two of the design&apos;s three pivots. The third — copy trace id — needs a
              trace id on the record, and there is none.
            </p>
          </div>
        </Section>

        <Section title="Not on the record">
          <div className="flex flex-col gap-2">
            <NotRecorded
              label="Field-level diff"
              detail="The event record has no diff column and nothing writes one, so this panel can say that a mutation was accepted and by whom, but not which fields changed."
            />
            <NotRecorded
              label="Trace id"
              detail="No trace id is stored, so there is nothing to copy and no trace to pivot to."
            />
            <p className="text-small leading-snug text-faint-2">
              Both are GRPH-979, deferred with the measurements rather than drawn as a
              plausible blank here.
            </p>
          </div>
        </Section>

        <Section title="Summary">
          {summarizeMeta(e.meta) ? (
            <p className="break-words font-mono text-small leading-relaxed text-muted">
              {summarizeMeta(e.meta)}
            </p>
          ) : (
            <p className="text-[11.5px] text-faint-2">
              This event carried no metadata beyond the columns above.
            </p>
          )}
        </Section>
      </div>
    </aside>
  );
}

interface ChainLink {
  slot: string;
  value: string | null;
  absent: string;
}

/**
 * person → key → agent, from the columns and the meta the record actually carries.
 * Every slot is present whether or not it has a value, and the absence says which of the
 * two it is: not applicable to this kind of actor, or applicable and not recorded.
 */
function actorChain(e: Event): ChainLink[] {
  const meta = (e.meta ?? {}) as Record<string, unknown>;
  const metaAgent = typeof meta.agent_id === "string" && meta.agent_id ? meta.agent_id : null;

  if (e.actor_type === "apikey") {
    return [
      {
        slot: "person",
        value: e.principal || null,
        absent: "not recorded — this key had no owner captured when it acted",
      },
      { slot: "key", value: e.actor_label || e.actor_id || null, absent: "not recorded" },
      {
        slot: "agent",
        value: metaAgent,
        absent: "not recorded — agents share a credential by design, so the key alone cannot say which one",
      },
    ];
  }
  if (e.actor_type === "system") {
    return [
      { slot: "person", value: null, absent: "none — recorded by the system, not by a person" },
      { slot: "key", value: null, absent: "none — no credential was used" },
      { slot: "agent", value: e.actor_label || null, absent: "not recorded" },
    ];
  }
  return [
    { slot: "person", value: e.actor_label || e.actor_id || null, absent: "not recorded" },
    { slot: "key", value: null, absent: "none — a person acted directly" },
    {
      slot: "agent",
      value: e.agent || null,
      absent: "none — no assistant was involved",
    },
  ];
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-4">
      <h3 className="mb-1.5 text-[11.5px] text-muted">{title}</h3>
      {children}
    </section>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-20 flex-none text-small text-faint">{label}</dt>
      <dd className={cn("min-w-0 flex-1 break-words text-[12px] text-fg", mono && "font-mono text-[11.5px]")}>
        {value}
      </dd>
    </div>
  );
}

function PivotButton({
  onClick,
  disabled,
  title,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "rounded-md border border-control px-2 py-1 text-left text-[11.5px] text-muted transition-colors",
        disabled ? "cursor-not-allowed text-faint-2" : "hover:border-control-hover hover:text-ink",
      )}
    >
      {children}
    </button>
  );
}

/** The seam: a labelled gap that says what is missing and why, in the shape of the thing
 *  that will eventually go there. Not a blank, and not a stubbed value. */
function NotRecorded({ label, detail }: { label: string; detail: string }) {
  return (
    <div className="rounded-[8px] border border-dashed border-line-2 px-2.5 py-2">
      <p className="text-[11.5px] text-muted">{label}</p>
      <p className="mt-0.5 text-small leading-snug text-faint-2">
        Not recorded. {detail}
      </p>
    </div>
  );
}
