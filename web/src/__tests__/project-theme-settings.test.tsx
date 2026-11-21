/**
 * Settings → Project can change a theme the wizard already set (GRPH-1802).
 * The picker is the wizard's. This file pins the settings call: prefilled
 * controls, a PATCH of only the theme fields that changed, and a projects
 * refetch so tiles update without a reload.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { projectFill } from "@/lib/projectFill";
import { keys } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { Project } from "@/lib/types";

const LIME = "#c6f24e";
const INDIGO = "#9d9cff";

const project: Project = {
  id: "core",
  tag: "CORE",
  name: "Core",
  accent: LIME,
  accent2: INDIGO,
  pattern: "paisley",
  visibility: "private",
  description: "A library",
  share_global_memory: false,
  auto_extract: true,
  mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "",
  memory_auto_reject: true,
  memory_write_mode: "review",
  memory_llm_judge: false,
  agent_adjudication: false,
  allow_self_review: false,
};

const { projectsSpy, updateSpy, configSpy } = vi.hoisted(() => ({
  projectsSpy: vi.fn(async () => [project]),
  updateSpy: vi.fn(async (_id: string, body: Partial<Project>) => ({ ...project, ...body })),
  configSpy: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    updateProject: updateSpy,
    config: configSpy,
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
    platform: vi.fn(async () => null),
    members: vi.fn(async () => []),
  },
}));

function renderSettings() {
  localStorage.setItem("gb_last_project_tag", "CORE");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[settingsPath("project")]}>
        <ProjectProvider>
          <Routes>
            <Route path="/settings/*" element={<SettingsView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { invalidate, ...view };
}

function checkedIn(name: string) {
  return within(screen.getByRole("radiogroup", { name }))
    .getAllByRole("radio")
    .find((r) => r.getAttribute("aria-checked") === "true");
}

async function shown() {
  expect(await screen.findByText("Theme")).toBeInTheDocument();
  return userEvent.setup();
}

describe("Settings project theme (GRPH-1802)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem("gb_last_project_tag");
    projectsSpy.mockResolvedValue([project]);
    updateSpy.mockImplementation(async (_id: string, body: Partial<Project>) => ({ ...project, ...body }));
    configSpy.mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
  });

  it("prefills Lime, ombre Indigo and paisley, and patches only the pattern that changed", async () => {
    const { invalidate } = renderSettings();
    const user = await shown();
    const painted = projectFill({ accent: LIME, accent2: INDIGO, pattern: "paisley" });

    expect(checkedIn("Accent style")).toHaveAccessibleName("OMBRE");
    expect(checkedIn("Ombre, first colour")).toHaveAccessibleName("Lime");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Indigo");
    expect(checkedIn("Pattern")).toHaveAccessibleName("Paisley");
    expect(screen.getByTestId("pw-pattern-name")).toHaveTextContent("Paisley");
    expect(screen.getByTestId("project-tile")).toHaveStyle({ "--tile-fill": painted });

    const reads = projectsSpy.mock.calls.length;
    await user.click(within(screen.getByRole("radiogroup", { name: "Pattern" })).getByRole("radio", { name: "Speckled" }));
    expect(checkedIn("Pattern")).toHaveAccessibleName("Speckled");
    expect(screen.getByTestId("project-tile")).toHaveStyle({
      "--tile-fill": projectFill({ accent: LIME, accent2: INDIGO, pattern: "speckled" }),
    });

    await user.click(screen.getByRole("button", { name: "Save project" }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    const [id, body] = updateSpy.mock.calls[0] as [string, Partial<Project>];
    expect(id).toBe("core");
    expect(body.pattern).toBe("speckled");
    expect(body).not.toHaveProperty("accent");
    expect(body).not.toHaveProperty("accent2");
    expect(body.name).toBe("Core");
    await waitFor(() => expect(projectsSpy.mock.calls.length).toBeGreaterThan(reads));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: keys.projects });
  });

  it("saves accent2 null when Ombre switches to Solid", async () => {
    renderSettings();
    const user = await shown();
    await user.click(screen.getByRole("radio", { name: "SOLID" }));
    await user.click(screen.getByRole("button", { name: "Save project" }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    const body = updateSpy.mock.calls[0][1] as Partial<Project>;
    expect(body.accent2).toBeNull();
    expect(body).not.toHaveProperty("accent");
    expect(body).not.toHaveProperty("pattern");
  });

  it("saves pattern none when the pattern is None", async () => {
    renderSettings();
    const user = await shown();
    await user.click(screen.getByRole("radio", { name: "None" }));
    await user.click(screen.getByRole("button", { name: "Save project" }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    const body = updateSpy.mock.calls[0][1] as Partial<Project>;
    expect(body.pattern).toBe("none");
    expect(body).not.toHaveProperty("accent");
    expect(body).not.toHaveProperty("accent2");
  });

  it("moves within each radio group with arrows, Home and End, and skips a disabled swatch", async () => {
    renderSettings();
    const user = await shown();
    const patterns = screen.getByRole("radiogroup", { name: "Pattern" });
    within(patterns).getByRole("radio", { name: "Paisley" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("Bubbles");
    expect(checkedIn("Pattern")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("None");
    await user.keyboard("{End}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("Bubbles");

    const to = screen.getByRole("radiogroup", { name: "Ombre, second colour" });
    expect(within(to).getByRole("radio", { name: "Lime, same as the first colour" })).toBeDisabled();
    within(to).getByRole("radio", { name: "Indigo" }).focus();
    await user.keyboard("{Home}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Mint");
    expect(checkedIn("Ombre, second colour")).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Yellow");
  });
});
