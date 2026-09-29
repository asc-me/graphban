/**
 * The Activity ledger (AL-43) rebuilt as a lens-and-histogram surface (PRD-47 S10 /
 * GRPH-961).
 *
 * Re-derived rather than recovered: the two tests this file replaced asserted against a
 * payload shape that no longer exists, and one of them — `queryByText(/EVENTS/)` — had
 * already gone vacuous, because the rebuilt header stopped rendering that word in ANY
 * state, so it passed against a view that rendered the empty state under the failure.
 *
 * What is asserted here, and why each one is load-bearing:
 *   - the two behaviours the old file protected still hold (AL-197's actor chain, and
 *     evidence receipts never rendering as `[object Object]`);
 *   - the `rejected` lens STATES its own coverage, at a count of zero as well as at a
 *     count of one — a tile labelled "Rejected" that silently means three recorded
 *     refusal kinds is the absence-reads-as-clean defect wearing a tile;
 *   - an undeclared coverage produces an `unknown` verdict, not a reassuring `ok`;
 *   - the two empties are different sentences, and which one renders is decided by a
 *     number no filter touches;
 *   - a `partial` histogram keeps its bars (see the sabotage note on that test);
 *   - the filters reach the CALL, not just the component that holds the state.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityView } from "@/features/activity/ActivityView";
import { ProjectProvider } from "@/features/ProjectContext";
import type {
  ActivityFacets,
  ActivityHistogram,
  ActivityLens,
  Event,
  EventPage,
  HistogramBucket,
} from "@/lib/types";

const REFUSALS = ["sign_off_refused", "role_refused", "agent_reject_shard"];

const ISO = "2026-09-28T12:00:00+00:00";

function event(over: Partial<Event> = {}): Event {
  return {
    id: 1, ts: ISO, actor_type: "apikey", actor_id: "k1", actor_label: "loop-agent",
    surface: "mcp", action: "create_item", target_type: "item", target_id: "AL-42",
    project_id: "core", meta: null, ...over,
  } as Event;
}

function bucket(index: number, over: Partial<HistogramBucket> = {}): HistogramBucket {
  const start = new Date(Date.parse(ISO) - (47 - index) * 1800_000).toISOString();
  const end = new Date(Date.parse(start) + 1800_000).toISOString();
  return { index, start, end, agent: 0, human: 0, rejected: 0, system: 0, ...over };
}

/** 48 bars, the last one carrying something, so the chart has a peak to draw against. */
function buckets(filled: Partial<HistogramBucket> = {}): HistogramBucket[] {
  return Array.from({ length: 48 }, (_, i) => bucket(i, i === 47 ? filled : {}));
}

function histogram(over: Partial<ActivityHistogram> = {}): ActivityHistogram {
  return {
    range: "24h", bucket_seconds: 1800, origin: ISO, buckets: buckets({ agent: 2 }),
    coverage: "full", scanned: 2, partial: false, ...over,
  };
}

/** `covers` is what lets the tile state its own limits; omit it to model a payload that
 *  never declared them. */
function lenses(over: Partial<Record<ActivityLens["id"], Partial<ActivityLens>>> = {}): ActivityLens[] {
  const defs: [ActivityLens["id"], string, number][] = [
    ["everything", "Everything", 3],
    ["agent_writes", "Agent writes", 2],
    ["human_decisions", "Human decisions", 1],
    ["keys_access", "Keys & access", 1],
    ["memory", "Memory changes", 0],
    ["rejected", "Rejected", 0],
  ];
  return defs.map(([id, label, count]) => ({
    id, label, count, hint: `${label} hint`,
    ...(id === "rejected" ? { covers: REFUSALS } : {}),
    ...over[id],
  }));
}

function facets(): ActivityFacets {
  return {
    person: {
      values: [
        { value: "k1", label: "loop-agent", kind: "apikey", count: 2 },
        { value: "u1", label: "alex", kind: "user", count: 1 },
      ],
      truncated: false,
    },
    surface: {
      values: [{ value: "mcp", label: "mcp", count: 2 }, { value: "rest", label: "rest", count: 1 }],
      truncated: false,
    },
    object: { values: [{ value: "", label: "untyped", count: 3 }], truncated: false },
  };
}

function page(over: Partial<EventPage> = {}): EventPage {
  const results = over.results ?? [
    event({
      id: 2, actor_type: "apikey", actor_id: "k1", actor_label: "loop-agent",
      principal: "alex", agent: "loop-agent", action: "create_item", target_id: "AL-42",
      meta: null,
    }),
    event({
      id: 1, ts: ISO, actor_type: "user", actor_id: "u1", actor_label: "ascme",
      surface: "rest", action: "revoke_api_key", target_type: "api_key", target_id: "k9",
      meta: { name: "old" },
    }),
  ];
  return {
    results, total: results.length, limit: 100, offset: 0, has_more: false,
    ledger_total: results.length, lenses: lenses(), histogram: histogram(),
    facets: facets(),
    filters: {
      lens: "everything", actor: null, surface: null, target_type: null, target_id: null,
      bucket: null, range: "24h", action: null,
    },
    ...over,
  } as EventPage;
}

/** Read at CALL time, so each test can swap the payload without re-mocking. */
let served: EventPage = page();

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => []),
    events: vi.fn(async () => served),
  },
}));

async function show() {
  const { api } = await import("@/lib/api");
  vi.mocked(api.events).mockClear();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/activity"]}>
        <ProjectProvider>
          <ActivityView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // The first read has to land before anything can be asserted about the row list.
  await screen.findByRole("group", { name: "Activity lenses" });
  return vi.mocked(api.events);
}

function lensGroup() {
  return screen.getByRole("group", { name: "Activity lenses" });
}

function row(id: number): HTMLElement {
  const el = document.querySelector(`[data-event-id="${id}"]`);
  if (!el) throw new Error(`no row rendered for event ${id}`);
  return el as HTMLElement;
}

/** The query the view last asked for — the CALL, not the state that would drive it. */
function lastQuery(events: { mock: { calls: unknown[][] } }): Record<string, unknown> {
  const calls = events.mock.calls;
  return (calls[calls.length - 1]?.[0] ?? {}) as Record<string, unknown>;
}

beforeEach(() => {
  served = page();
});

describe("the ledger rows", () => {
  it("renders the actor chain AL-197 asked for: person, via agent, action, target", async () => {
    await show();
    // Scoped to the row: "alex" is also a value in the person facet, and those are two
    // different claims about the same name.
    const agentRow = within(row(2));
    expect(agentRow.getByText("alex")).toBeInTheDocument();
    expect(agentRow.getByText("loop-agent")).toBeInTheDocument();
    expect(agentRow.getByText("via")).toBeInTheDocument();
    expect(agentRow.getByText("create_item")).toBeInTheDocument();
    expect(agentRow.getByText("AL-42")).toBeInTheDocument();

    const humanRow = within(row(1));
    expect(humanRow.getByText("revoke_api_key")).toBeInTheDocument();
    expect(humanRow.getByText("k9")).toBeInTheDocument();
  });

  it("renders evidence receipts as kind — detail, not [object Object]", async () => {
    served = page({
      results: [event({
        id: 3, action: "update_item", target_id: "GRPH-920",
        meta: { status: "review", evidence: [{ kind: "test", detail: "pnpm test 774 pass" }] },
      })],
      ledger_total: 1,
    });
    await show();
    expect(await screen.findByText(/evidence: test — pnpm test 774 pass/)).toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it("stamps a verdict per row, and the recorded refusals are the rejected ones", async () => {
    served = page({
      results: [event({ id: 5, action: "sign_off_refused" }), event({ id: 4, action: "create_item" })],
      lenses: lenses({ rejected: { count: 1 } }),
      ledger_total: 2,
    });
    await show();
    expect(within(row(5)).getByText("rejected")).toBeInTheDocument();
    expect(within(row(4)).getByText("ok")).toBeInTheDocument();
  });
});

describe("the rejected lens states its own coverage", () => {
  it("names the refusal kinds it reads, so the count cannot be read as every refusal", async () => {
    await show();
    const tile = within(lensGroup()).getByRole("button", { name: /Rejected/ });
    expect(within(tile).getByText(/3 recorded refusal kinds only/)).toBeInTheDocument();
    expect(within(tile).getByText(REFUSALS.join(" · "))).toBeInTheDocument();
  });

  it("still says it at a count of zero — a quiet tile is not a complete one", async () => {
    served = page({ results: [], ledger_total: 0 });
    await show();
    const tile = within(lensGroup()).getByRole("button", { name: /Rejected/ });
    expect(within(tile).getByText("0")).toBeInTheDocument();
    expect(within(tile).getByText(/3 recorded refusal kinds only/)).toBeInTheDocument();
  });
});

describe("an undeclared coverage is not a clean pass", () => {
  it("verdicts rows unknown rather than ok when the payload never said what a refusal is", async () => {
    served = page({ lenses: lenses({ rejected: { covers: undefined } }) });
    await show();
    expect(within(row(2)).getByText("unknown")).toBeInTheDocument();
    expect(within(row(2)).queryByText("ok")).not.toBeInTheDocument();
  });
});

describe("the two empties are different sentences", () => {
  it("says nothing matches this selection when the ledger holds events", async () => {
    served = page({ results: [], total: 0, ledger_total: 1200 });
    await show();
    expect(await screen.findByText(/Nothing in this selection/)).toBeInTheDocument();
    expect(screen.getByText(/1,200 events overall/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear filter/i })).toBeInTheDocument();
    // The claim that nothing has EVER happened must not be reachable from a filter.
    expect(screen.queryByText(/No activity yet/i)).not.toBeInTheDocument();
  });

  it("says no activity yet only when the ledger itself is empty", async () => {
    served = page({ results: [], total: 0, ledger_total: 0, histogram: histogram({ buckets: buckets() }) });
    await show();
    expect(await screen.findByText(/No activity yet/)).toBeInTheDocument();
    expect(screen.getByText(/this is not a filter result/)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing in this selection/)).not.toBeInTheDocument();
  });
});

describe("the histogram", () => {
  it("keeps its bars when coverage is partial, and names what it could not scan", async () => {
    served = page({
      histogram: histogram({ coverage: "partial", partial: true, scanned: 50_000 }),
    });
    await show();
    expect(screen.getByText(/newest 50,000 events in range/)).toBeInTheDocument();
    // Sabotage: render the note INSTEAD of the bars when `coverage === "partial"` — the
    // recovered code did exactly that, and this is the assertion that catches it. A note
    // describing bars nobody can see is worse than no note: the chart goes quiet and the
    // reason given is about a window that is merely too big to read in one pass.
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(48);
  });

  it("says the chart was never asked for rather than drawing 48 flat bars", async () => {
    served = page({
      histogram: {
        range: null, bucket_seconds: null, origin: null, buckets: [],
        coverage: "not_requested", scanned: 0, partial: false,
      },
    });
    await show();
    expect(screen.getByText(/No range selected/)).toBeInTheDocument();
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(0);
  });

  it("calls a flat chart a true zero over the window, not a chart that failed", async () => {
    served = page({ histogram: histogram({ buckets: buckets() }) });
    await show();
    expect(screen.getByText(/true zero over the whole window/)).toBeInTheDocument();
  });
});

describe("filters reach the call", () => {
  it("asks for the lens that was clicked", async () => {
    const events = await show();
    fireEvent.click(within(lensGroup()).getByRole("button", { name: /Agent writes/ }));
    await screen.findByText("create_item");
    expect(lastQuery(events).lens).toBe("agent_writes");
    expect(lastQuery(events).range).toBe("24h");
  });

  it("asks for the bar that was clicked, and drops the bar when the range changes", async () => {
    const events = await show();
    fireEvent.click(document.querySelector('[data-bucket="47"]')!);
    await screen.findByText(/Bar 48/);
    expect(lastQuery(events).bucket).toBe(47);

    fireEvent.change(screen.getByLabelText(/Range/), { target: { value: "7d" } });
    await screen.findByText(/Nothing in this selection|create_item/);
    expect(lastQuery(events).range).toBe("7d");
    expect(lastQuery(events).bucket).toBeNull();
  });

  it("asks for the facet value that was clicked, and not for the dimension it just collapsed", async () => {
    const events = await show();
    const person = () => within(screen.getByRole("group", { name: "Filter by person" }));
    fireEvent.click(person().getByRole("button", { name: /^alex/ }));
    await screen.findByText(/person: alex/);
    expect(lastQuery(events).actor).toBe("u1");
    // The person facet is listed unfiltered by person, or picking one would leave no way
    // to pick another.
    expect(person().getByRole("button", { name: /^loop-agent/ })).toBeInTheDocument();
  });
});

describe("the event panel", () => {
  it("opens on the row and names what the record does not carry", async () => {
    await show();
    fireEvent.click(row(2));
    const panel = await screen.findByRole("complementary", { name: /Event 2/ });

    expect(within(panel).getByText("Actor chain")).toBeInTheDocument();
    expect(within(panel).getByText("alex")).toBeInTheDocument();

    // The design's field-level diff and trace id are not columns on the record. The seam
    // is labelled: an empty area where a diff should be reads as "nothing changed".
    expect(within(panel).getByText("Field-level diff")).toBeInTheDocument();
    expect(within(panel).getByText("Trace id")).toBeInTheDocument();
    expect(within(panel).getAllByText(/Not recorded/).length).toBeGreaterThanOrEqual(2);
    expect(within(panel).getByText(/GRPH-979/)).toBeInTheDocument();
  });

  it("disables the target pivot for an event that recorded no target id", async () => {
    served = page({ results: [event({ id: 7, target_type: "", target_id: "" })], ledger_total: 1 });
    await show();
    fireEvent.click(row(7));
    const panel = await screen.findByRole("complementary", { name: /Event 7/ });
    const pivot = within(panel).getByRole("button", { name: /All events on this target/ });
    expect(pivot).toBeDisabled();
    expect(pivot.getAttribute("title")).toMatch(/nothing to pivot on/);
  });

  it("closes on Escape", async () => {
    await show();
    fireEvent.click(row(2));
    await screen.findByRole("complementary", { name: /Event 2/ });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("complementary", { name: /Event 2/ })).not.toBeInTheDocument();
  });
});

describe("keyboard navigation", () => {
  it("moves the cursor with J and opens the panel with Enter", async () => {
    await show();
    expect(row(2)).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(window, { key: "j" });
    expect(row(1)).toHaveAttribute("aria-current", "true");
    // The list is two rows long; J must not walk off the end.
    fireEvent.keyDown(window, { key: "j" });
    expect(row(1)).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(window, { key: "k" });
    expect(row(2)).toHaveAttribute("aria-current", "true");

    fireEvent.keyDown(window, { key: "Enter" });
    await screen.findByRole("complementary", { name: /Event 2/ });
  });
});

describe("export", () => {
  it("is disabled when there is nothing to export", async () => {
    served = page({ results: [], total: 0, ledger_total: 0, histogram: histogram({ buckets: buckets() }) });
    await show();
    expect(screen.getByRole("button", { name: /Export 0 rows/ })).toBeDisabled();
  });

  it("names how many rows it will write, and says when that is fewer than matched", async () => {
    served = page({ has_more: true, total: 250, ledger_total: 900 });
    await show();
    const button = screen.getByRole("button", { name: /Export 2 rows/ });
    expect(button).toBeEnabled();
    expect(button.getAttribute("title")).toMatch(/250 match — the rest are beyond this page/);
  });
});
