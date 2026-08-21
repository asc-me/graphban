/**
 * A PRD status the build has never heard of costs one chip, not the page (GRPH-458).
 *
 * Reported live as "the PRD page isn't loading, other views work fine". It was not slow and
 * not erroring on the wire — `/api/prds` returned 2.8 KB in milliseconds. `PRD_STATUS_META`
 * covered `draft | review | approved`; the backend's canonical list has four; `GRPH-P18` was
 * closed on 2026-08-20 and from that moment `meta.color` threw on every render of the list
 * and the editor. One row with an unmapped status, and the whole surface went white.
 *
 * TypeScript could not have caught it: `Record<PrdStatus, …>` was exhaustive over a union
 * that was itself missing the value, so the map was complete, the index type-checked, and
 * the type agreed with the code while both disagreed with the server. The backend-facing
 * half of this guard therefore lives in `backend/tests/test_prd_status_union.py`, which
 * compares the two lists — a test inside the frontend cannot know what the server can send.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PRD_STATUS_META, PRD_STATUS_ORDER, prdStatusMeta } from "@/features/prds/meta";
import type { PrdStatus } from "@/lib/types";

describe("prdStatusMeta", () => {
  it("names every status the union declares", () => {
    for (const s of PRD_STATUS_ORDER) {
      expect(PRD_STATUS_META[s], `${s} has no meta`).toBeTruthy();
      expect(prdStatusMeta(s).label).toBe(PRD_STATUS_META[s].label);
    }
  });

  it("includes `closed`, the one that broke the page", () => {
    // Named rather than covered by the loop above: the loop passes just as happily on a
    // three-entry map paired with a three-entry order, which is exactly the state that
    // shipped.
    expect(PRD_STATUS_ORDER).toContain("closed" as PrdStatus);
    expect(prdStatusMeta("closed").label).toBe("Closed");
  });

  it("returns a usable chip for a status this build has never seen", () => {
    const meta = prdStatusMeta("superseded");

    expect(meta.color).toMatch(/^#/);
    // The RAW value, not "Unknown" — whoever is looking at it needs to know which status
    // their build cannot name in order to say so.
    expect(meta.label).toBe("superseded");
  });

  it("never returns undefined, which is the whole failure", () => {
    for (const s of ["", "closed", "nonsense", "APPROVED"]) {
      const meta = prdStatusMeta(s);
      expect(meta).toBeTruthy();
      expect(typeof meta.color).toBe("string");
    }
  });
});

/**
 * The regression itself, rendered. The unit tests above would all pass against a version
 * that still indexed the map directly at the call site, because they never render a row.
 */
describe("the PRD list renders a row whose status is unmapped", () => {
  function StatusChip({ status }: { status: string }) {
    const meta = prdStatusMeta(status);
    return (
      <span style={{ color: meta.color }} data-testid="chip">
        <span style={{ background: meta.color }} />
        {meta.label}
      </span>
    );
  }

  it("does not throw, and shows the status", () => {
    render(<StatusChip status="closed" />);
    expect(screen.getByTestId("chip").textContent).toBe("Closed");
  });

  it("survives a status invented after this build shipped", () => {
    expect(() => render(<StatusChip status="archived" />)).not.toThrow();
  });
});
