/**
 * PRD-47 S15 / GRPH-966 — Log export settings.
 *
 * The interesting assertions here are the absence ones. Every number on this panel is a count
 * that looks reassuring when it is actually unknown, so each test that checks a value also
 * checks that the reassuring reading is NOT on the page: a failed fetch must not arrive as
 * "off", an unavailable counter must not arrive as `0`, and a probe that has not run must not
 * arrive as one that passed.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import { TEST_NOT_RUN } from "@/features/settings/LogExportPanel";
import { SettingsView } from "@/features/settings/SettingsView";
import { settingsPath } from "@/lib/routes";
import type {
  LogExportSample,
  LogExportStatus,
  LogExportTestResult,
  LogExportView,
  Project,
} from "@/lib/types";

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

function sample(over: Partial<LogExportSample> = {}): LogExportSample {
  return {
    kind: "log",
    source: "event",
    note: "",
    timestamp: "2026-09-29T12:00:00+00:00",
    severity: "INFO",
    body: "create_item",
    attributes: {
      "gb.scope": "project",
      "gb.project": "APP",
      "gb.action": "create_item",
      "gb.api_key": "[masked]",
      "gb.summary": "[redacted]",
    },
    redaction: { summaries: true, client_ips: true, api_keys: true },
    ...over,
  };
}

function status(over: Partial<LogExportStatus> = {}): LogExportStatus {
  return {
    enabled: true,
    state: "exporting",
    state_note: "",
    exporter_running: true,
    last_batch: null,
    last_batch_state: "never",
    sent_24h: 12,
    dropped_24h: 0,
    queue_depth: 3,
    queue_state: "draining",
    coverage: "full",
    note: "",
    ...over,
  };
}

function payload(over: Partial<LogExportView> = {}): LogExportView {
  return {
    config: {
      enabled: true,
      endpoint: "http://localhost:4318",
      protocol: "http/protobuf",
      compression: "none",
      headers: [{ name: "Authorization", value: "Bear…cdef" }],
      send_events: true,
      send_tool_calls: true,
      send_heartbeats: false,
      event_types: [],
      redact_summaries: true,
      redact_client_ips: true,
      mask_api_keys: true,
      updated_at: "2026-09-29T12:00:00+00:00",
    },
    status: status(),
    sample: sample(),
    protocols: [
      { id: "http/protobuf", label: "HTTP / protobuf", supported: true, note: "OTLP/HTTP." },
      { id: "grpc", label: "gRPC", supported: false, note: "gRPC needs grpcio, which this build does not carry." },
    ],
    compressions: ["none", "gzip"],
    event_type_options: ["create_item", "sign_off_refused"],
    event_types_all: true,
    retention_note: "Exporting never removes anything. MCP call records are swept after 7 day(s).",
    catch_up_note: "Turning export on starts from now.",
    endpoint_problem: null,
    hosted: false,
    writable: true,
    ...over,
  };
}

const { logExportSpy, updateSpy, testBatchSpy, sampleSpy, projectsSpy, configSpy } = vi.hoisted(() => ({
  logExportSpy: vi.fn(),
  updateSpy: vi.fn(),
  testBatchSpy: vi.fn(),
  sampleSpy: vi.fn(),
  projectsSpy: vi.fn(),
  configSpy: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: projectsSpy,
    config: configSpy,
    logExport: logExportSpy,
    updateLogExport: updateSpy,
    logExportTestBatch: testBatchSpy,
    logExportSample: sampleSpy,
    credentials: vi.fn(async () => []),
    platform: vi.fn(async () => null),
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
    members: vi.fn(async () => []),
    apiKeys: vi.fn(async () => []),
    gitops: vi.fn(async () => ({ fields: {}, control: { state: "local", writable: true, message: "" } })),
    updateCheck: vi.fn(async () => null),
  },
}));

function renderPage(path = settingsPath("deployment/log-export"), hosted = false) {
  configSpy.mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/settings/*" element={<SettingsView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The status card, so an assertion about a counter cannot be satisfied by prose elsewhere. */
function statusCard() {
  return screen.getByText("Sent 24h").closest("div.rounded-\\[13px\\]") as HTMLElement;
}

/** One strip cell. Scoped because a measured zero elsewhere on the strip is legitimate — the
 *  claim is always about THIS number, and a card-wide assertion would be satisfied by any of
 *  them. */
function metricCell(card: HTMLElement, label: string) {
  return within(card).getByText(label).parentElement as HTMLElement;
}

describe("Log export settings page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("gb_last_project_tag", "APP");
    projectsSpy.mockResolvedValue([proj]);
    configSpy.mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
    logExportSpy.mockResolvedValue(payload());
    sampleSpy.mockResolvedValue({ sample: sample(), redaction: sample().redaction });
    updateSpy.mockResolvedValue(payload());
    testBatchSpy.mockResolvedValue({ ok: true, ran: true, error: "", detail: "3 of 3 record(s) accepted in 41 ms.", records: 3, latency_ms: 41, endpoint: "http://localhost:4318" } satisfies LogExportTestResult);
  });

  // ── placement ────────────────────────────────────────────────────────────

  it("sits under This box as Log export", async () => {
    renderPage();
    expect(await screen.findByText("This box")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Log export" });
    expect(link).toHaveAttribute("href", settingsPath("deployment/log-export"));
  });

  it("the nav link lives in the This box group, not the This project one", async () => {
    renderPage();
    const link = await screen.findByRole("link", { name: "Log export" });
    // The nav renders one container per group, with the heading inside it.
    const group = link.parentElement as HTMLElement;
    expect(group.textContent).toContain("This box");
    expect(group.textContent).not.toContain("This project");
    expect(within(group).getByRole("link", { name: "Updates" })).toBeInTheDocument();
  });

  it("shows the panel at its own path", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Log export" })).toBeInTheDocument();
    expect(screen.getByLabelText("Collector endpoint")).toHaveValue("http://localhost:4318");
  });

  it("docs overlay matches Log export before the /settings catch-all", () => {
    const doc = docFor(settingsPath("deployment/log-export"));
    expect(doc.title).toBe("Log export");
    expect(doc.badge).toBe("LOG EXPORT");
    expect(docFor("/settings").title).toBe("Settings");
  });

  it("hosted settles on the redirect, so a tenant never gets the panel", async () => {
    renderPage(settingsPath("deployment/log-export"), true);
    // `SettingsView` reads hosted_mode from /api/config, so the self-host nav is what paints
    // first on ANY deployment — that is pre-existing behaviour for every pane, not this one's.
    // What must hold is where it settles: no panel, no nav entry, no "This box".
    await waitFor(() => expect(screen.queryByText("This box")).not.toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Log export" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Log export" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Collector endpoint")).not.toBeInTheDocument();
  });

  // ── the state floor: a failed read is not "off" ───────────────────────────

  it("a failed fetch is an error affordance, not a panel saying off", async () => {
    logExportSpy.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/The request failed/)).toBeInTheDocument();
    expect(screen.getByText(/this is not "off"/)).toBeInTheDocument();
    // The reassuring empty must not be on the page at all.
    expect(screen.queryByText("Paused")).not.toBeInTheDocument();
    expect(screen.queryByText("Exporting")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Log export" })).not.toBeInTheDocument();
    expect(screen.queryAllByText("0")).toHaveLength(0);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("unknown is a state of its own, not paused and not exporting", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ enabled: null, state: "unknown", state_note: "The export configuration could not be read, so this panel does not know whether export is on. That is not 'off'.", coverage: "unavailable", sent_24h: null, dropped_24h: null, queue_depth: null, queue_state: "unavailable" }) }),
    );
    renderPage();
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("Paused")).not.toBeInTheDocument();
    expect(screen.queryByText("Exporting")).not.toBeInTheDocument();
    expect(screen.getByText(/does not know whether export is on/i)).toBeInTheDocument();
  });

  it("paused and on-but-not-draining are different sentences", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ enabled: false, state: "paused", exporter_running: false, queue_depth: null, queue_state: "exporter_not_running", state_note: "Export is off." }) }),
    );
    const { unmount } = renderPage();
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("Export is off.")).toBeInTheDocument();
    unmount();

    logExportSpy.mockResolvedValue(
      payload({ status: status({ state: "not_running", exporter_running: false, queue_depth: null, queue_state: "exporter_not_running", state_note: "Export is ON but no exporter is running in this process." }) }),
    );
    renderPage();
    expect(await screen.findByText("On, but nothing is draining")).toBeInTheDocument();
    expect(screen.queryByText("Paused")).not.toBeInTheDocument();
  });

  // ── the state floor: unavailable is an em dash, never a zero ──────────────

  it("measured zeros render as 0 and no em dash", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ sent_24h: 0, dropped_24h: 0, queue_depth: 0 }) }),
    );
    renderPage();
    await screen.findByText("Exporting");
    const card = statusCard();
    expect(within(card).getAllByText("0")).toHaveLength(3);
    expect(within(card).queryAllByText("—")).toHaveLength(0);
    expect(screen.queryByText(/shown as unknown/i)).not.toBeInTheDocument();
  });

  it("unavailable counters render as an em dash and never as a zero", async () => {
    logExportSpy.mockResolvedValue(
      payload({
        status: status({
          sent_24h: null,
          dropped_24h: null,
          queue_depth: null,
          queue_state: "unavailable",
          coverage: "unavailable",
          note: "Could not read 24h counters (RuntimeError), so those are shown as unknown. That is not a zero, and it is not 'off'.",
        }),
      }),
    );
    renderPage();
    await screen.findByText("Exporting");
    const card = statusCard();
    expect(within(card).getAllByText("—")).toHaveLength(3);
    expect(within(card).queryAllByText("0")).toHaveLength(0);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText(/that is not a zero/i)).toBeInTheDocument();
  });

  it("queue depth is unknown while no exporter is running, and says why", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ state: "not_running", exporter_running: false, queue_depth: null, queue_state: "exporter_not_running" }) }),
    );
    renderPage();
    expect(await screen.findByText("On, but nothing is draining")).toBeInTheDocument();
    const cell = metricCell(statusCard(), "Queue depth");
    expect(within(cell).getByText("—")).toBeInTheDocument();
    expect(within(cell).queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByText(/Queue depth is unknown, not zero/i)).toBeInTheDocument();
    expect(screen.getByText(/nobody is working down/i)).toBeInTheDocument();
  });

  it("a failed last-batch read is unavailable, not never", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ last_batch: null, last_batch_state: "unavailable", coverage: "unavailable", note: "Could not read last batch (RuntimeError), so those are shown as unknown." }) }),
    );
    renderPage();
    expect(await screen.findByText("Exporting")).toBeInTheDocument();
    const card = statusCard();
    expect(within(card).getByText("Last batch")).toBeInTheDocument();
    expect(within(card).queryByText("never")).not.toBeInTheDocument();
    expect(within(card).getByText("—")).toBeInTheDocument();
  });

  it("never having run is its own answer", async () => {
    logExportSpy.mockResolvedValue(
      payload({ status: status({ last_batch: null, last_batch_state: "never", sent_24h: 0, dropped_24h: 0, queue_depth: 0 }) }),
    );
    renderPage();
    expect(await screen.findByText("Exporting")).toBeInTheDocument();
    const card = statusCard();
    expect(within(card).getByText("never")).toBeInTheDocument();
    expect(within(card).queryByText("unavailable")).not.toBeInTheDocument();
  });

  // ── Send test batch ───────────────────────────────────────────────────────

  it("an unrun probe does not look like a passed one", async () => {
    renderPage();
    expect(await screen.findByText(TEST_NOT_RUN)).toBeInTheDocument();
    expect(screen.queryByText(/Accepted\./)).not.toBeInTheDocument();
    expect(testBatchSpy).not.toHaveBeenCalled();
  });

  it("a portless endpoint is that specific failure, not a generic error", async () => {
    const user = userEvent.setup();
    testBatchSpy.mockResolvedValue({
      ok: false, ran: true, error: "no_port",
      detail: "No port in endpoint — collector unreachable",
      records: null, latency_ms: null, endpoint: "http://collector.internal",
    } satisfies LogExportTestResult);
    renderPage();
    await user.click(await screen.findByRole("button", { name: /send test batch/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("no_port")).toBeInTheDocument();
    expect(screen.getByText(/No port in endpoint — collector unreachable/)).toBeInTheDocument();
    expect(screen.queryByText(/Accepted\./)).not.toBeInTheDocument();
    // A failed probe reports no count. `0 records` would be a number nobody measured.
    expect(screen.queryByText(/0 record/)).not.toBeInTheDocument();
    expect(screen.queryByText(TEST_NOT_RUN)).not.toBeInTheDocument();
  });

  it("a reachable collector reports records and latency", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /send test batch/i }));
    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.getByText(/Accepted\./)).toBeInTheDocument();
    expect(screen.getByText(/3 of 3 record\(s\) accepted in 41 ms\./)).toBeInTheDocument();
    expect(screen.queryByText(TEST_NOT_RUN)).not.toBeInTheDocument();
  });

  it("THE CALL: the probe posts the endpoint on screen, not the saved one", async () => {
    const user = userEvent.setup();
    renderPage();
    const endpoint = await screen.findByLabelText("Collector endpoint");
    await user.clear(endpoint);
    await user.type(endpoint, "http://collector.internal");
    await user.click(screen.getByRole("button", { name: /send test batch/i }));
    await waitFor(() => expect(testBatchSpy).toHaveBeenCalledTimes(1));
    expect(testBatchSpy.mock.calls[0]![0]).toMatchObject({
      endpoint: "http://collector.internal",
      protocol: "http/protobuf",
      compression: "none",
    });
  });

  it("a probe that could not even be posted is a failure, not a return to not-run", async () => {
    const user = userEvent.setup();
    testBatchSpy.mockRejectedValue(new Error("network down"));
    renderPage();
    await user.click(await screen.findByRole("button", { name: /send test batch/i }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("request_failed")).toBeInTheDocument();
    expect(screen.queryByText(TEST_NOT_RUN)).not.toBeInTheDocument();
  });

  it("the saved endpoint problem is named inline before anyone presses anything", async () => {
    logExportSpy.mockResolvedValue(
      payload({ endpoint_problem: { error: "no_port", detail: "No port in endpoint — collector unreachable" } }),
    );
    renderPage();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("no_port")).toBeInTheDocument();
    expect(screen.getByLabelText("Collector endpoint")).toHaveAttribute("aria-invalid", "true");
  });

  it("gRPC is offered but disabled, with the reason this build cannot speak it", async () => {
    renderPage();
    const protocol = await screen.findByLabelText("Protocol");
    const grpc = within(protocol).getByRole("option", { name: /gRPC/ }) as HTMLOptionElement;
    expect(grpc.disabled).toBe(true);
    expect(grpc.textContent).toContain("not in this build");
    expect(protocol).toHaveValue("http/protobuf");
  });

  // ── what to send, and the live sample ─────────────────────────────────────

  it("names the three signals and the kind each becomes", async () => {
    renderPage();
    expect(await screen.findByText("Activity events")).toBeInTheDocument();
    expect(screen.getByText(/as logs/)).toBeInTheDocument();
    expect(screen.getByText("MCP tool calls")).toBeInTheDocument();
    expect(screen.getByText(/as traces/)).toBeInTheDocument();
    expect(screen.getByText("Agent heartbeats")).toBeInTheDocument();
    expect(screen.getByText(/as metrics/)).toBeInTheDocument();
  });

  it("an empty event-type filter says every action, not none", async () => {
    renderPage();
    expect(await screen.findByText(/Every action the ledger records/)).toBeInTheDocument();
    expect(screen.queryByText(/nothing to filter on/i)).not.toBeInTheDocument();
  });

  it("the event-type filter offers only actions this ledger records", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: "create_item" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sign_off_refused" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "an_action_never_recorded" })).not.toBeInTheDocument();
  });

  it("THE CALL: a redaction toggle re-asks the server with the unsaved choice", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Drop client IPs");
    expect(sampleSpy).toHaveBeenCalledWith({
      redact_summaries: true, redact_client_ips: true, mask_api_keys: true,
    });
    await user.click(screen.getByLabelText(/Drop client IPs/));
    await waitFor(() =>
      expect(sampleSpy).toHaveBeenCalledWith({
        redact_summaries: true, redact_client_ips: false, mask_api_keys: true,
      }));
  });

  it("the sample shows what the server returned, including an IP the toggle let through", async () => {
    const user = userEvent.setup();
    sampleSpy.mockResolvedValue({
      sample: sample({
        redaction: { summaries: true, client_ips: false, api_keys: true },
        attributes: { "gb.project": "APP", "gb.client_ip": "203.0.113.7", "gb.api_key": "[masked]" },
      }),
      redaction: { summaries: true, client_ips: false, api_keys: true },
    });
    renderPage();
    await user.click(await screen.findByLabelText(/Drop client IPs/));
    expect(await screen.findByText("gb.client_ip")).toBeInTheDocument();
    expect(screen.getByText("203.0.113.7")).toBeInTheDocument();
  });

  it("a synthetic sample says it is not your data", async () => {
    logExportSpy.mockResolvedValue(
      payload({
        sample: sample({
          source: "synthetic",
          note: "This ledger has no events yet, so this is the shape of a record, not one of yours.",
        }),
      }),
    );
    sampleSpy.mockResolvedValue({
      sample: sample({ source: "synthetic", note: "This ledger has no events yet, so this is the shape of a record, not one of yours." }),
      redaction: sample().redaction,
    });
    renderPage();
    expect(await screen.findByText(/the shape of a record, not one of yours/i)).toBeInTheDocument();
  });

  it("a failed sample read does not quietly show the saved one", async () => {
    sampleSpy.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findAllByText(/The request failed/)).not.toHaveLength(0);
    expect(screen.getByText(/The sample was not read/i)).toBeInTheDocument();
    // The record from the panel payload must not be rendered as if it were the live answer.
    expect(screen.queryByText("gb.summary")).not.toBeInTheDocument();
  });

  // ── saving ────────────────────────────────────────────────────────────────

  it("THE CALL: save posts the deployment config and no project id", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /save log export/i }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    const body = updateSpy.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toMatchObject({
      enabled: true,
      endpoint: "http://localhost:4318",
      protocol: "http/protobuf",
      compression: "none",
      send_events: true,
      send_tool_calls: true,
      send_heartbeats: false,
      redact_summaries: true,
      redact_client_ips: true,
      mask_api_keys: true,
    });
    // There is no project to scope this by, and a `project_id` here would be the per-project
    // config the item refuses.
    expect("project_id" in body).toBe(false);
    // Headers go as a map, and blank rows are not sent as empty-named headers.
    expect(body.headers).toEqual({ Authorization: "Bear…cdef" });
    expect("updated_at" in body).toBe(false);
  });

  it("a save that reports catch-up says so on the page", async () => {
    const user = userEvent.setup();
    updateSpy.mockResolvedValue({ ...payload(), notes: { catch_up: "from_now", catch_up_note: "Turning export on starts from now." } });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /save log export/i }));
    expect(await screen.findByText("Turning export on starts from now.")).toBeInTheDocument();
  });

  it("a refused save shows the server's reason", async () => {
    const user = userEvent.setup();
    updateSpy.mockRejectedValue(new Error(JSON.stringify({ detail: "protocol must be one of http/protobuf, grpc" })));
    renderPage();
    await user.click(await screen.findByRole("button", { name: /save log export/i }));
    expect(await screen.findByText(/protocol must be one of/i)).toBeInTheDocument();
    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("the paused note reports retention rather than promising export deletes nothing silently", async () => {
    renderPage();
    expect(await screen.findByText(/Exporting never removes anything/)).toBeInTheDocument();
    expect(screen.getByText(/swept after 7 day\(s\)/)).toBeInTheDocument();
  });
});

describe("Log export nav source", () => {
  const sources = import.meta.glob("../features/settings/SettingsView.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;
  const src = Object.values(sources)[0] ?? "";

  it("adds Log export under This box and not under This project", () => {
    const boxStart = src.indexOf('group: "This box"');
    const projectStart = src.indexOf('group: "This project"');
    const navEnd = src.indexOf("function SelfHostSettings");
    expect(boxStart).toBeGreaterThan(-1);
    expect(projectStart).toBeGreaterThan(boxStart);
    expect(navEnd).toBeGreaterThan(projectStart);

    const box = src.slice(boxStart, projectStart);
    const project = src.slice(projectStart, navEnd);
    expect(box).toContain('settingsPath("deployment/log-export")');
    expect(box).toContain('label: "Log export"');
    // The pin the item asks for: the same entry must NOT appear in the project group.
    expect(project).not.toContain("log-export");
    expect(project).not.toContain("Log export");
  });
});
