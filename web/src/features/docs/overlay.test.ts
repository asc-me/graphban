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
  [adminPath("users/roles"), "Roles & permissions"],
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

  it("Cloud / Sync paste overlay names Link key, not a credential", () => {
    const d = docFor(settingsPath("deployment/sync"));
    const paste = d.sections.find((s) => /paste/i.test(s.h));
    expect(paste?.b).toMatch(/Link key/);
    expect(paste?.b).toMatch(/not a credential/);
    expect(paste?.b).toMatch(/--api-key/);
    expect(paste?.b).not.toMatch(/Sync API key/i);
  });

  it("org Deployments overlay names minting a link key", () => {
    const d = docFor(adminPath("deployments"));
    expect(d.sections[0]?.h).toMatch(/link key/i);
    const body = d.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).not.toMatch(/sync credential/i);
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
    expect(sitting?.b).toMatch(/Release defined in/);
    expect(sitting?.b).toMatch(/docs\/release\.md/);
    expect(sitting?.b).toMatch(/CalVer/);
    expect(sitting?.b).toMatch(/does not fetch/);

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

  it("Org gitops overlay names a link key, not a deployment credential", () => {
    const org = docFor(adminPath("gitops"));
    const linked = org.sections.find((s) => /linked boxes/i.test(s.h));
    expect(linked?.b).toMatch(/link key/);
    expect(linked?.b).not.toMatch(/deployment credential/i);
    expect(linked?.b).not.toMatch(/sync credential/i);
  });

  it("AI providers overlay names task-model inherit, not a missing judge", () => {
    const selfHost = docFor(settingsPath("deployment/providers"));
    const hosted = docFor(settingsPath("project/providers"));
    const tagged = docFor("/p/CORE/settings/deployment/providers");
    for (const d of [selfHost, hosted, tagged]) {
      const task = d.sections.find((s) => /task models/i.test(s.h));
      expect(task?.b).toMatch(/inherits this project's chat/);
      expect(task?.b).toMatch(/not 'no judge'/);
      expect(task?.b).toMatch(/ungraded, not a quieter model/);
    }
  });

  it("AI providers overlay names pending validation as not unreachable", () => {
    const selfHost = docFor(settingsPath("deployment/providers"));
    const hosted = docFor(settingsPath("project/providers"));
    const tagged = docFor("/p/CORE/settings/deployment/providers");
    for (const d of [selfHost, hosted, tagged]) {
      const health = d.sections.find((s) => /health is of the credential/i.test(s.h));
      expect(health?.b).toMatch(/pending validation is not unreachable/i);
      expect(health?.b).toMatch(/nobody asked yet/);
      expect(health?.b).toMatch(/Test connection is the ask/);
      expect(health?.b).toMatch(/pending cannot be default/);
    }
  });

  it("Updates Install names the packed tarball, not a source zip", () => {
    const d = docFor(settingsPath("deployment/updates"));
    const install = d.sections.find((s) => /check and install/i.test(s.h));
    expect(install?.b).toMatch(/graphban-<tag>\.tar\.gz/);
    expect(install?.b).toMatch(/source zip/i);
  });

  it("Billing overlay names two modes, not No Stripe here", () => {
    const d = docFor(adminPath("billing"));
    expect(d.sections.map((s) => s.h).join(" ")).not.toMatch(/No Stripe here/i);
    const mode = d.sections.find((s) => /two modes/i.test(s.h));
    expect(mode?.b).toMatch(/not a broken billing page/);
    expect(mode?.b).toMatch(/Checkout/);
    expect(mode?.b).toMatch(/no invoice list/);
    expect(mode?.b).toMatch(/Enterprise/);
    expect(mode?.b).toMatch(/Pro\/Team only/);
    expect(mode?.b).toMatch(/not a missing button/);
    expect(d.tagline).not.toMatch(/Display only/i);
  });

  it("Updates overlay names stamp/publish as not apply", () => {
    const selfHost = docFor(settingsPath("deployment/updates"));
    const tagged = docFor("/p/CORE/settings/deployment/updates");
    for (const d of [selfHost, tagged]) {
      const install = d.sections.find((s) => /check and install/i.test(s.h));
      expect(install?.b).toMatch(/do not apply/);
      expect(install?.b).toMatch(/operator gate/);
    }
  });

  it("Updates overlay names empty notes as not a failed fetch", () => {
    const d = docFor(settingsPath("deployment/updates"));
    const notes = d.sections.find((s) => /release notes/i.test(s.h));
    expect(notes?.b).toMatch(/empty body is no notes/i);
    expect(notes?.b).toMatch(/failed fetch is could not load/i);
    expect(notes?.b).toMatch(/not a rollup/i);
  });

  it("Updates overlay names empty via as not compose", () => {
    const selfHost = docFor(settingsPath("deployment/updates"));
    const tagged = docFor("/p/CORE/settings/deployment/updates");
    for (const d of [selfHost, tagged]) {
      const install = d.sections.find((s) => /check and install/i.test(s.h));
      expect(install?.b).toMatch(/could not tell/);
      expect(install?.b).toMatch(/empty via is not compose/);
    }
  });

  it("PRD editor overlay names empty headings as not a clean pass", () => {
    const d = docFor("/prds/abc");
    const cov = d.sections.find((s) => /coverage/i.test(s.h));
    expect(cov?.b).toMatch(/empty/i);
    expect(cov?.b).toMatch(/no sections is not a clean pass/i);
    expect(cov?.b).not.toMatch(/\d+%/);
  });

  it("PRD editor overlay names Fill gaps as gated on approved", () => {
    const d = docFor("/prds/abc");
    const cov = d.sections.find((s) => /coverage/i.test(s.h));
    expect(cov?.b).toMatch(/grill earns approved/);
    expect(cov?.b).toMatch(/draft is not a task list/);
  });

  it("PRD editor overlay names Readiness to approve as a warning, not a gate", () => {
    const d = docFor("/prds/abc");
    const ready = d.sections.find((s) => /readiness to approve/i.test(s.h));
    expect(ready?.b).toMatch(/not a gate/);
    expect(ready?.b).toMatch(/ungraded/i);
    expect(ready?.b).toMatch(/not asking is not a pass/);
    expect(ready?.b).not.toMatch(/\d+%/);
  });

  it("Memory review overlay names list judging and that a missing score is not clean", () => {
    const ask = docFor("/memory-review").sections.find((s) => /llm judge/i.test(s.h));
    expect(ask?.b).toMatch(/capped/);
    expect(ask?.b).toMatch(/never that the note is fine/);
    expect(ask?.b).toMatch(/keep\/quality/);
    expect(ask?.b).not.toMatch(/nobody asked/);
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

  it("API keys overlay names Minted with, not gone after the banner", () => {
    const keys = docFor(settingsPath("project/api-keys"));
    const minted = keys.sections.find((s) => /minted with/i.test(s.h));
    expect(minted?.b).toMatch(/scopes/);
    expect(minted?.b).toMatch(/agent keys only/);
    expect(minted?.b).toMatch(/core-only/);
    expect(minted?.b).toMatch(/not missing/);
    expect(keys.sections.find((s) => /shown once/i.test(s.h))?.b).toMatch(/shown once/);
  });

  it("API keys overlay says a seat is not a key", () => {
    const keys = docFor(settingsPath("project/api-keys"));
    const body = keys.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/seat/i);
    expect(keys.related?.some((r) => r.label === "Fleet")).toBe(true);
    expect(keys.related?.some((r) => r.label === "AI providers")).toBe(true);
  });

  it("API keys overlay names Graphban keys, not LLM credentials", () => {
    const keys = docFor(settingsPath("project/api-keys"));
    expect(keys.tagline).toMatch(/not LLM credentials/);
    expect(keys.sections.find((s) => /three kinds/i.test(s.h))?.b).toMatch(
      /Looking for LLM credentials/,
    );
  });

  it("API keys overlay names a link key, not a sync credential", () => {
    // Sync / Link already mints a "link key". The API keys kind used to say
    // "sync credential" for the same object — two names, one mint.
    const keys = docFor(settingsPath("project/api-keys"));
    const body = keys.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/link key/i);
    expect(body).not.toMatch(/sync credential/i);
    const sync = docFor(settingsPath("deployment/sync"));
    expect(sync.sections[0]?.b).toMatch(/Link key/);
    expect(sync.sections[0]?.b).not.toMatch(/Sync credential/);
  });

  it("AI providers overlay sends Graphban keys to API keys, not this page", () => {
    // This page is LLM credentials. Agent keys live on API keys — the same
    // two-pages mix Fleet's "Looking for MCP?" exists to stop.
    const providers = docFor(settingsPath("deployment/providers"));
    expect(providers.tagline).toMatch(/not Graphban API keys/);
    expect(providers.related?.some((r) => r.label === "API keys")).toBe(true);
  });

  it("AI providers overlay names Provider key, not the Graphban API key field", () => {
    const selfHost = docFor(settingsPath("deployment/providers"));
    const hosted = docFor(settingsPath("project/providers"));
    const tagged = docFor("/p/CORE/settings/deployment/providers");
    for (const d of [selfHost, hosted, tagged]) {
      const box = d.sections.find((s) => /this box/i.test(s.h));
      expect(box?.b).toMatch(/Provider key/);
      expect(box?.b).toMatch(/Looking for API keys/);
    }
  });

  it("MCP Tools overlay does not pretend this page mints keys", () => {
    const mcp = docFor("/mcp-tools");
    const body = mcp.sections.map((s) => `${s.h} ${s.b}`).join(" ");
    expect(body).toMatch(/does not mint keys/);
    expect(body).toMatch(/seat/i);
    expect(mcp.related?.some((r) => r.label === "API keys")).toBe(true);
  });

  it("Org roles overlay is not Users, and fleet roles are a seat not a credential", () => {
    // THE CALL. users/roles used to startWith(users) and open the seat-meter overlay.
    const roles = docFor(adminPath("users/roles"));
    expect(roles.title).toBe("Roles & permissions");
    expect(roles.title).not.toBe("Users & access");
    const people = roles.sections.find((s) => /people/i.test(s.h));
    expect(people?.b).toMatch(/seat/);
    expect(people?.b).toMatch(/not a credential/);
    expect(docFor(adminPath("users")).title).toBe("Users & access");
  });

  it("MCP Tools overlay names Looking for API keys, not an LLM secret", () => {
    for (const path of ["/mcp-tools", settingsPath("project/mcp")]) {
      const auth = docFor(path).sections.find((s) => /authenticate/i.test(s.h));
      expect(auth?.b, path).toMatch(/Looking for API keys/);
      expect(auth?.b, path).toMatch(/not an LLM secret/);
    }
  });
});
