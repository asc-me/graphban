import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FeedbackKitView } from "@/features/feedback/FeedbackKitView";
import { initialDnsResults, measureDnsRecords } from "@/features/feedback/dnsCheck";
import { ProjectProvider } from "@/features/ProjectContext";
import type { PlatformConfig, Project } from "@/lib/types";

const proj: Project = {
  id: "prj_a",
  tag: "APP",
  name: "App",
  accent: "#c6f24e",
  visibility: "private",
  description: "",
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

const platform = {
  project_id: "prj_a",
  llm_mode: "local" as const,
  local_base_url: "",
  local_model: "",
  cloud_provider: "",
  cloud_model: "",
  github_connected: false,
  github_account: "",
  github_repo: "",
  github_scope: "",
  gdrive_connected: false,
  gdrive_account: "",
  gdrive_folder: "",
  rate_limit_per_min: 60,
  turnstile_sitekey: "site-key",
  turnstile_secret_set: true,
  active_chat_provider: "stub",
  effective_chat_provider: "stub",
  provider_config: {},
  public_share_enabled: false,
  intake_enabled: true,
  public_form_enabled: false,
  public_roadmap_enabled: false,
  public_issues_enabled: false,
  public_requests_enabled: false,
  capture_identity: false,
  share_token: "share_xyz",
  ingest_token_prefix: "ing_abc",
  sync_graph: false,
} satisfies PlatformConfig;

const { projectsSpy, platformSpy } = vi.hoisted(() => ({
  projectsSpy: vi.fn(async () => [proj]),
  platformSpy: vi.fn(async () => platform),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    platform: platformSpy,
  },
}));

function renderKit() {
  localStorage.setItem("gb_last_project_tag", "APP");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProjectProvider>
          <FeedbackKitView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Feedback Kit Setup tab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    projectsSpy.mockResolvedValue([proj]);
    platformSpy.mockResolvedValue(platform);
  });

  it("switches between Customize and Setup tabs", async () => {
    const user = userEvent.setup();
    renderKit();

    expect(await screen.findByRole("button", { name: "customize" })).toHaveClass("bg-surface-4");
    expect(screen.getByText("Embed mode")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "setup" }));
    expect(screen.getByRole("button", { name: "setup" })).toHaveClass("bg-surface-4");
    expect(
      screen.getByText(/This deployment runs on a private host, so people on your website can't send to it directly/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Embed mode")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "customize" }));
    expect(screen.getByText("Embed mode")).toBeInTheDocument();
  });

  it("shows unknown DNS status until Check DNS runs", async () => {
    const user = userEvent.setup();
    renderKit();

    await user.click(await screen.findByRole("button", { name: "setup" }));
    await user.click(screen.getByRole("button", { name: /Relay through the cloud/i }));
    await user.click(screen.getByRole("button", { name: /Custom domain/i }));

    const table = screen.getByRole("table");
    expect(within(table).getAllByText("unknown")).toHaveLength(2);
    expect(within(table).getAllByTitle("Not checked yet")).toHaveLength(2);

    await user.click(screen.getByRole("button", { name: /Check DNS/i }));

    expect(within(table).getAllByText("unknown")).toHaveLength(2);
    expect(
      within(table).getAllByTitle("Check ran — no DNS verification endpoint on this deployment yet"),
    ).toHaveLength(2);
  });
});

describe("measureDnsRecords", () => {
  const records = [
    {
      type: "CNAME" as const,
      name: "feedback.example.com",
      value: "host.example.com",
      purpose: "Points the widget's submit URL at this deployment",
    },
    {
      type: "TXT" as const,
      name: "_graphban-verify.example.com",
      value: "token",
      purpose: "Proves ownership so TLS can be issued",
    },
  ];

  it("starts unknown with an explicit not-checked detail", () => {
    const initial = initialDnsResults(records);
    expect(initial["CNAME-feedback.example.com"]).toEqual({ status: "unknown", detail: "Not checked yet" });
    expect(initial["TXT-_graphban-verify.example.com"]).toEqual({ status: "unknown", detail: "Not checked yet" });
  });

  it("reports unknown with what was measured when no endpoint exists", () => {
    const measured = measureDnsRecords(records);
    expect(measured["CNAME-feedback.example.com"].status).toBe("unknown");
    expect(measured["CNAME-feedback.example.com"].detail).toMatch(/Check ran/);
    expect(measured["TXT-_graphban-verify.example.com"].detail).toMatch(/no DNS verification endpoint/);
  });
});
