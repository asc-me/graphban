import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AuditCoverage, CloseReport, EvidenceRollup } from "@/lib/types";

/** "Did we build what we said?" on one screen (GRPH-328 / PRD-12).
 *
 *  PRD-12's first success criterion asks a reviewer to answer that "from one screen
 *  without hand-walking linked items". Every mechanism existed and none of it was
 *  reachable by a human — `api.ts` called no PRD-12 endpoint at all. That gap was found by
 *  auditing the PRD through its own agent surface and then reading the repo, which is the
 *  authority split PRD-12 designed working as intended.
 *
 *  These tests mostly assert NEGATIVES, because every finding this screen carries is an
 *  absence, and an absence rendered as blank space is one nobody sees. That is the failure
 *  this PRD hit five separate times in its own implementation. */

const mocks = vi.hoisted(() => ({
  report: vi.fn(), evidence: vi.fn(), coverage: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    closeReport: mocks.report,
    prdEvidence: mocks.evidence,
    auditCoverage: mocks.coverage,
  },
}));

const { AcceptancePanel } = await import("@/features/prds/AcceptancePanel");

function report(over: Partial<CloseReport> = {}): CloseReport {
  return {
    governed: true,
    original_version: "v1.0",
    governing_version: "v1.2",
    sections: [
      { section: "Problem", current_title: "Problem", introduced_at: "v1.0", dropped_at: null,
        framing: true, fate: "framing", delivered_items: [], planned_items: [], history: [],
        disposition: null },
      { section: "Baseline", current_title: "Baseline", introduced_at: "v1.0", dropped_at: null,
        framing: false, fate: "delivered", delivered_items: ["GB-1"], planned_items: ["GB-1"],
        history: [], disposition: null },
      { section: "Judging", current_title: "Judging", introduced_at: "v1.0", dropped_at: null,
        framing: false, fate: "undelivered", delivered_items: [], planned_items: ["GB-2"],
        history: [], disposition: null },
    ],
    dropped: [], never_delivered: ["Judging"], expanded_scope: [],
    added_after_approval: [],
    drift: { accumulated: 3, current: 1, total: 4 },
    closed: null,
    ...over,
  };
}

const emptyEvidence: EvidenceRollup = {
  governed: true, baseline_version: "v1.2", sections: [], unsupported: [], uncorroborated: [],
};
const coverage: AuditCoverage = { governed: true, covered: ["Baseline"], uncovered: ["Judging"] };

function draw(r: CloseReport = report(), e: EvidenceRollup = emptyEvidence) {
  mocks.report.mockResolvedValue(r);
  mocks.evidence.mockResolvedValue(e);
  mocks.coverage.mockResolvedValue(coverage);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AcceptancePanel prdId="PRD-12" />
    </QueryClientProvider>,
  );
}

describe("acceptance panel", () => {
  it("names intent a rebaseline cut, which no other surface can show", async () => {
    // The reason this is built on close-report: a cut section does not exist in the
    // GOVERNING baseline, so a completeness view literally cannot report it.
    draw(report({
      dropped: ["Judging"],
      sections: report().sections.map((s) =>
        s.section === "Judging"
          ? { ...s, fate: "dropped" as const, dropped_at: "v1.1" }
          : s),
    }));

    expect(await screen.findByText(/Cut from the spec \(1\)/)).toBeTruthy();
    expect(screen.getByText(/removed at v1\.1/)).toBeTruthy();
  });

  it("states plainly when nothing was cut, rather than showing an empty space", async () => {
    // An absence rendered as blank is one nobody reads. Five defects in this PRD were
    // exactly that shape.
    draw();
    expect(await screen.findByText(/Nothing was removed from the original intent/)).toBeTruthy();
  });

  it("separates work never delivered from intent that was cut", async () => {
    // Somebody decided, versus nobody did. Conflating them tells a reviewer a choice was
    // made when none was — and this screen exists for exactly that choice.
    draw();
    expect(await screen.findByText(/Nothing delivered \(1\)/)).toBeTruthy();
    expect(screen.getByText(/Cut from the spec \(0\)/)).toBeTruthy();
  });

  it("measures against the ORIGINAL baseline and says so", async () => {
    draw();
    expect(await screen.findByText(/v1\.0/)).toBeTruthy();
    expect(screen.getByText(/where the spec ended up/)).toBeTruthy();
  });

  it("never renders the PRD as complete", async () => {
    // PRD-12: the platform assesses whether CLAIMED work covers STATED intent and must
    // never render that as a finished PRD. No score, no percentage, no overall tick.
    const { container } = draw(report({
      sections: report().sections.map((s) => ({ ...s, fate: s.framing ? "framing" as const : "delivered" as const })),
      never_delivered: [],
    }));
    await screen.findByText(/Delivered vs original intent/);

    expect(container.textContent).not.toMatch(/\bcomplete\b/i);
    expect(container.textContent).not.toMatch(/\d+%/);
  });

  it("distinguishes 'no baseline' from 'nothing to report'", async () => {
    draw({ ...report(), governed: false });
    expect(await screen.findByText(/no baseline/)).toBeTruthy();
    expect(screen.getByText(/nothing to check delivery against/)).toBeTruthy();
  });

  it("flags delivered work resting only on unfalsifiable proof", async () => {
    draw(report(), { ...emptyEvidence, unsupported: ["Baseline"] });
    expect(await screen.findByText(/Delivered on unfalsifiable proof only \(1\)/)).toBeTruthy();
  });

  it("flags declared code the graph cannot corroborate", async () => {
    // The finding that exposed this whole gap: three items declared web/ touchpoints and
    // shipped none.
    draw(report(), { ...emptyEvidence, uncorroborated: ["GRPH-241 → web/src/features/prds"] });
    expect(await screen.findByText(/Claimed code that is not in the graph \(1\)/)).toBeTruthy();
    expect(screen.getByText(/GRPH-241 → web\/src\/features\/prds/)).toBeTruthy();
  });

  it("shows a rename as one piece of intent, not two", async () => {
    draw(report({
      sections: report().sections.map((s) =>
        s.section === "Judging" ? { ...s, current_title: "Judging work" } : s),
    }));
    expect(await screen.findByText(/→ Judging work/)).toBeTruthy();
  });

  it("reports how much of the PRD carries a sign-off verdict", async () => {
    draw();
    expect(await screen.findByText(/1\/2 sections/)).toBeTruthy();
    expect(screen.getByText(/un-audited: Judging/)).toBeTruthy();
  });

  it("carries a mechanical close's disclosure rather than dropping it", async () => {
    draw(report({
      closed: {
        closed_at: "2026-08-09", closed_by: "user:1", mode: "mechanical",
        baseline_version: "v1.2", verdict: "",
        disclosure: "No judge is configured. Whether the work SATISFIES the intent was not assessed.",
        dispositions: [],
      },
    }));
    expect(await screen.findByText(/was not assessed/)).toBeTruthy();
  });
});
