/**
 * Every live page must get its own overlay. A catch-all that opens AI Providers
 * on API keys (and Graphban-default on /p/:tag/tracker) is the failure this pins.
 */
import { describe, expect, it } from "vitest";

import { docFor } from "@/features/docs/content";
import { adminPath, orgPath, settingsPath } from "@/lib/routes";

const PAGES: [string, string][] = [
  ["/tracker", "Tracker"],
  ["/p/CORE/tracker", "Tracker"],
  ["/triage", "Triage"],
  ["/p/CORE/triage", "Triage"],
  ["/requests", "Requests"],
  ["/p/CORE/requests", "Requests"],
  ["/prds", "PRDs"],
  ["/p/CORE/prds", "PRDs"],
  ["/prds/abc", "PRD editor"],
  ["/p/CORE/prds/abc", "PRD editor"],
  ["/roadmap", "Roadmap"],
  ["/p/CORE/roadmap", "Roadmap"],
  ["/code", "Code graph"],
  ["/p/CORE/code", "Code graph"],
  ["/links", "Links graph"],
  ["/p/CORE/links", "Links graph"],
  ["/fleet", "Fleet"],
  ["/p/CORE/fleet", "Fleet"],
  ["/activity", "Activity"],
  ["/p/CORE/activity", "Activity"],
  ["/memory-review", "Memory review"],
  ["/p/CORE/memory-review", "Memory review"],
  ["/lessons", "Lessons"],
  ["/lessons/sh_1", "Lessons"],
  ["/p/CORE/lessons", "Lessons"],
  ["/p/CORE/lessons/sh_1", "Lessons"],
  ["/home", "Home"],
  ["/dashboard", "Dashboard"],
  ["/p/CORE/dashboard", "Dashboard"],
  ["/p/CORE", "Project home"],
  ["/p/CORE/", "Project home"],
  ["/mcp-tools", "MCP Tools"],
  ["/p/CORE/mcp-tools", "MCP Tools"],
  ["/feedback-kit", "Feedback Kit"],
  ["/p/CORE/feedback-kit", "Feedback Kit"],
  ["/profile", "Profile"],
  ["/admin", "Operator"],
  ["/admin/orgs", "Operator"],
  ["/admin/users", "Operator"],
  ["/admin/licensing", "Operator"],
  [settingsPath("project/api-keys"), "API keys"],
  ["/p/CORE/settings/project/api-keys", "API keys"],
  ["/p/CORE/settings/deployment/sync", "Cloud / Sync"],
  ["/p/CORE/settings/account", "Account"],
  [settingsPath("project/mcp"), "MCP Tools"],
  [settingsPath("project/feedback-kit"), "Feedback Kit"],
  [settingsPath("project/providers"), "AI providers"],
  [settingsPath("deployment/providers"), "AI providers"],
  ["/p/CORE/settings/deployment/providers", "AI providers"],
  [settingsPath("project/integrations"), "Integrations"],
  [settingsPath("project/members"), "Members"],
  [settingsPath("project"), "Project"],
  [settingsPath("account"), "Account"],
  [settingsPath("deployment/sync"), "Cloud / Sync"],
  [settingsPath("deployment/gitops"), "Gitops"],
  [settingsPath("deployment/updates"), "Updates"],
  ["/settings", "Settings"],
  [orgPath(), "Organization"],
  [orgPath("galaxy"), "Galaxy"],
  [adminPath("gitops"), "Org gitops"],
  [adminPath("users"), "Users & access"],
  [adminPath("teams"), "Teams"],
  [adminPath("deployments"), "Deployments"],
  [adminPath("integrations"), "Org integrations"],
  [adminPath("billing"), "Billing"],
  [adminPath("branding"), "Branding"],
  [adminPath(), "Users & access"],
];

describe("docs overlay routes", () => {
  it("maps every live page, including the hosted /p/:tag prefix", () => {
    for (const [path, title] of PAGES) {
      expect(docFor(path).title, path).toBe(title);
    }
  });

  it("does not let the /settings catch-all steal a settings subpage", () => {
    // THE CALL. startsWith("/settings") used to win for every settings path.
    const stolen = [
      settingsPath("project/api-keys"),
      settingsPath("project/providers"),
      settingsPath("deployment/providers"),
      settingsPath("project/integrations"),
      settingsPath("project/members"),
      settingsPath("project"),
      settingsPath("account"),
      settingsPath("deployment/sync"),
      settingsPath("deployment/gitops"),
      settingsPath("deployment/updates"),
      settingsPath("project/mcp"),
      settingsPath("project/feedback-kit"),
    ];
    for (const path of stolen) {
      expect(docFor(path).title, path).not.toBe("Settings");
    }
    expect(docFor("/settings").title).toBe("Settings");
  });

  it("Cloud / Sync overlay is about minting a link key in the cloud org", () => {
    const d = docFor(settingsPath("deployment/sync"));
    expect(d.tagline).toMatch(/cloud org/i);
    expect(d.sections[0]?.h).toMatch(/cloud org/i);
    expect(d.sections.map((s) => s.h).join(" ")).not.toMatch(/Incremental graph/);
    expect(d.sections[0]?.b).toMatch(/sync/i);
  });

  it("org Deployments overlay names minting a link key", () => {
    const d = docFor(adminPath("deployments"));
    expect(d.sections[0]?.h).toMatch(/link key/i);
  });

  it("does not open AI Providers on a page that is not providers", () => {
    const notProviders = [
      settingsPath("project/api-keys"),
      settingsPath("deployment/sync"),
      settingsPath("project"),
      settingsPath("project/members"),
      "/fleet",
      "/tracker",
    ];
    for (const path of notProviders) {
      expect(docFor(path).title, path).not.toBe("AI providers");
      expect(docFor(path).sections[0]?.h ?? "", path).not.toMatch(/AI Providers/);
    }
  });

  it("the sitting is one document across Updates, Gitops, and Tracker", () => {
    // THE CALL. Related links and sitting copy are the operator document.
    // Deleting Tracker from Gitops related, or the sitting section, must fail here.
    const gitops = docFor(settingsPath("deployment/gitops"));
    const updates = docFor(settingsPath("deployment/updates"));
    const tracker = docFor("/tracker");
    const hostedTracker = docFor("/p/CORE/tracker");

    expect(gitops.related?.map((r) => r.label)).toEqual(
      expect.arrayContaining(["Updates", "Tracker"]),
    );
    expect(updates.related?.map((r) => r.label)).toEqual(
      expect.arrayContaining(["Gitops", "Tracker"]),
    );
    expect(tracker.related?.map((r) => r.label)).toEqual(
      expect.arrayContaining(["Gitops"]),
    );
    expect(hostedTracker.related?.map((r) => r.label)).toEqual(
      expect.arrayContaining(["Gitops"]),
    );

    expect(gitops.sections.find((s) => /named models/i.test(s.h))?.b).toMatch(
      /Custom, not Unmeasured/,
    );

    const sitting = gitops.sections.find((s) => /sitting/i.test(s.h));
    expect(sitting?.b).toMatch(/PRs to base/);
    expect(sitting?.b).toMatch(/Updates/);
    expect(sitting?.b).toMatch(/Tracker/);
    expect(sitting?.b).toMatch(/get_context/);

    const box = updates.sections.find((s) => /this box/i.test(s.h));
    expect(box?.b).toMatch(/Gitops/);
    expect(box?.b).toMatch(/not the same record/);

    const checklist = tracker.sections.find((s) => /graduation checklist/i.test(s.h));
    expect(checklist?.b).toMatch(/observe the remote/i);
    expect(checklist?.b).toMatch(/does not run git/i);
    expect(checklist?.b).toMatch(/process, not code/i);
    expect(checklist?.b).toMatch(/stall in review/);
    expect(checklist?.b).toMatch(/P23/);

    const org = docFor(adminPath("gitops"));
    expect(org.sections.map((s) => s.h).join(" ")).not.toMatch(/sitting/i);
    const house = org.sections.find((s) => /house process/i.test(s.h));
    expect(house?.b).toMatch(/Custom, not Unmeasured/);
    expect(house?.b).toMatch(/does not file a graduation checklist/);
  });

  it("Fleet overlay talks seats, not gate keys", () => {
    // Gate keys are minted on API keys. Naming them on Fleet is the two-pages
    // mix that sends an operator to mint the wrong object.
    const fleet = docFor("/fleet");
    const body = fleet.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/seat/i);
    expect(body).not.toMatch(/gate key/i);
    expect(fleet.related?.some((r) => r.label === "API keys")).toBe(true);
  });

  it("API keys overlay says a seat is not a key", () => {
    const keys = docFor(settingsPath("project/api-keys"));
    const body = keys.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/seat/i);
    expect(keys.related?.some((r) => r.label === "Fleet")).toBe(true);
  });

  it("MCP Tools overlay does not pretend this page mints keys", () => {
    const mcp = docFor("/mcp-tools");
    const body = mcp.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/does not mint keys/);
    expect(body).toMatch(/seat/i);
    expect(mcp.related?.some((r) => r.label === "API keys")).toBe(true);
  });
});
