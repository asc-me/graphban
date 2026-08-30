import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { LessonsView } from "@/features/lessons/LessonsView";
import { ProjectProvider } from "@/features/ProjectContext";
import type { Eligibility, LessonDetail, LessonEnums, LessonList, LessonListRow } from "@/lib/types";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const ENUMS: LessonEnums = {
  reaches: ["project", "org"],
  lesson_classes: ["correction", "drift", "preference", "observation"],
  unclassified_filter: "unclassified",
  caught_states: ["caught", "missed", "unknown", "mixed"],
  eligibilities: ["ineligible", "eligible", "unverifiable", "promoted"],
  trends: ["dropping", "stable", "rising", "unmeasured"],
  transferability_states: ["unverified", "evidenced", "unverifiable", "overridden"],
};

const emptyList = (): LessonList => ({
  enums: ENUMS,
  results: [],
  total: 0,
  limit: 50,
  offset: 0,
  has_more: false,
});

const unverifiable: Eligibility = {
  state: "unverifiable",
  independence: null,
  distinct_projects: null,
  distinct_users: null,
  cluster_scan: "cluster_scope_unmeasured",
  reason: "unmeasured: cluster_scope_unmeasured",
};

function row(over: Partial<LessonListRow> = {}): LessonListRow {
  return {
    id: "sh_1",
    text: "Always bump the migration range.",
    scope: "global",
    source: "lesson from CORE-1",
    status: "published",
    origin: "user:alex",
    item_id: "CORE-1",
    project_id: "core",
    fresh: true,
    scoring_source: "",
    auto_confidence: null,
    created_at: "2026-08-01T00:00:00Z",
    reach: "project",
    lesson_class: "",
    suggested_class: "correction",
    age_state: "fresh",
    caught_state: "unknown",
    effectiveness: { score: null, trend: "unmeasured", drop_reasons: [] },
    eligibility: unverifiable,
    transferability: "unverifiable",
    ...over,
  };
}

function detail(over: Partial<LessonDetail> = {}): LessonDetail {
  const base = row();
  return {
    ...base,
    effectiveness: { ...base.effectiveness, history: [] },
    origin_path: "unknown",
    cluster: [],
    unread_cluster_tags: [],
    outcomes: [],
    events: [],
    originating_item: { id: "CORE-1", title: "Bump the range", status: "done" },
    ...over,
  };
}

function without<T extends object, K extends keyof T>(obj: T, key: K): Omit<T, K> {
  const { [key]: _, ...rest } = obj;
  return rest;
}

const eligible: Eligibility = {
  state: "eligible",
  independence: 3,
  distinct_projects: 3,
  distinct_users: 1,
  cluster_scan: "scanned",
  reason: "3 project(s) × 1 user(s) → independence 3 ≥ 3",
};

const { lessonsSpy, lessonSpy, promoteSpy, recordSpy } = vi.hoisted(() => ({
  lessonsSpy: vi.fn(async () => emptyList()),
  lessonSpy: vi.fn(async () => detail()),
  promoteSpy: vi.fn(async () => detail({ reach: "org" })),
  recordSpy: vi.fn(async () => detail()),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [project]),
    lessons: lessonsSpy,
    lesson: lessonSpy,
    recordLessonOutcome: recordSpy,
    promoteOrgLesson: promoteSpy,
  },
}));

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/lessons/:id" element={<LessonsView />} />
            <Route path="/lessons" element={<LessonsView />} />
            <Route path="/memory-review" element={<div>memory inbox</div>} />
            <Route path="/p/:tag/lessons/:id" element={<LessonsView />} />
            <Route path="/p/:tag/lessons" element={<LessonsView />} />
            <Route path="/p/:tag/memory-review" element={<div>memory inbox</div>} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Lessons catalog", () => {
  beforeEach(() => {
    lessonsSpy.mockReset();
    lessonSpy.mockReset();
    promoteSpy.mockReset();
    recordSpy.mockReset();
    lessonsSpy.mockResolvedValue(emptyList());
    lessonSpy.mockResolvedValue(detail());
    promoteSpy.mockResolvedValue(detail({ reach: "org" }));
    recordSpy.mockResolvedValue(detail());
  });

  it("uses the catalog empty copy and links to Memory, not a scoreboard", async () => {
    renderAt("/lessons");
    expect(await screen.findByText(/No published lessons in this project/)).toBeInTheDocument();
    expect(screen.getByText(/not a scoreboard/)).toBeInTheDocument();
    expect(screen.getByText(/empty catalog is not a high score/)).toBeInTheDocument();
    const memory = screen.getByRole("link", { name: "Memory" });
    expect(memory).toHaveAttribute("href", "/memory-review");
    expect(screen.queryByText(/you're all caught up/i)).not.toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
    expect(screen.queryByText(/all caught up/i)).not.toBeInTheDocument();
  });

  it("shows an unmeasured banner on a non-empty catalog, not the empty state", async () => {
    const published = row();
    lessonsSpy.mockResolvedValue({
      enums: ENUMS,
      results: [published],
      total: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    });
    renderAt("/lessons");
    const banner = await screen.findByText(/1 published lesson has no outcomes yet/);
    expect(banner.closest("div")).toHaveTextContent(/unknown, not effective/);
    expect(screen.getByText(/Always bump the migration range/)).toBeInTheDocument();
    expect(screen.queryByText(/No published lessons in this project/)).not.toBeInTheDocument();
  });

  it("does not invent a high score when effectiveness is dropped from the payload", async () => {
    const stripped = {
      ...row({ text: "A lesson with no score field." }),
      effectiveness: undefined,
    };
    lessonsSpy.mockResolvedValue({
      enums: ENUMS,
      results: [stripped as unknown as LessonListRow],
      total: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    });
    renderAt("/lessons");
    expect(await screen.findByText(/A lesson with no score field/)).toBeInTheDocument();
    expect(screen.queryByText("1.0")).not.toBeInTheDocument();
    expect(screen.queryByText("1.00")).not.toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
    expect(screen.queryByText(/you're all caught up/i)).not.toBeInTheDocument();
  });

  it("renders chips from payload enums, including unclassified_filter", async () => {
    lessonsSpy.mockResolvedValue({
      enums: ENUMS,
      results: [row()],
      total: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    });
    renderAt("/lessons");
    expect(await screen.findByRole("button", { name: "Unclassified" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unknown outcomes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Eligible for org" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dropping" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Unclassified" }));
    expect(lessonsSpy).toHaveBeenCalledWith("core", expect.objectContaining({ lesson_class: "unclassified" }));
  });

  it("counts omitted caught_state as unknown, so the banner still fires", async () => {
    const stripped = without(row({ text: "No caught_state on the wire." }), "caught_state");
    lessonsSpy.mockResolvedValue({
      enums: ENUMS,
      results: [stripped as unknown as LessonListRow],
      total: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    });
    renderAt("/lessons");
    expect(await screen.findByText(/No caught_state on the wire/)).toBeInTheDocument();
    expect(screen.getByText("1 UNMEASURED")).toBeInTheDocument();
    expect(screen.queryByText("0 UNMEASURED")).not.toBeInTheDocument();
    const banner = screen.getByText(/1 published lesson has no outcomes yet/);
    expect(banner.closest("div")).toHaveTextContent(/unknown, not effective/);
  });

  it("does not claim total in the unmeasured banner when has_more", async () => {
    lessonsSpy.mockResolvedValue({
      enums: ENUMS,
      results: [row()],
      total: 80,
      limit: 50,
      offset: 0,
      has_more: true,
    });
    renderAt("/lessons");
    expect(await screen.findByText(/At least 1 of this page/)).toBeInTheDocument();
    expect(screen.queryByText(/80 published lesson/)).not.toBeInTheDocument();
    expect(screen.getByText(/More lessons exist beyond this page/)).toBeInTheDocument();
    expect(screen.getByText(/1 UNMEASURED THIS PAGE/)).toBeInTheDocument();
  });

  it("names a failed catalog fetch instead of spinning or showing empty", async () => {
    lessonsSpy.mockRejectedValue(new Error("network down"));
    renderAt("/lessons");
    expect(await screen.findByText(/The lesson catalog could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
    expect(screen.queryByText(/No published lessons in this project/)).not.toBeInTheDocument();
  });

  it("keeps the Memory empty-state link on the current hosted project", async () => {
    renderAt("/p/CORE/lessons");
    const memory = await screen.findByRole("link", { name: "Memory" });
    expect(memory).toHaveAttribute("href", "/p/CORE/memory-review");
  });
});

describe("Lesson detail — promote is always visible with the real reason", () => {
  beforeEach(() => {
    lessonsSpy.mockReset();
    lessonSpy.mockReset();
    promoteSpy.mockReset();
    recordSpy.mockReset();
    lessonsSpy.mockResolvedValue(emptyList());
    promoteSpy.mockResolvedValue(detail({ reach: "org" }));
    recordSpy.mockResolvedValue(detail());
  });

  it("disables promote with eligibility.reason and has no override when unverifiable", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: unverifiable,
      reach: "project",
    }));
    renderAt("/lessons/sh_1");
    const btn = await screen.findByRole("button", { name: "Promote to org" });
    expect(btn).toBeDisabled();
    expect(screen.getByText("unmeasured: cluster_scope_unmeasured")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Override with reason/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.getByText(/not "ineligible/)).toBeInTheDocument();
  });

  it("offers override-with-reason when ineligible, still showing the formula", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: {
        state: "ineligible",
        independence: 1,
        distinct_projects: 1,
        distinct_users: 1,
        cluster_scan: "scanned",
        reason: "1 project(s) × 1 user(s) → independence 1 < 3",
      },
    }));
    renderAt("/lessons/sh_1");
    expect(await screen.findByText(/independence 1 < 3/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote to org" })).toBeDisabled();
    const override = screen.getByRole("button", { name: "Override with reason" });
    expect(override).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText(/Override reason/), "I have looked at three boxes");
    expect(screen.getByRole("button", { name: "Override with reason" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Override with reason" }));
    expect(promoteSpy).toHaveBeenCalledWith("sh_1", "I have looked at three boxes");
  });

  it("lets eligible + ok promote without an override", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: eligible,
      caught_state: "unknown",
      effectiveness: { score: null, trend: "unmeasured", drop_reasons: [], history: [] },
    }));
    renderAt("/lessons/sh_1");
    const btn = await screen.findByRole("button", { name: "Promote to org" });
    expect(btn).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Override with reason/ })).not.toBeInTheDocument();
    await userEvent.click(btn);
    expect(promoteSpy).toHaveBeenCalledWith("sh_1", undefined);
  });

  it("blocks eligible + failing behind override", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: eligible,
      caught_state: "missed",
      effectiveness: { score: 0, trend: "dropping", drop_reasons: ["contradicted"], history: [] },
    }));
    renderAt("/lessons/sh_1");
    expect(await screen.findByRole("button", { name: "Promote to org" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Override with reason" })).toBeInTheDocument();
    expect(screen.getByText(/contradicted by a later incident/)).toBeInTheDocument();
  });

  it("names empty history as empty, not a flat high score", async () => {
    lessonSpy.mockResolvedValue(detail({
      effectiveness: { score: null, trend: "unmeasured", drop_reasons: [], history: [] },
    }));
    renderAt("/lessons/sh_1");
    await screen.findByText(/Effectiveness history/);
    await userEvent.click(screen.getByText(/Effectiveness history/));
    expect(screen.getByText(/No counted outcomes — history is empty, not a flat high score/)).toBeInTheDocument();
    expect(screen.queryByText("1.0")).not.toBeInTheDocument();
  });

  it("distinguishes unindexed from a deleted path", async () => {
    lessonSpy.mockResolvedValue(detail({ origin_path: "unindexed" }));
    renderAt("/lessons/sh_1");
    expect(await screen.findByText(/code graph has not been indexed — not the same as a deleted path/)).toBeInTheDocument();
  });

  it("treats a missing cluster_scan as unmeasured, not an empty cluster", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: { ...unverifiable, cluster_scan: undefined as unknown as string },
    }));
    renderAt("/lessons/sh_1");
    await userEvent.click(await screen.findByText(/Corroborating shards/));
    expect(screen.getByText(/Other-project recurrence was not scanned/)).toBeInTheDocument();
    expect(screen.queryByText(/no corroborating shards/)).not.toBeInTheDocument();
  });

  it("treats a dropped eligibility as unverifiable, with no override", async () => {
    lessonSpy.mockResolvedValue(without(detail({ reach: "project" }), "eligibility") as unknown as LessonDetail);
    renderAt("/lessons/sh_1");
    expect(await screen.findByRole("button", { name: "Promote to org" })).toBeDisabled();
    expect(screen.getByText("eligibility was not returned")).toBeInTheDocument();
    expect(screen.getByText(/not "ineligible/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Override with reason/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByText(/Corroborating shards/));
    expect(screen.getByText(/Other-project recurrence was not scanned/)).toBeInTheDocument();
    expect(screen.queryByText(/no corroborating shards/)).not.toBeInTheDocument();
  });

  it("does not stamp evidenced when transferability is omitted on an org-reach lesson", async () => {
    lessonSpy.mockResolvedValue(without(detail({ reach: "org" }), "transferability") as unknown as LessonDetail);
    renderAt("/lessons/sh_1");
    expect(await screen.findByText(/transferability was not recorded/)).toBeInTheDocument();
    expect(screen.queryByText(/evidenced/)).not.toBeInTheDocument();
  });

  it("keeps gone-path eligible lessons behind override even if effectiveness is dropped", async () => {
    lessonSpy.mockResolvedValue({
      ...without(detail({
        origin_path: "gone",
        eligibility: eligible,
        caught_state: "unknown",
      }), "effectiveness"),
    } as unknown as LessonDetail);
    renderAt("/lessons/sh_1");
    expect(await screen.findByText(/originating code path no longer resolves/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote to org" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Override with reason" })).toBeInTheDocument();
  });

  it("surfaces a promote 422 reason instead of looking like a no-op", async () => {
    lessonSpy.mockResolvedValue(detail({
      eligibility: eligible,
      caught_state: "unknown",
      effectiveness: { score: null, trend: "unmeasured", drop_reasons: [], history: [] },
    }));
    promoteSpy.mockRejectedValue(
      new Error(JSON.stringify({
        detail: { state: "unverifiable", reason: "unmeasured: cluster_scope_unmeasured" },
      })),
    );
    renderAt("/lessons/sh_1");
    await userEvent.click(await screen.findByRole("button", { name: "Promote to org" }));
    expect(await screen.findByText("unmeasured: cluster_scope_unmeasured")).toBeInTheDocument();
  });
});

describe("Lessons docs overlay", () => {
  it("covers list and detail, including the hosted prefix", () => {
    expect(docFor("/lessons").title).toBe("Lessons");
    expect(docFor("/lessons/sh_1").title).toBe("Lessons");
    expect(docFor("/p/CORE/lessons").title).toBe("Lessons");
    expect(docFor("/p/CORE/lessons/sh_1").badge).toBe("LESSONS");
    expect(docFor("/lessons").related?.map((r) => r.to)).toEqual(["/memory-review", "/activity"]);
  });
});
