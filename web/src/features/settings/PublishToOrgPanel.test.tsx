/** Settings → Project → Publish to org (GRPH-1674): both on by default, says what each sends,
 * and a toggle sends only the flag it names. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { PublishToOrgPanel } from "@/features/settings/PublishToOrgPanel";
import type { Project } from "@/lib/types";

const proj: Project = {
  id: "core", tag: "CORE", name: "core", accent: "#c6f24e",
  visibility: "private", description: "", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
  credential_id: null, model_override: "",
};

const setPublishToOrg = vi.fn(async (_id: string, _body: object) => proj);

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [proj]),
    setPublishToOrg: (id: string, body: object) => setPublishToOrg(id, body),
  },
}));

it("shows both flags on by default with what each sends, and toggles one flag", async () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <PublishToOrgPanel />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const specs = await screen.findByLabelText("Publish specs to org");
  const digests = screen.getByLabelText("Publish item digests to org");
  expect(specs).toBeChecked();
  expect(digests).toBeChecked();
  expect(screen.getByText(/spec title, body and status/)).toBeInTheDocument();
  expect(screen.getByText(/item title, kind, status and touchpoints — never the item body/)).toBeInTheDocument();
  await userEvent.setup().click(specs);
  expect(setPublishToOrg).toHaveBeenCalledWith("core", { specs: false });
});
