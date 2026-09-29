/**
 * Activity's pure helpers (PRD-47 S10 / GRPH-961).
 *
 * These are the words and numbers the view renders, so they are tested without a DOM:
 * a component test that only checks "something rendered" would pass against a helper
 * that stamped every row `ok`. The two claims that matter most here are the ones about
 * absence — a payload that never declared its refusal coverage has to produce `unknown`
 * rather than a reassuring `ok`, and a histogram that was never asked for has to say so
 * rather than drawing a flat chart.
 */
import { describe, expect, it } from "vitest";

import {
  absTime,
  bucketRangeLabel,
  bucketTotal,
  bucketWidthLabel,
  coverageNote,
  groupEvents,
  RANGE_CHOICES,
  refusalActions,
  relTime,
  SERIES,
  summarizeMeta,
  targetLabel,
  toCsv,
  verdictOf,
  VERDICT_TITLE,
} from "@/features/activity/model";
import type { ActivityHistogram, ActivityLens, Event, HistogramBucket } from "@/lib/types";

const REFUSALS = ["sign_off_refused", "role_refused", "agent_reject_shard"];

function event(over: Partial<Event> = {}): Event {
  return {
    id: 1,
    ts: "2026-09-28T12:00:00+00:00",
    actor_type: "apikey",
    actor_id: "k1",
    actor_label: "loop-agent",
    surface: "mcp",
    action: "create_item",
    target_type: "item",
    target_id: "AL-42",
    project_id: "core",
    meta: null,
    ...over,
  } as Event;
}

function bucket(over: Partial<HistogramBucket> = {}): HistogramBucket {
  return { index: 0, start: "2026-09-28T11:00:00+00:00", end: "2026-09-28T11:30:00+00:00",
    agent: 0, human: 0, rejected: 0, system: 0, ...over };
}

function histogram(over: Partial<ActivityHistogram> = {}): ActivityHistogram {
  return { range: "24h", bucket_seconds: 1800, origin: "2026-09-28T12:00:00+00:00",
    buckets: [], coverage: "full", scanned: 0, partial: false, ...over };
}

describe("refusal coverage", () => {
  it("reads the lens payload rather than a second copy of the enum", () => {
    const lenses = [{ id: "rejected", label: "Rejected", hint: "", count: 1, covers: REFUSALS }] as ActivityLens[];
    expect(refusalActions(lenses)).toEqual(REFUSALS);
  });

  it("is null — not empty — when the payload never declared it", () => {
    // `null` and `[]` are different claims: an empty list says "no action is a refusal",
    // which would stamp every row `ok`.
    expect(refusalActions(undefined)).toBeNull();
    expect(refusalActions([])).toBeNull();
    expect(refusalActions([{ id: "everything", label: "", hint: "", count: 3 }])).toBeNull();
  });

  it("verdicts a row as unknown when coverage was not declared", () => {
    const refusal = event({ action: "sign_off_refused" });
    expect(verdictOf(refusal, null)).toBe("unknown");
    expect(verdictOf(event({ action: "create_item" }), null)).toBe("unknown");
  });

  it("verdicts against the declared coverage only", () => {
    expect(verdictOf(event({ action: "sign_off_refused" }), REFUSALS)).toBe("rejected");
    // A bounced review is a refusal in prose and not one in this ledger. With coverage
    // declared it must read `ok`, which is why the tile publishes what it covers.
    expect(verdictOf(event({ action: "bounce" }), REFUSALS)).toBe("ok");
  });

  it("explains all three verdicts, including the one that is not a pass", () => {
    expect(VERDICT_TITLE.unknown).toMatch(/unknown rather than ok/i);
    expect(Object.keys(VERDICT_TITLE)).toEqual(["ok", "rejected", "unknown"]);
  });
});

describe("histogram buckets", () => {
  it("totals every series, so a bar's total is the events in it", () => {
    expect(bucketTotal(bucket({ agent: 2, human: 1, rejected: 1, system: 3 }))).toBe(7);
    expect(SERIES.map((s) => s.key)).toEqual(["agent", "human", "rejected", "system"]);
  });

  it("names the bar width in words, and says nothing when there is none", () => {
    expect(bucketWidthLabel(1800)).toBe("30 min");
    expect(bucketWidthLabel(3600)).toBe("1h");
    expect(bucketWidthLabel(86400)).toBe("1d");
    expect(bucketWidthLabel(null)).toBe("");
    expect(bucketWidthLabel(0)).toBe("");
  });

  it("falls back to a bar number when the timestamps are unreadable", () => {
    expect(bucketRangeLabel(bucket({ index: 4, start: "nope", end: "also nope" }))).toBe("bar 5");
  });

  it("keeps not-requested, partial and full apart", () => {
    expect(coverageNote(histogram({ coverage: "not_requested" }))).toMatch(/no range selected/i);
    const partial = coverageNote(histogram({ coverage: "partial", scanned: 50000 }));
    expect(partial).toMatch(/50,000/);
    expect(partial).toMatch(/holds more than one read returns/);
    // A full scan needs no caveat — and inventing one would train readers to ignore it.
    expect(coverageNote(histogram({ coverage: "full" }))).toBeNull();
  });
});

describe("grouping", () => {
  it("labels runs without re-sorting them, so a header describes the rows under it", () => {
    const rows = [
      event({ id: 3, ts: "2026-09-28T12:00:00+00:00" }),
      event({ id: 2, ts: "2026-09-28T11:00:00+00:00" }),
      event({ id: 1, ts: "2026-09-28T10:00:00+00:00" }),
    ];
    // 24h groups by day AND hour; 30d collapses the same three rows into one day.
    const groups = groupEvents(rows, "time", "24h");
    expect(groups.map((g) => g.rows.map((r) => r.id))).toEqual([[3], [2], [1]]);
    expect(groupEvents(rows, "time", "30d")).toHaveLength(1);
  });

  it("buckets by minute at the 1h range rather than putting an hour under one header", () => {
    const rows = [
      event({ id: 2, ts: "2026-09-28T12:01:00+00:00" }),
      event({ id: 1, ts: "2026-09-28T12:00:00+00:00" }),
    ];
    expect(groupEvents(rows, "time", "1h")).toHaveLength(2);
    expect(groupEvents(rows, "time", "24h")).toHaveLength(1);
  });

  it("gives an undated row its own group instead of folding it into the run above", () => {
    const groups = groupEvents([event({ id: 2 }), event({ id: 1, ts: null })], "time", "24h");
    expect(groups).toHaveLength(2);
    expect(groups[1]!.label).toBe("No readable timestamp");
    expect(groups[1]!.rows.map((r) => r.id)).toEqual([1]);
  });

  it("groups by target, treating an empty target_type as a value", () => {
    const rows = [
      event({ id: 2, target_type: "", target_id: "" }),
      event({ id: 1, target_type: "item", target_id: "AL-1" }),
    ];
    const groups = groupEvents(rows, "target", "24h");
    expect(groups.map((g) => g.label)).toEqual(["untyped", "item · AL-1"]);
  });

  it("labels a target as untyped rather than blank", () => {
    expect(targetLabel(event({ target_type: "", target_id: "" }))).toBe("untyped");
    expect(targetLabel(event({ target_type: "item", target_id: "" }))).toBe("item");
    expect(targetLabel(event({ target_type: "item", target_id: "AL-1" }))).toBe("item · AL-1");
  });
});

describe("the row's summary line", () => {
  it("renders evidence receipts as kind — detail, never [object Object]", () => {
    const meta = { status: "review", evidence: [{ kind: "test", detail: "pnpm test 774 pass" }] };
    expect(summarizeMeta(meta)).toBe("status: review · evidence: test — pnpm test 774 pass");
    expect(summarizeMeta(meta)).not.toMatch(/\[object Object\]/);
  });

  it("omits the keys the header already shows", () => {
    const meta = { principal: { id: "u1", label: "alex" }, origin: "assistant:claude", agent_id: "g1" };
    expect(summarizeMeta(meta)).toBe("");
  });

  it("truncates a long receipt instead of pushing the row wide", () => {
    const summary = summarizeMeta({ evidence: [{ kind: "test", detail: "x".repeat(400) }] });
    expect(summary.length).toBeLessThan(200);
    expect(summary).toMatch(/…$/);
  });

  it("is empty for a record with no metadata", () => {
    expect(summarizeMeta(null)).toBe("");
  });
});

describe("time", () => {
  it("relativises a readable timestamp and declines an absent one", () => {
    // Not pinned to an exact second: the boundary between 4s and 5s is a timing flake.
    expect(relTime(new Date(Date.now() - 5000).toISOString())).toMatch(/^\ds ago$/);
    expect(relTime(new Date(Date.now() - 5 * 60_000).toISOString())).toMatch(/^\dm ago$/);
    expect(relTime(null)).toBe("");
    expect(relTime("not a date")).toBe("");
  });

  it("says a timestamp is missing rather than rendering nothing", () => {
    expect(absTime(null)).toBe("no timestamp recorded");
    expect(absTime("not a date")).toBe("unreadable timestamp");
    expect(absTime("2026-09-28T12:00:00+00:00")).not.toBe("");
  });
});

describe("CSV export", () => {
  it("carries the verdict column through, including unknown", () => {
    const rows = [event({ action: "sign_off_refused" }), event({ id: 2, action: "create_item" })];
    const csv = toCsv(rows, REFUSALS);
    expect(csv.split("\r\n")[0]).toMatch(/^id,ts,verdict,/);
    expect(csv).toMatch(/rejected/);

    // An export that stamped every row `ok` would carry the tile's defect into a
    // spreadsheet, where nothing renders the caveat next to it.
    const undeclared = toCsv(rows, null);
    expect(undeclared).toMatch(/unknown/);
    expect(undeclared).not.toMatch(/,ok,/);
  });

  it("quotes values that would otherwise shift every column after them", () => {
    const csv = toCsv([event({ actor_label: 'a, b "c"' })], REFUSALS);
    expect(csv).toContain('"a, b ""c"""');
  });

  it("exports only the rows it is given", () => {
    const lines = toCsv([event()], REFUSALS).trim().split("\r\n");
    expect(lines).toHaveLength(2);
  });
});

describe("range choices", () => {
  it("offers the four ranges the backend defines", () => {
    expect(RANGE_CHOICES.map((r) => r.id)).toEqual(["1h", "24h", "7d", "30d"]);
  });
});
