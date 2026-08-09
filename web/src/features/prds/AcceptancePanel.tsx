import { AlertTriangle, Check, CircleDashed, FileWarning, Minus, ShieldQuestion } from "lucide-react";

import { useAuditCoverage, useCloseReport, usePrdEvidence } from "@/lib/queries";
import type { CloseReport, CloseReportSection, EvidenceRollup } from "@/lib/types";

/** Did we build what we said? — on one screen (GRPH-328 / PRD-12).
 *
 *  PRD-12's first success criterion asks that a reviewer answer that question "from one
 *  screen without hand-walking linked items". Every mechanism existed and none of it was
 *  reachable by a human: `api.ts` called no PRD-12 endpoint at all. The audit that found
 *  this could only find it by reading the repo, which is the authority split working.
 *
 *  Built on `close-report` rather than `completeness`, because the close report is the one
 *  surface that reads the ORIGINAL baseline. Intent a rebaseline removed does not exist in
 *  the governing one, so no other view can show a reviewer that something was cut — and
 *  "what got dropped" is the decision this screen exists to support.
 *
 *  It never says "complete". PRD-12 is explicit that the platform assesses whether CLAIMED
 *  work covers STATED intent and must never render that as a finished PRD, so there is no
 *  score, no percentage, and no green tick for the PRD as a whole. */

const FATE = {
  delivered: { icon: Check, color: "#5fd07a", label: "delivered" },
  undelivered: { icon: CircleDashed, color: "#e0b34a", label: "nothing delivered" },
  dropped: { icon: Minus, color: "#ff6b6b", label: "CUT FROM SPEC" },
  framing: { icon: null, color: "#8b949e", label: "framing" },
} as const;

export function AcceptancePanel({ prdId }: { prdId: string }) {
  const { data: report } = useCloseReport(prdId);
  const { data: evidence } = usePrdEvidence(prdId);
  const { data: coverage } = useAuditCoverage(prdId);

  if (!report) return <p className="text-[12px] text-faint">Loading…</p>;
  if (!report.governed) {
    // Never a zero. "Nothing to report" and "never had agreed intent to report against"
    // are different facts, and showing the second as the first is the misleading green
    // this whole PRD exists to stop.
    return (
      <div className="rounded-[11px] border border-line-2 bg-surface-2 p-4">
        <p className="text-[12.5px] text-fg-2">This PRD has no baseline.</p>
        <p className="mt-1 text-[11.5px] text-faint">
          Nothing was ever agreed, so there is nothing to check delivery against. Finish the
          grill to freeze intent — that is what makes acceptance answerable at all.
        </p>
      </div>
    );
  }

  const demanding = report.sections.filter((s) => !s.framing);
  const cut = demanding.filter((s) => s.fate === "dropped");
  const missing = demanding.filter((s) => s.fate === "undelivered");

  return (
    <div className="flex flex-col gap-3">
      <Header report={report} />

      {/* The two lists a reviewer acts on, and the reason the screen exists. Absence is
          reported explicitly rather than as an empty space someone has to notice. */}
      <Finding
        icon={<Minus size={12} />} tone="#ff6b6b"
        title={`Cut from the spec (${cut.length})`}
        empty="Nothing was removed from the original intent."
        rows={cut.map((s) => ({
          key: s.section,
          text: s.section,
          note: `removed at ${s.dropped_at}${s.disposition ? ` · ${s.disposition.disposition}${s.disposition.reason ? `: ${s.disposition.reason}` : ""}` : ""}`,
        }))}
      />
      <Finding
        icon={<CircleDashed size={12} />} tone="#e0b34a"
        title={`Nothing delivered (${missing.length})`}
        empty="Every section of agreed intent has work against it."
        rows={missing.map((s) => ({
          key: s.section,
          text: s.section,
          note: s.planned_items.length
            ? `${s.planned_items.length} item(s) linked, none done`
            : "no work linked at all",
        }))}
      />

      {evidence && <EvidenceFindings evidence={evidence} />}

      {report.expanded_scope.length > 0 && (
        <Finding
          icon={<FileWarning size={12} />} tone="#7ca2ff"
          title={`Not in the original spec (${report.expanded_scope.length})`}
          empty=""
          rows={report.expanded_scope.map((s) => ({ key: s, text: s, note: "added at a later baseline" }))}
        />
      )}

      <Sections sections={demanding} />

      {coverage?.governed && (
        <p className="font-mono text-[10px] text-faint">
          sign-off verdicts: {coverage.covered.length}/{coverage.covered.length + coverage.uncovered.length} sections
          {coverage.uncovered.length > 0 && ` · un-audited: ${coverage.uncovered.join(", ")}`}
        </p>
      )}
    </div>
  );
}

function Header({ report }: { report: CloseReport }) {
  const d = report.drift;
  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2 p-3">
      <div className="flex items-center gap-2">
        <ShieldQuestion size={13} className="text-fg-2" />
        <span className="font-mono text-[10px] uppercase tracking-wide text-fg-2">
          Delivered vs original intent
        </span>
      </div>
      <p className="mt-1.5 text-[11.5px] leading-snug text-faint">
        Measured against <span className="text-fg-2">{report.original_version}</span>, the intent
        first agreed — not <span className="text-fg-2">{report.governing_version}</span>, which is
        where the spec ended up. {d && (
          <>Drift: <span className="text-fg-2">{d.total}</span> ({d.accumulated} frozen in the
          chain, {d.current} live). Rebaselining moves drift into history; it never erases it.</>
        )}
      </p>
      {report.closed && (
        <p className="mt-2 rounded-[8px] border border-line-2 bg-surface px-2 py-1.5 text-[11px] text-fg-2">
          Closed {report.closed.mode === "mechanical" ? "mechanically" : "with a judge"} against{" "}
          {report.closed.baseline_version}.
          {report.closed.disclosure && (
            <span className="mt-0.5 block text-[10.5px] text-[#e0b34a]">{report.closed.disclosure}</span>
          )}
        </p>
      )}
    </div>
  );
}

function EvidenceFindings({ evidence }: { evidence: EvidenceRollup }) {
  if (!evidence.governed) return null;
  return (
    <>
      <Finding
        icon={<AlertTriangle size={12} />} tone="#e0b34a"
        title={`Delivered on unfalsifiable proof only (${evidence.unsupported.length})`}
        empty="Everything delivered carries proof someone else could check."
        rows={evidence.unsupported.map((s) => ({
          key: s, text: s,
          // PRD-12's words: a free-text note is "as easy to fabricate as a description".
          note: "only free-text notes — nothing re-runnable or re-fetchable",
        }))}
      />
      <Finding
        icon={<AlertTriangle size={12} />} tone="#ff6b6b"
        title={`Claimed code that is not in the graph (${evidence.uncorroborated.length})`}
        empty="Every declared touchpoint has code behind it."
        rows={evidence.uncorroborated.map((s) => ({ key: s, text: s, note: "" }))}
      />
    </>
  );
}

function Finding({ icon, tone, title, empty, rows }: {
  icon: React.ReactNode; tone: string; title: string; empty: string;
  rows: { key: string; text: string; note: string }[];
}) {
  if (rows.length === 0 && !empty) return null;
  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2 p-3">
      <div className="mb-1.5 flex items-center gap-2" style={{ color: rows.length ? tone : "#8b949e" }}>
        {icon}
        <span className="font-mono text-[10px] uppercase tracking-wide">{title}</span>
      </div>
      {rows.length === 0 ? (
        <p className="text-[11.5px] text-faint">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.map((r) => (
            <li key={r.key} className="text-[12px] text-fg-2">
              {r.text}
              {r.note && <span className="ml-1.5 text-[11px] text-faint">— {r.note}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Sections({ sections }: { sections: CloseReportSection[] }) {
  return (
    <div className="overflow-hidden rounded-[11px] border border-line-2">
      {sections.map((s) => {
        const meta = FATE[s.fate] ?? FATE.framing;
        const Icon = meta.icon;
        return (
          <div key={s.section}
               className="flex items-center gap-2 border-b border-line-2 px-2.5 py-1.5 last:border-b-0">
            {Icon ? <Icon size={11} style={{ color: meta.color }} /> : <span className="w-[11px]" />}
            <span className="min-w-0 flex-1 truncate text-[12px] text-fg-2">
              {s.section}
              {/* A rename is not a drop. Showing both names is what stops a reviewer
                  reading one piece of intent as two. */}
              {s.current_title !== s.section && (
                <span className="ml-1.5 text-[11px] text-faint">→ {s.current_title}</span>
              )}
            </span>
            <span className="font-mono text-[9.5px] text-faint">
              {s.delivered_items.length}/{s.planned_items.length}
            </span>
            <span className="w-[104px] shrink-0 text-right font-mono text-[9px] uppercase tracking-wide"
                  style={{ color: meta.color }}>
              {meta.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}
