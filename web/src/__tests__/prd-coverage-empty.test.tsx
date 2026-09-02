import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PrdCoverage } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  coverage: vi.fn(),
  items: vi.fn(async () => []),
}));

vi.mock("@/lib/api", () => ({
  api: {
    prdCoverage: mocks.coverage,
    items: mocks.items,
  },
}));

const { CoveragePanel } = await import("@/features/prds/PrdEditorView");

function draw(cov: PrdCoverage) {
  mocks.coverage.mockResolvedValue(cov);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CoveragePanel prdId="p1" projectId="core" onDecomposed={() => {}} />
    </QueryClientProvider>,
  );
}

const base: PrdCoverage = {
  prd_id: "p1",
  title: "T",
  status: "draft",
  sections: [],
  section_count: 0,
  implementable_sections: 0,
  sections_with_tasks: 0,
  gaps: [],
  shaped: true,
  empty_sections: [],
  total_items: 0,
  done_items: 0,
  percent_done: 0,
  open_high_fidelity: 0,
};

describe("Coverage panel occupancy (GRPH-651)", () => {
  it("names an unshaped body instead of looking fully covered", async () => {
    draw({ ...base, shaped: false, empty_sections: [] });
    expect(await screen.findByText(/No sections yet — not a clean pass/)).toBeInTheDocument();
    expect(screen.queryByText(/empty — not a task gap/)).not.toBeInTheDocument();
  });

  it("does not offer Fill gaps on a draft", async () => {
    draw({
      ...base,
      status: "draft",
      section_count: 1,
      implementable_sections: 1,
      gaps: ["API"],
      sections: [{
        section: "API",
        implementable: true,
        item_count: 0,
        done: 0,
        by_status: {},
        gap: true,
        empty: true,
        high_fidelity: 0,
        open_high_fidelity: 0,
        item_ids: [],
      }],
    });
    expect(await screen.findByText(/grill earns approved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Fill/ })).not.toBeInTheDocument();
  });

  it("offers Fill gaps once approved", async () => {
    draw({
      ...base,
      status: "approved",
      section_count: 1,
      implementable_sections: 1,
      gaps: ["API"],
      sections: [{
        section: "API",
        implementable: true,
        item_count: 0,
        done: 0,
        by_status: {},
        gap: true,
        empty: true,
        high_fidelity: 0,
        open_high_fidelity: 0,
        item_ids: [],
      }],
    });
    expect(await screen.findByRole("button", { name: /Fill 1 gap/ })).toBeInTheDocument();
    expect(screen.queryByText(/grill earns approved/i)).not.toBeInTheDocument();
  });

  it("chips Empty distinct from no tasks", async () => {
    draw({
      ...base,
      section_count: 1,
      implementable_sections: 1,
      gaps: ["API"],
      empty_sections: ["API"],
      sections: [{
        section: "API",
        implementable: true,
        item_count: 0,
        done: 0,
        by_status: {},
        gap: true,
        empty: true,
        high_fidelity: 0,
        open_high_fidelity: 0,
        item_ids: [],
      }],
    });
    expect(await screen.findByText("empty")).toBeInTheDocument();
    expect(screen.getByText("no tasks")).toBeInTheDocument();
    expect(screen.getByText(/1 empty — not a task gap/)).toBeInTheDocument();
  });
});
