import { useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, Copy, Github, HardDrive, KeyRound, Plug, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { FeedbackKitView } from "@/features/feedback/FeedbackKitView";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { CredentialsPanel } from "@/features/settings/CredentialsPanel";
import { GitopsPanel } from "@/features/settings/GitopsPanel";
import { UpdatesPanel } from "@/features/settings/UpdatesPanel";
import { McpInstall, type KeyScope } from "@/features/settings/McpInstall";
import { CloudOrgLinkPanel } from "@/features/settings/CloudOrgLinkPanel";
import { SyncCredentialInstall } from "@/features/settings/SyncCredentialInstall";
import { SyncLinkPanel } from "@/features/settings/SyncLinkPanel";
import { useProjectCtx } from "@/features/ProjectContext";
import { copyText } from "@/lib/clipboard";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { keys, useApiKeys, useConfig, useMembers, usePlatform } from "@/lib/queries";
import { adminPath, settingsPath } from "@/lib/routes";
import type { ApiKey, PlatformConfig, Project } from "@/lib/types";
import { Link, NavLink, Navigate, useLocation } from "react-router-dom";

export function SettingsView() {
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  if (hosted) return <HostedSettingsTabs />;
  return <SelfHostSettings />;
}

const SELF_HOST_NAV: { group: string; items: { to: string; label: string; end?: boolean }[] }[] = [
  {
    // "This box" is the deployment section: what this instance RUNS ON belongs here, not
    // per-project (GRPH-625). Credentials already resolve deployment-default-first with
    // project pointers on top — the nav said "this project" while the resolution said
    // "this box".
    group: "This box",
    items: [
      { to: settingsPath("deployment/providers"), label: "AI providers" },
      { to: settingsPath("deployment/sync"), label: "Cloud / Sync" },
      { to: settingsPath("deployment/gitops"), label: "Gitops" },
      { to: settingsPath("deployment/updates"), label: "Updates" },
    ],
  },
  {
    group: "This project",
    items: [
      { to: settingsPath("project"), label: "Project", end: true },
      { to: settingsPath("project/api-keys"), label: "API keys" },
      // Not "MCP": Fleet's "Looking for MCP?" is the connect snippet, on API keys.
      { to: settingsPath("project/mcp"), label: "MCP Tools" },
      { to: settingsPath("project/integrations"), label: "Integrations" },
      { to: settingsPath("project/feedback-kit"), label: "Feedback Kit" },
      { to: settingsPath("project/members"), label: "Members" },
    ],
  },
];

function SelfHostSettings() {
  const { pathname } = useLocation();
  if (pathname === "/settings" || pathname === "/settings/") {
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">This project and this box.</p>
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-[200px_1fr]">
        <div className="flex flex-col gap-0.5 overflow-y-auto border-r border-line p-3">
          {SELF_HOST_NAV.map((g) => (
            <div key={g.group} className="mb-2">
              <div className="mb-1 px-3 font-mono text-[10px] uppercase tracking-wide text-faint">
                {g.group}
              </div>
              {g.items.map((it) => (
                <NavLink
                  key={it.to}
                  to={it.to}
                  end={it.end}
                  className={({ isActive }) =>
                    cn(
                      "block rounded-[9px] px-3 py-2 text-[13px] transition-colors",
                      isActive ? "bg-surface-3 text-fg" : "text-muted hover:bg-surface-3 hover:text-fg-2",
                    )
                  }
                >
                  {it.label}
                </NavLink>
              ))}
            </div>
          ))}
          <div className="mt-auto border-t border-line pt-2">
            <NavLink
              to={settingsPath("account")}
              className={({ isActive }) =>
                cn(
                  "block rounded-[9px] px-3 py-2 text-[13px] transition-colors",
                  isActive ? "bg-surface-3 text-fg" : "text-muted hover:bg-surface-3 hover:text-fg-2",
                )
              }
            >
              Account
            </NavLink>
          </div>
        </div>
        <div className="min-h-0 overflow-y-auto p-6">
          <SelfHostPane pathname={pathname} />
        </div>
      </div>
    </div>
  );
}

function SelfHostPane({ pathname }: { pathname: string }) {
  if (pathname.startsWith(settingsPath("deployment/gitops"))) return <GitopsPanel />;
  if (pathname.startsWith(settingsPath("deployment/updates"))) return <UpdatesPanel />;
  if (pathname.startsWith(settingsPath("deployment/sync"))) return <SyncLinkPanel />;
  if (pathname.startsWith(settingsPath("project/mcp"))) return <McpToolsView />;
  if (pathname.startsWith(settingsPath("project/feedback-kit"))) return <FeedbackKitView />;
  if (pathname.startsWith(settingsPath("project/integrations"))) return <IntegrationsPanel />;
  if (pathname.startsWith(settingsPath("project/api-keys"))) return <ApiKeysPanel />;
  if (pathname.startsWith(settingsPath("deployment/providers"))) return <CredentialsPanel />;
  // GRPH-625 moved providers from This project to This box. The old path must redirect,
  // not fall through: the project-prefix catch-all below would render ProjectPanel at a
  // providers URL, and a wrong pane that looks like a pane is the worse failure.
  if (pathname.startsWith(settingsPath("project/providers")))
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  if (pathname.startsWith(settingsPath("project/members"))) return <MembersPanel />;
  if (pathname === settingsPath("project") || pathname.startsWith(`${settingsPath("project")}/`)) {
    return <ProjectPanel />;
  }
  if (pathname.startsWith(settingsPath("account"))) return <AccountPanel />;
  return <CredentialsPanel />;
}

/**
 * Hosted Settings is path-per-item too (GRPH-P28 D3). In-memory tabs left every
 * pane on `/settings`, so `?` opened the catch-all — API keys as "AI Providers",
 * Sync / Link as the self-host paste form until that one tab grew a path.
 */
const HOSTED_NAV: { to: string; label: string; end?: boolean }[] = [
  { to: settingsPath("deployment/providers"), label: "AI Providers" },
  { to: settingsPath("project/integrations"), label: "Integrations" },
  { to: settingsPath("deployment/sync"), label: "Sync / Link" },
  { to: settingsPath("project"), label: "Project", end: true },
  { to: settingsPath("project/members"), label: "Members" },
  { to: settingsPath("project/api-keys"), label: "API keys" },
  { to: settingsPath("account"), label: "Account" },
  { to: settingsPath("deployment/updates"), label: "Updates" },
];

function HostedSettingsTabs() {
  const { pathname } = useLocation();
  if (pathname === "/settings" || pathname === "/settings/") {
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">Providers, integrations, project config, members, and API keys.</p>
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-[200px_1fr]">
        <div className="flex flex-col gap-0.5 border-r border-line p-3">
          {HOSTED_NAV.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              end={it.end}
              className={({ isActive }) =>
                cn(
                  "block rounded-[9px] px-3 py-2 text-[13px] transition-colors",
                  isActive ? "bg-surface-3 text-fg" : "text-muted hover:bg-surface-3 hover:text-fg-2",
                )
              }
            >
              {it.label}
            </NavLink>
          ))}
        </div>
        <div className="min-h-0 overflow-y-auto p-6">
          {/* ONE list. `AiProvidersPanel` sat below this until S6, because it was the only
              way to edit the legacy `providers` blob that resolution still read. S6 removed
              that step and migrated every blob into a credential row, so the old panel became
              a second editor for configuration nothing consults — two lists of the same thing,
              free to disagree. */}
          <HostedPane pathname={pathname} />
        </div>
      </div>
    </div>
  );
}

function HostedPane({ pathname }: { pathname: string }) {
  if (pathname.startsWith(settingsPath("deployment/gitops"))) {
    return <Navigate to={adminPath("gitops")} replace />;
  }
  if (pathname.startsWith(settingsPath("deployment/sync"))) return <CloudOrgLinkPanel />;
  if (pathname.startsWith(settingsPath("deployment/updates"))) return <UpdatesPanel />;
  if (pathname.startsWith(settingsPath("deployment/providers"))) return <CredentialsPanel />;
  if (pathname.startsWith(settingsPath("project/providers"))) {
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  }
  if (pathname.startsWith(settingsPath("project/mcp"))) return <McpToolsView />;
  if (pathname.startsWith(settingsPath("project/feedback-kit"))) return <FeedbackKitView />;
  if (pathname.startsWith(settingsPath("project/integrations"))) return <IntegrationsPanel />;
  if (pathname.startsWith(settingsPath("project/api-keys"))) return <ApiKeysPanel />;
  if (pathname.startsWith(settingsPath("project/members"))) return <MembersPanel />;
  if (pathname === settingsPath("project") || pathname.startsWith(`${settingsPath("project")}/`)) {
    return <ProjectPanel />;
  }
  if (pathname.startsWith(settingsPath("account"))) return <AccountPanel />;
  return <CredentialsPanel />;
}

function Section({ title, desc, extra, children }: {
  title: string; desc?: React.ReactNode; extra?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="mb-6 max-w-2xl">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[14px] font-semibold">{title}</div>
        {extra}
      </div>
      {desc && <p className="mb-3 mt-0.5 text-[12.5px] text-muted">{desc}</p>}
      <div className={desc ? "" : "mt-3"}>{children}</div>
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function IntegrationsPanel() {
  const { activeId } = useProjectCtx();
  const { data: cfg } = usePlatform(activeId);
  const qc = useQueryClient();
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const invalidate = () => qc.invalidateQueries({ queryKey: ["platform"] });
  const [ghAccount, setGhAccount] = React.useState("");
  const [ghRepo, setGhRepo] = React.useState("");
  const [drAccount, setDrAccount] = React.useState("");
  const [drFolder, setDrFolder] = React.useState("");
  const [copied, setCopied] = React.useState(false);
  const [syncing, setSyncing] = React.useState(false);
  const [syncReport, setSyncReport] = React.useState<Awaited<ReturnType<typeof api.gdriveSync>> | null>(null);
  async function runSync() {
    setSyncing(true);
    try {
      setSyncReport(await api.gdriveSync(activeId));
      qc.invalidateQueries({ queryKey: keys.prds });
    } finally {
      setSyncing(false);
    }
  }
  const [rateLimit, setRateLimit] = React.useState(20);
  const [sitekey, setSitekey] = React.useState("");
  const [secret, setSecret] = React.useState("");
  const [spamSaved, setSpamSaved] = React.useState(false);
  React.useEffect(() => {
    if (cfg) {
      setRateLimit(cfg.rate_limit_per_min);
      setSitekey(cfg.turnstile_sitekey);
    }
  }, [cfg?.project_id, cfg?.rate_limit_per_min, cfg?.turnstile_sitekey]); // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSpam() {
    const body: Partial<PlatformConfig> & { turnstile_secret?: string } = {
      rate_limit_per_min: rateLimit,
      turnstile_sitekey: sitekey,
    };
    if (secret) body.turnstile_secret = secret;
    await api.updatePlatform(activeId, body);
    setSecret("");
    invalidate();
    setSpamSaved(true);
    setTimeout(() => setSpamSaved(false), 1500);
  }

  if (!cfg) return null;

  return (
    <div className="max-w-2xl space-y-6">
      {/* GitHub */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-3 flex items-center gap-2.5">
          <Github size={17} className="text-fg" />
          <div className="text-[14px] font-semibold">GitHub</div>
          <StatusPill connected={cfg.github_connected} />
        </div>
        {cfg.github_connected ? (
          <div className="space-y-3">
            <Row label="Account" value={cfg.github_account} />
            <Row label="Repository" value={cfg.github_repo} />
            <Row label="Scope" value={cfg.github_scope} />
            <Button variant="danger" size="sm" onClick={() => api.githubDisconnect(activeId).then(invalidate)}>
              Disconnect
            </Button>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div><Label>Account / org</Label><Input value={ghAccount} onChange={(e) => setGhAccount(e.target.value)} placeholder="acme" /></div>
              <div><Label>Repository</Label><Input value={ghRepo} onChange={(e) => setGhRepo(e.target.value)} placeholder="acme/app" /></div>
            </div>
            <Button size="sm" disabled={!ghAccount || !ghRepo} onClick={() => api.githubConnect(activeId, ghAccount, ghRepo).then(invalidate)}>
              Connect
            </Button>
          </div>
        )}
        <div className="mt-4 border-t border-line pt-3">
          <Label>Inbound issues webhook</Label>
          <div className="flex items-center gap-2">
            <code className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface px-2.5 py-1.5 font-mono text-[11px] text-muted-2">
              {origin}/api/public/github/webhook
            </code>
            <button
              className="rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
              onClick={() =>
                copyText(`${origin}/api/public/github/webhook`).then(
                  (ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)),
                )
              }
            >
              {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            </button>
          </div>
          <p className="mt-1.5 text-[11px] text-faint">
            Opened issues from the connected repo become tracker items <em>in this project</em>,
            each linked back to the GitHub issue.
          </p>
        </div>
      </div>

      {/* Google Drive */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-3 flex items-center gap-2.5">
          <HardDrive size={17} className="text-fg" />
          <div className="text-[14px] font-semibold">Google Drive</div>
          <StatusPill connected={cfg.gdrive_connected} />
        </div>
        {cfg.gdrive_connected ? (
          <div className="space-y-3">
            <Row label="Account" value={cfg.gdrive_account} />
            <Row label="Folder" value={cfg.gdrive_folder} />
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={runSync} disabled={syncing}>
                <RefreshCw size={13} className={syncing ? "animate-spin" : ""} />
                {syncing ? "Syncing…" : "Sync now"}
              </Button>
              <Button variant="danger" size="sm" onClick={() => api.gdriveDisconnect(activeId).then(invalidate)}>
                Disconnect
              </Button>
            </div>
            {syncReport && (
              <div className="rounded-[11px] border border-line-2 bg-surface px-3 py-2.5 text-[12px]">
                <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted">
                  <span>↑ exported {syncReport.exported.length + syncReport.updated_file.length}</span>
                  <span>↓ imported {syncReport.imported.length + syncReport.updated_db.length}</span>
                  <span>= in sync {syncReport.in_sync}</span>
                  {syncReport.conflicts.length > 0 && (
                    <span className="text-st-blocked">⚠ conflicts {syncReport.conflicts.join(", ")}</span>
                  )}
                </div>
                <code className="block overflow-x-auto text-[10.5px] text-faint">{syncReport.prds_dir}</code>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div><Label>Account</Label><Input value={drAccount} onChange={(e) => setDrAccount(e.target.value)} placeholder="you@example.com" /></div>
              <div><Label>Folder</Label><Input value={drFolder} onChange={(e) => setDrFolder(e.target.value)} placeholder="/Graphban" /></div>
            </div>
            <Button size="sm" disabled={!drAccount} onClick={() => api.gdriveConnect(activeId, drAccount, drFolder).then(invalidate)}>
              Connect
            </Button>
          </div>
        )}
        <div className="mt-4 border-t border-line pt-3">
          <Label>How the folder is organized</Label>
          <p className="mb-2 text-[11px] text-faint">
            The folder is this project's root. PRDs two-way sync with the <code>PRDs/</code>
            subfolder — drop a <code>.md</code> there to import a draft; conflicts are flagged, not
            clobbered. The sync directory is a mounted volume; point it at a Google Drive Desktop
            folder to reach Drive.
          </p>
          <pre className="overflow-x-auto rounded-md border border-line-2 bg-surface px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-muted-2">{`<folder>/
  PRDs/         PRD markdown — drop a .md here to import a draft
  Digests/      generated progress digests
  Exports/      memory & item snapshots (JSON)
  Attachments/  feedback screenshots`}</pre>
          <p className="mt-1.5 text-[11px] text-faint">
            Files outside these subfolders are ignored; deleting a mirror never deletes the PRD.
          </p>
        </div>
      </div>

      {/* Public sharing */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-1 flex items-center gap-2.5">
          <ShieldCheck size={17} className="text-fg" />
          <div className="text-[14px] font-semibold">Public sharing</div>
        </div>
        <p className="mb-3 text-[12px] text-muted">
          Off by default — no project data is public until you turn this on. When enabled, the
          read-only roadmap and feedback widget become reachable via an unguessable link.
        </p>
        <label className="mb-3 flex items-center gap-2 text-[12px] text-fg-2">
          <input
            type="checkbox"
            checked={cfg.public_share_enabled}
            onChange={async (e) => {
              await api.updatePlatform(activeId, { public_share_enabled: e.target.checked });
              invalidate();
            }}
            className="accent-accent"
          />
          Enable public roadmap + feedback widget for this project
        </label>
        {cfg.public_share_enabled && cfg.share_token && (
          <div className="flex items-center gap-2">
            <code className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 font-mono text-[11px] text-fg-2">
              {origin}/embed/roadmap?token={cfg.share_token}
            </code>
            <button
              className="rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
              onClick={() => copyText(`${origin}/embed/roadmap?token=${cfg.share_token}`).then((ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)))}
              title="Copy public roadmap link"
            >
              {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            </button>
          </div>
        )}
      </div>

      {/* Spam protection */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-1 flex items-center gap-2.5">
          <ShieldCheck size={17} className="text-fg" />
          <div className="text-[14px] font-semibold">Spam protection</div>
        </div>
        <p className="mb-3 text-[12px] text-muted">
          Applies to the public feedback endpoints for this project. A honeypot is always on.
        </p>
        <div className="space-y-3">
          <div className="max-w-[200px]">
            <Label>Rate limit (submissions / minute / IP)</Label>
            <Input type="number" value={rateLimit} onChange={(e) => setRateLimit(Number(e.target.value) || 0)} />
          </div>
          <div>
            <Label>Cloudflare Turnstile site key <span className="text-faint-2">(optional)</span></Label>
            <Input value={sitekey} onChange={(e) => setSitekey(e.target.value)} placeholder="0x4AAAAAAA…" />
          </div>
          <div>
            <Label>
              Turnstile secret key{" "}
              {cfg.turnstile_secret_set ? (
                <span className="text-accent">· configured</span>
              ) : (
                <span className="text-faint-2">(optional; write-only)</span>
              )}
            </Label>
            <Input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={cfg.turnstile_secret_set ? "•••••••• (leave blank to keep)" : "0x4AAAAAAA…"}
            />
          </div>
          <Button size="sm" onClick={saveSpam}>{spamSaved ? "Saved" : "Save spam settings"}</Button>
          <p className="text-[11px] text-faint">
            When a secret is set, submissions must pass Turnstile. Leave both blank for no captcha
            (default). The widget renders the challenge automatically.
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Change a project's tag (PRD-13 / AL-258).
 *
 * Separate from the Save button on purpose: renaming a project is a PATCH, but moving a
 * tag has to record tag history so every key rendered under the old one keeps resolving.
 * Folding it into the form would make that a silent side effect of an unrelated edit.
 *
 * Nothing is rewritten — no id moves, no link breaks, no agent claim drops. Old keys
 * keep working, which is what the confirmation says rather than warning about a
 * migration that does not happen.
 */
function RetagRow() {
  const { active } = useProjectCtx();
  const qc = useQueryClient();
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [tag, setTag] = React.useState("");
  const [check, setCheck] = React.useState<{ available: boolean; reason: string } | null>(null);

  React.useEffect(() => {
    if (!tag) { setCheck(null); return; }
    let cancelled = false;
    const t = setTimeout(() => {
      api.tagCheck(tag).then((r) => { if (!cancelled) setCheck(r); }).catch(() => {});
    }, 250);
    return () => { cancelled = true; clearTimeout(t); };
  }, [tag]);

  if (!active) return null;

  async function apply() {
    setBusy(true);
    setError("");
    try {
      await api.retagProject(active!.id, tag);
      await qc.invalidateQueries({ queryKey: keys.projects });
      setOpen(false);
      setTag("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change the tag.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-3">
      <Label>Tag</Label>
      <div className="flex items-center gap-2">
        <Input value={active.tag} readOnly className="max-w-[7rem] font-mono" />
        {!open && (
          <Button type="button" variant="ghost" onClick={() => setOpen(true)}>Change</Button>
        )}
        {open && (
          <>
            <Input
              value={tag}
              onChange={(e) => setTag(e.target.value.toUpperCase().slice(0, 4))}
              placeholder="new tag"
              maxLength={4}
              aria-label="New tag"
              className="max-w-[7rem] font-mono"
            />
            <Button type="button" onClick={apply} disabled={busy || !tag || check?.available === false}>
              {busy ? "Changing…" : "Apply"}
            </Button>
            <Button type="button" variant="ghost" onClick={() => { setOpen(false); setTag(""); setError(""); }}>
              Cancel
            </Button>
          </>
        )}
      </div>
      <p className="mt-1.5 font-mono text-[10px] text-faint" role="status">
        {error ? <span className="text-danger">{error}</span>
          : check && !check.available ? <span className="text-danger">{check.reason}</span>
          : open && tag ? <>Keys will display as <span className="text-fg-2">{tag}-12</span>. Existing keys keep working — nothing is renumbered.</>
          : "The prefix this project's item, request, and PRD keys display with."}
      </p>
    </div>
  );
}

function ProjectPanel() {
  const { active } = useProjectCtx();
  const qc = useQueryClient();
  const [form, setForm] = React.useState<Partial<Project>>({});
  const [saved, setSaved] = React.useState(false);
  React.useEffect(() => {
    if (active) setForm({ name: active.name, description: active.description, share_global_memory: active.share_global_memory, auto_extract: active.auto_extract, mcp_enabled: active.mcp_enabled, memory_write_mode: active.memory_write_mode, memory_auto_reject: active.memory_auto_reject, memory_llm_judge: active.memory_llm_judge, agent_adjudication: active.agent_adjudication, allow_self_review: active.allow_self_review });
  }, [active?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!active) return null;

  async function save() {
    await api.updateProject(active!.id, form);
    qc.invalidateQueries({ queryKey: keys.projects });
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  const flags: { key: keyof Project; label: string; hint?: string; disabled?: boolean }[] = [
    { key: "share_global_memory", label: "Share global memory across projects" },
    { key: "auto_extract", label: "Auto-extract lessons on item completion" },
    { key: "mcp_enabled", label: "Expose MCP tools for this project" },
    // GRPH-380. Named for what it is: with it on, an agent can be the only thing that ever
    // looked at its own work. The hint states the condition, because the condition is the
    // reason this is not simply "review off" — the server still refuses whenever anyone else
    // could have reviewed it.
    { key: "allow_self_review", label: "Danger mode: let an agent sign off its own work", hint: "Off by default. Only applies when NO other agent could review the item — with a second agent here, self-review is still refused. Items signed off this way say so on the item. Adversarial evidence is still required." },
  ];

  // AL-227: memory auto-triage — the scorer acts on agent candidates on write.
  const triageFlags: { key: keyof Project; label: string; hint?: string; disabled?: boolean }[] = [
    { key: "memory_auto_reject", label: "Auto-reject duplicate & rejected-alike memories", hint: "On: near-duplicates and shards resembling ones you've rejected drop straight to rejected (kept, never surfaced — undoable). Applies in every write mode." },
    { key: "memory_llm_judge", label: "Use the LLM judge to assess memories", hint: "Needs a chat provider configured. The model rates each candidate's quality to refine the decisions above; falls back to similarity when no model is set." },
    { key: "agent_adjudication", label: "Let agents adjudicate memory", hint: "Off by default. An agent can discard its own candidates, and can SUBMIT one for review by the configured model — it never publishes its own work, and with no model configured the shard stays here for you. Everything it publishes is labelled." },
  ];

  // AL-280: what happens to a NOVEL agent write. Replaced the auto-publish checkbox,
  // which could never publish anything novel however it was set.
  const writeModes: { value: string; label: string; hint: string }[] = [
    { value: "review", label: "Review", hint: "Agent writes wait as candidates until you publish them. The agent can't read back what it just wrote." },
    { value: "auto", label: "Auto", hint: "Publishes only strongly-corroborated lessons — a recurring or vouched-for note. Novel facts still wait for you." },
    { value: "trusted", label: "Trusted", hint: "Publishes on write, so the agent can read back what it wrote. Nobody has assessed these — they're labelled and undoable in Memory review." },
  ];

  return (
    <Section title="Project" desc="Configuration for the active project.">
      <div className="mb-3"><Label>Name</Label><Input value={form.name ?? ""} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} className="max-w-sm" /></div>
      <RetagRow />
      <div className="mb-4"><Label>Description</Label><Textarea rows={2} value={form.description ?? ""} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} /></div>
      <div className="mb-4 space-y-2">
        {flags.map((fl) => (
          <label key={fl.key} className="flex cursor-pointer items-center gap-2.5 text-[12.5px] text-fg-2">
            <input type="checkbox" className="accent-accent" checked={!!form[fl.key]} onChange={(e) => setForm((f) => ({ ...f, [fl.key]: e.target.checked }))} />
            {fl.label}
          </label>
        ))}
      </div>
      <div className="mb-4">
        <Label>Agent memory writes</Label>
        <div className="mt-1 space-y-2.5">
          {writeModes.map((m) => (
            <label key={m.value} className="flex cursor-pointer gap-2.5 text-[12.5px] text-fg-2">
              <input
                type="radio"
                name="memory_write_mode"
                className="accent-accent mt-0.5"
                checked={(form.memory_write_mode ?? "review") === m.value}
                onChange={() => setForm((f) => ({ ...f, memory_write_mode: m.value }))}
              />
              <span>
                {m.label}
                <span className="mt-0.5 block text-[11px] leading-snug text-faint">{m.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </div>
      <div className="mb-4">
        <Label>Memory auto-triage</Label>
        <div className="mt-1 space-y-2.5">
          {triageFlags.map((fl) => (
            <label key={fl.key} className={cn("flex cursor-pointer gap-2.5 text-[12.5px] text-fg-2", fl.disabled && "cursor-not-allowed opacity-55")}>
              <input type="checkbox" className="accent-accent mt-0.5" disabled={fl.disabled} checked={!!form[fl.key]} onChange={(e) => setForm((f) => ({ ...f, [fl.key]: e.target.checked }))} />
              <span>
                {fl.label}
                {fl.hint && <span className="mt-0.5 block text-[11px] leading-snug text-faint">{fl.hint}</span>}
              </span>
            </label>
          ))}
        </div>
      </div>
      <Button size="sm" onClick={save}>{saved ? "Saved" : "Save project"}</Button>
    </Section>
  );
}

function MembersPanel() {
  const { activeId } = useProjectCtx();
  const { data: members = [] } = useMembers(activeId);
  return (
    <Section title="Members" desc="People with access to this project and their roles.">
      <div className="space-y-2">
        {members.map((m) => (
          <div key={m.user.id} className="flex items-center gap-3 rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
            <Avatar initials={m.user.initials} color={m.user.avatar} size={30} />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] text-fg-2">{m.user.name}</div>
              <div className="font-mono text-[10.5px] text-faint">@{m.user.handle}</div>
            </div>
            <span className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">{m.role}</span>
            <span className="w-12 text-right font-mono text-[10px] text-faint">{m.access}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}

/**
 * Changing your own password. There was no way to do this at all until GRPH-219 — an
 * operator provisioned by `bootstrap-hosted` was handed a generated password and no means
 * of rotating it, which is exactly the credential most likely to have been pasted somewhere
 * it shouldn't live.
 */
function AccountPanel() {
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState("");

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < 8;
  const ready = current.length > 0 && next.length >= 8 && next === confirm && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setDone(false);
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(errorDetail(err, "could not change the password"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section
      title="Password"
      desc="Changing it signs out every other device — that is the point, not a side effect."
    >
      <form onSubmit={submit} className="max-w-[380px] space-y-3">
        <Input
          type="password"
          autoComplete="current-password"
          placeholder="Current password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <Input
          type="password"
          autoComplete="new-password"
          placeholder="New password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
        <Input
          type="password"
          autoComplete="new-password"
          placeholder="Confirm new password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {tooShort && <p className="text-[12px] text-muted">At least 8 characters.</p>}
        {mismatch && <p className="text-[12px] text-muted">Those two don’t match.</p>}
        {error && <p className="text-[12px] text-red-400">{error}</p>}
        {done && <p className="text-[12px] text-emerald-400">Password changed. Other devices are signed out.</p>}
        <Button type="submit" disabled={!ready}>
          {busy ? "Changing…" : "Change password"}
        </Button>
      </form>
    </Section>
  );
}

/** Agent keys talk to the MCP endpoint; sync credentials only push a code graph (AL-219 D4).
 *
 * `gate` attests that work was checked (GRPH-580). It shipped as a scope in GRPH-541, completion
 * was made to depend on it in GRPH-543, and a CI adapter was built for it in GRPH-551 — while the
 * only way to mint one was curl with a JWT. A capability nobody can create is one nobody uses,
 * and the gate then runs permanently under the weak path, which looks identical to working.
 */
type KeyKind = "agent" | "sync" | "gate";

/**
 * What to do with a gate key, which is NOT what to do with an agent key.
 *
 * Routing this to `McpInstall` — the previous behaviour for anything non-sync — would tell the
 * operator to paste a completion-attesting key into the MCP config of the agent doing the work.
 * That is precisely the arrangement the scope exists to prevent: the gate would still be armed,
 * still refuse `done` without an attestation, and the attestation would come from the same
 * agent. It would look exactly like a working gate.
 *
 * The consumer is `scripts/attest_ci.py`, which reads these two variables and nothing else.
 */
function GateKeyInstall({ apiKey }: { apiKey: string }) {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const snippet = `GRAPHBAN_URL=${origin}\nGRAPHBAN_GATE_KEY=${apiKey}`;
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="mt-3 border-t border-line-2 pt-3">
      <p className="mb-2 text-[12px] text-muted">
        Store as CI secrets — <span className="text-fg-2">not</span> in an agent&rsquo;s MCP
        config. <code className="font-mono text-[11px] text-fg-2">scripts/attest_ci.py</code>{" "}
        reads these after the check that decides CI is green.
      </p>
      <div className="flex items-start gap-2">
        <pre className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 p-2 font-mono text-[11px] text-fg-2">
          {snippet}
        </pre>
        <button
          className="rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
          aria-label="Copy CI secrets"
          onClick={() =>
            copyText(snippet).then(
              (ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)),
            )
          }
        >
          {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
        </button>
      </div>
    </div>
  );
}

/**
 * The optional MCP tool tiers (GRPH-571), mirroring `app/services/tool_tiers.TIER_PURPOSE`.
 *
 * Duplicated rather than fetched, and that is a real trade: the server is authoritative and
 * these can drift. Fetching would need an endpoint that exists only for this list. The guard
 * against drift is that the server REFUSES an unknown tier with a 422 naming the allowed set,
 * so a stale entry here fails loudly at mint rather than minting a key that never widens.
 */
const TOOL_TIERS: ReadonlyArray<readonly [string, string, string]> = [
  ["prd", "PRDs", "Authoring and grilling specs — write, grill, decompose, close. A coding agent reads specs; it does not write them."],
  ["codegraph", "Code graph writes", "Describing symbols and linking them. Reading the graph is core and always present."],
  ["fleet", "Fleet admin", "Running a fleet — allocation, roles, enrolment codes, waves. Being in a fleet needs a seat, not this tier."],
  ["misc", "Occasional", "Projects, digests, lessons, memory review. Rare, and none of it mid-task."],
];

export function ApiKeysPanel() {
  const { data: apiKeys = [] } = useApiKeys();
  const syncKeys = apiKeys.filter((k) => k.scopes?.includes("sync"));
  const gateKeys = apiKeys.filter((k) => k.scopes?.includes("gate"));
  // Everything that is neither. Written as an exclusion of BOTH rather than of `sync` alone:
  // the old expression put a gate key in the agent list, where its scope is invisible.
  const agentKeys = apiKeys.filter(
    (k) => !k.scopes?.includes("sync") && !k.scopes?.includes("gate"),
  );
  const { active, projects } = useProjectCtx();
  const qc = useQueryClient();
  const [kind, setKind] = React.useState<KeyKind>("agent");
  // Optional MCP tool tiers (GRPH-571). Empty is the default and not a degraded state: a key
  // gets the core manifest, and a tool left out of it still works when called — the tier
  // decides only what `tools/list` advertises.
  const [tiers, setTiers] = React.useState<string[]>([]);
  const [name, setName] = React.useState("");
  // Project|Global, same axis as the CLI's `graphban init --key-scope`. A boolean named
  // `global` was the old checkbox's shape; as a two-way control the name should say what
  // the control is.
  const [scope, setScope] = React.useState<KeyScope>("project");
  const [expiryDays, setExpiryDays] = React.useState<number | null>(null);
  // A sync credential pins to one cloud project, so it gets its own explicit target rather
  // than riding the active project — a fleet mints one per project (D6).
  const [syncProject, setSyncProject] = React.useState<string>("");
  // `scope` rides along so the connect snippet's harness flag matches the key that was just
  // minted — the toggle's whole point is that nobody re-declares the scope when wiring the agent.
  const [created, setCreated] = React.useState<{ plaintext: string; kind: KeyKind; projectId: string; scope: KeyScope } | null>(null);
  const [copied, setCopied] = React.useState(false);
  const [connectId, setConnectId] = React.useState<string | null>(null);
  // Which key's "minted with" detail is open. Independent of `connectId` — the permissions a
  // key was minted with and the snippet for wiring it into an agent are separate questions.
  const [openDetails, setOpenDetails] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  // Write access isn't in the Project shape; the backend rejects a sync key on a read-only
  // project with an honest 403, which surfaces in `error` below.
  const syncTarget = syncProject || active?.id || projects[0]?.id || "";

  const projectName = (id: string | null) =>
    id ? (projects.find((p) => p.id === id)?.name ?? id) : "All projects";

  async function create() {
    if (!name.trim()) return;
    setError(null);
    const isPinned = kind === "sync" || kind === "gate";
    const projectId = isPinned ? syncTarget : scope === "global" ? null : active?.id ?? null;
    if (isPinned && !projectId) {
      setError(
        `Pick a project — a ${kind === "gate" ? "gate key" : "link key"} must target exactly one.`,
      );
      return;
    }
    try {
      // `["gate"]` alone mints a DEAD key. `attest_ci.py` attests via `update_item`, which
      // mcp_server refuses without `write` — so the key would be created successfully, be
      // stored as a CI secret, and 403 on the first real attestation. `fleet.mint` already
      // carries read+write alongside `gate` for exactly this reason; this matches it.
      const scopes = kind === "sync" ? ["sync"] : kind === "gate" ? ["read", "write", "gate"] : undefined;
      // Tiers only mean anything for an agent key: a sync credential calls no MCP tools, and
      // a gate key calls exactly one, which is core.
      const res = await api.createApiKey(name.trim(), projectId, expiryDays, scopes,
        kind === "agent" ? tiers : undefined);
      setCreated({ plaintext: res.plaintext, kind, projectId: projectId ?? "", scope });
      setName("");
      qc.invalidateQueries({ queryKey: keys.apiKeys });
    } catch (e) {
      setError(errorDetail(e, "Could not create the key."));
    }
  }
  async function revoke(id: string) {
    await api.revokeApiKey(id);
    qc.invalidateQueries({ queryKey: keys.apiKeys });
  }

  return (
    <Section
      title="API keys"
      extra={
        <div className="flex items-center gap-3">
          {/* AI Providers' "Looking for API keys?" is the other direction. This page
              mints Graphban keys; LLM credentials (what the box runs on) live there. */}
          <Link
            to={settingsPath("deployment/providers")}
            className="text-[12px] font-normal text-muted transition-colors hover:text-fg-2"
          >
            Looking for LLM credentials?
          </Link>
          {/* Fleet's "Looking for MCP?" is the other direction of the same question. */}
          <Link to="/fleet" className="text-[12px] font-normal text-muted transition-colors hover:text-fg-2">
            Looking for seats?
          </Link>
        </div>
      }
      desc={
        <>
          An API key is who the process is — put it in MCP config once. Roles for a wave are{" "}
          <Link to="/fleet" className="text-fg-2 underline-offset-2 hover:underline">
            seats on Fleet
          </Link>
          , not a new key.
        </>
      }
    >
      {created && (
        <div className="mb-4 rounded-[11px] border border-accent/40 bg-[rgba(198,242,78,0.06)] p-3">
          <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-accent">Copy now — shown once</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 overflow-x-auto font-mono text-[12px] text-fg-2">{created.plaintext}</code>
            <button className="rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
              onClick={() => copyText(created.plaintext).then((ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)))}>
              {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            </button>
          </div>
          {created.kind === "sync" ? (
            <SyncCredentialInstall apiKey={created.plaintext} projectId={created.projectId} />
          ) : created.kind === "gate" ? (
            <GateKeyInstall apiKey={created.plaintext} />
          ) : (
            <McpInstall apiKey={created.plaintext} keyScope={created.scope} />
          )}
        </div>
      )}
      <div className="mb-2 flex flex-wrap gap-1.5">
        {(
          [
            ["agent", "Agent key", "Talks to the MCP endpoint — read/write on items, memory, code."],
            ["sync", "Link key", "Pushes a code graph from a local instance into one project. Nothing else. Same object Sync / Link mints."],
            ["gate", "Gate key", "Attests that work was checked, so an item may reach done. For CI or a reviewer — never for the agent doing the work."],
          ] as const
        ).map(([id, label, desc]) => (
          <button
            key={id}
            onClick={() => {
              setKind(id);
              setError(null);
            }}
            title={desc}
            className={cn(
              "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
              kind === id ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="mb-2 flex items-center gap-2">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={
            kind === "sync"
              ? "Key name (e.g. laptop — acme-core)"
              : kind === "gate"
                ? "Key name (e.g. github-actions)"
                : "Key name (e.g. claude-code)"
          }
          className="max-w-xs"
        />
        {(kind === "sync" || kind === "gate") && (
          <select
            value={syncTarget}
            onChange={(e) => setSyncProject(e.target.value)}
            className="rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 text-[12px] text-muted"
            aria-label={kind === "gate" ? "Gate target project" : "Link key target project"}
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        )}
        <select
          value={expiryDays ?? ""}
          onChange={(e) => setExpiryDays(e.target.value ? Number(e.target.value) : null)}
          className="rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 text-[12px] text-muted"
          aria-label="Key expiry"
        >
          <option value="">No expiry</option>
          <option value="30">Expires in 30 days</option>
          <option value="90">Expires in 90 days</option>
          <option value="365">Expires in 365 days</option>
        </select>
        <Button size="sm" onClick={create} disabled={!name.trim()}>
          <Plus size={14} />{kind === "sync" ? "Mint link key" : kind === "gate" ? "Mint gate key" : "Mint key"}
        </Button>
      </div>
      {error && <p className="mb-2 text-[12px] text-st-blocked">{error}</p>}
      {kind === "agent" && (
        <div className="mb-3">
          <div className="mb-1.5 text-[12px] text-muted">
            Tool tiers — an agent key is shipped the{" "}
            <span className="text-fg-2">core</span> tools by default. These are specialist and
            cost manifest tokens every turn, so they are opt-in. A tool left out is still{" "}
            <em className="not-italic text-fg-2">callable</em>; it just is not advertised.
          </div>
          <div className="flex flex-wrap gap-1.5">
            {TOOL_TIERS.map(([id, label, desc]) => {
              const on = tiers.includes(id);
              return (
                <button
                  key={id}
                  title={desc}
                  onClick={() =>
                    setTiers((cur) => (on ? cur.filter((x) => x !== id) : [...cur, id]))
                  }
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
                    on
                      ? "border-accent/50 bg-surface-3 text-fg"
                      : "border-line-2 text-muted hover:text-fg-2",
                  )}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {kind === "sync" ? (
        <p className="mb-4 text-[12px] text-muted">
          Pinned to <span className="text-fg-2">{projectName(syncTarget)}</span> — a link key targets exactly one
          project, so a key distributed to a fleet can only ever push there.
        </p>
      ) : kind === "gate" ? (
        <p className="mb-4 text-[12px] text-muted">
          Pinned to <span className="text-fg-2">{projectName(syncTarget)}</span> — a gate key attests in exactly one
          project, so a leaked CI secret cannot complete work across every project you can write.
        </p>
      ) : (
        <div className="mb-4">
          <div className="mb-1.5 text-[12px] text-muted">
            Scope — {scope === "global" ? (
              <span>a global key: the agent passes <code className="font-mono text-[11px]">project_id</code> per call, or falls back to its default project.</span>
            ) : (
              <span>pinned to <span className="text-fg-2">{active?.name ?? "the active project"}</span>: the agent's writes target it without naming it.</span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Key scope">
            {(
              [
                ["project", "Project", "Writes default to the active project."],
                ["global", "Global", "Any project the owner can write; calls pass project_id."],
              ] as const
            ).map(([id, label, desc]) => (
              <button
                key={id}
                onClick={() => setScope(id)}
                title={desc}
                aria-pressed={scope === id}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
                  scope === id ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}
      <KeyGroup
        title="Agent keys"
        blurb="Read and write items, memory and claims — who the process is. A seat on Fleet is the role for this wave."
        rows={agentKeys}
        empty="No agent key has been minted. Nothing can reach the MCP endpoint until one is."
      >
        {(k) => (
          <div key={k.id} className="rounded-[11px] border border-line-2 bg-surface-2">
            <div className="flex items-center gap-3 px-3 py-2.5">
              <button
                onClick={() => setOpenDetails(openDetails === k.id ? null : k.id)}
                aria-expanded={openDetails === k.id}
                title="Minted with"
                className="flex-none text-faint transition-colors hover:text-fg"
              >
                <ChevronRight size={14} className={cn("transition-transform", openDetails === k.id && "rotate-90")} />
              </button>
              <KeyRound size={14} className="text-muted" />
              <span className="text-[13px] text-fg-2">{k.name}</span>
              <code className="font-mono text-[11px] text-faint">{k.prefix}…</code>
              {k.scopes?.includes("sync") && (
                <span className="rounded border border-accent/40 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-accent">
                  sync
                </span>
              )}
              <span
                className={cn(
                  "rounded border px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide",
                  k.project_id
                    ? "border-line-2 text-muted"
                    : "border-[rgba(167,139,250,0.3)] text-purple-2",
                )}
              >
                {projectName(k.project_id)}
              </span>
              {k.fleet_wave && (
                <span
                  className="font-mono text-[9.5px] uppercase tracking-wide text-[color:var(--color-st-review)]"
                  title="End wave sweeps this and never a hand-minted key"
                >
                  {k.fleet_wave} · swept by End wave
                </span>
              )}
              {k.expires_at && (
                <span
                  className={cn(
                    "font-mono text-[9.5px] uppercase tracking-wide",
                    new Date(k.expires_at) <= new Date() ? "text-st-blocked" : "text-faint-2",
                  )}
                  title={`Expires ${new Date(k.expires_at).toLocaleDateString()}`}
                >
                  {new Date(k.expires_at) <= new Date() ? "expired" : `expires ${new Date(k.expires_at).toLocaleDateString()}`}
                </span>
              )}
              <span className="ml-auto font-mono text-[10px] text-faint-2" title={k.last_used ?? undefined}>
                {lastUsedLabel(k.last_used)}
              </span>
              {/* Sync credentials never touch the MCP endpoint, so the MCP install snippet
                  would be misleading for them — the link hand-off is shown at mint time. */}
              {!k.scopes?.includes("sync") && (
                <button
                  className={cn("hover:text-fg", connectId === k.id ? "text-accent" : "text-faint")}
                  onClick={() => setConnectId(connectId === k.id ? null : k.id)}
                  title="Connect an agent (MCP setup)"
                >
                  <Plug size={14} />
                </button>
              )}
              <button className="text-faint hover:text-st-blocked" onClick={() => revoke(k.id)} title="Revoke">
                <Trash2 size={14} />
              </button>
            </div>
            {openDetails === k.id && <MintedWith k={k} projectName={projectName} />}
            {connectId === k.id && (
              <div className="border-t border-line px-3 pb-3">
                <McpInstall apiKey="<YOUR_API_KEY>" keyPrefix={k.prefix} keyScope={k.project_id ? "project" : "global"} />
              </div>
            )}
          </div>
        )}
      </KeyGroup>

      <KeyGroup
        title="Link keys"
        blurb={
          <>
            Push a code graph and nothing else. Each one <em className="not-italic text-fg-2">is</em>{" "}
            a linked deployment's identity — one key, one box — so revoking it unlinks that
            deployment rather than just closing an access path. Sync / Link mints the same object.
          </>
        }
        rows={syncKeys}
        empty="No deployment is linked. A link key is what links one, and it is minted here."
      >
        {(k) => (
          <div key={k.id} className="rounded-[11px] border border-line-2 bg-surface-2">
            <div className="flex items-center gap-3 px-3 py-2.5">
              <button
                onClick={() => setOpenDetails(openDetails === k.id ? null : k.id)}
                aria-expanded={openDetails === k.id}
                title="Minted with"
                className="flex-none text-faint transition-colors hover:text-fg"
              >
                <ChevronRight size={14} className={cn("transition-transform", openDetails === k.id && "rotate-90")} />
              </button>
              <KeyRound size={14} className="text-muted" />
              <span className="text-[13px] text-fg-2">{k.name}</span>
              <code className="font-mono text-[11px] text-faint">{k.prefix}…</code>
              {k.scopes?.includes("sync") && (
                <span className="rounded border border-accent/40 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-accent">
                  sync
                </span>
              )}
              <span
                className={cn(
                  "rounded border px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide",
                  k.project_id
                    ? "border-line-2 text-muted"
                    : "border-[rgba(167,139,250,0.3)] text-purple-2",
                )}
              >
                {projectName(k.project_id)}
              </span>
              {k.expires_at && (
                <span
                  className={cn(
                    "font-mono text-[9.5px] uppercase tracking-wide",
                    new Date(k.expires_at) <= new Date() ? "text-st-blocked" : "text-faint-2",
                  )}
                  title={`Expires ${new Date(k.expires_at).toLocaleDateString()}`}
                >
                  {new Date(k.expires_at) <= new Date() ? "expired" : `expires ${new Date(k.expires_at).toLocaleDateString()}`}
                </span>
              )}
              <span className="ml-auto font-mono text-[10px] text-faint-2" title={k.last_used ?? undefined}>
                {lastUsedLabel(k.last_used)}
              </span>
              {/* Sync credentials never touch the MCP endpoint, so the MCP install snippet
                  would be misleading for them — the link hand-off is shown at mint time. */}
              {!k.scopes?.includes("sync") && (
                <button
                  className={cn("hover:text-fg", connectId === k.id ? "text-accent" : "text-faint")}
                  onClick={() => setConnectId(connectId === k.id ? null : k.id)}
                  title="Connect an agent (MCP setup)"
                >
                  <Plug size={14} />
                </button>
              )}
              <button className="text-faint hover:text-st-blocked" onClick={() => revoke(k.id)} title="Revoke">
                <Trash2 size={14} />
              </button>
            </div>
            {openDetails === k.id && <MintedWith k={k} projectName={projectName} />}
            {connectId === k.id && (
              <div className="border-t border-line px-3 pb-3">
                <McpInstall apiKey="<YOUR_API_KEY>" keyPrefix={k.prefix} keyScope={k.project_id ? "project" : "global"} />
              </div>
            )}
          </div>
        )}
      </KeyGroup>
      <KeyGroup
        title="Gate keys"
        blurb={
          <>
            Attest that work was checked, so an item may reach <code className="font-mono text-[11px]">done</code>.
            Give one to CI or to a reviewer — <em className="not-italic text-fg-2">never</em> to the
            agent doing the work, since the whole point is that the proof comes from somewhere else.
          </>
        }
        rows={gateKeys}
        empty="No gate key exists, so nothing can attest. Completion then depends entirely on a reviewer signing off with a commit."
      >
        {(k) => (
          <div key={k.id} className="rounded-[11px] border border-line-2 bg-surface-2">
            <div className="flex items-center gap-3 px-3 py-2.5">
              <button
                onClick={() => setOpenDetails(openDetails === k.id ? null : k.id)}
                aria-expanded={openDetails === k.id}
                title="Minted with"
                className="flex-none text-faint transition-colors hover:text-fg"
              >
                <ChevronRight size={14} className={cn("transition-transform", openDetails === k.id && "rotate-90")} />
              </button>
              <KeyRound size={14} className="text-muted" />
              <span className="text-[13px] text-fg-2">{k.name}</span>
              <code className="font-mono text-[11px] text-faint">{k.prefix}…</code>
              <span className="rounded border border-accent/40 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-accent">
                gate
              </span>
              <span className="ml-auto text-[11px] text-faint">{projectName(k.project_id ?? null)}</span>
              <button
                onClick={() => revoke(k.id)}
                className="rounded px-1.5 py-0.5 text-[11px] text-muted hover:text-st-blocked"
              >
                Revoke
              </button>
            </div>
            {openDetails === k.id && <MintedWith k={k} projectName={projectName} />}
          </div>
        )}
      </KeyGroup>
    </Section>
  );
}

/** Human labels for the opt-in tool tiers, from the same source as the mint picker. */
const TIER_LABEL: Record<string, string> = Object.fromEntries(
  TOOL_TIERS.map(([id, label]) => [id, label]),
);

/**
 * The permissions a key was minted with — chosen once at mint time and otherwise never shown
 * again, which is what made a key's tool tiers impossible to audit after the fact.
 *
 * Scopes (read / write / sync / gate) are the operations the key may perform. MCP tool tiers
 * are the specialist groups its manifest ADVERTISES, and only apply to agent keys — a link key
 * pushes a code graph and a gate key attests, and neither calls MCP tools, so a "tools" line
 * on them would describe a capability they do not have. `core` is named explicitly and always
 * present: an agent key with no extra tiers is core-only BY DESIGN, not missing anything, and
 * an empty tools row would read as "we forgot to say" rather than "this is the default".
 */
function MintedWith({ k, projectName }: { k: ApiKey; projectName: (id: string | null) => string }) {
  const scopes = k.scopes ?? [];
  const tiers = k.tool_tiers ?? [];
  const isAgent = !scopes.includes("sync") && !scopes.includes("gate");
  return (
    <div className="border-t border-line px-3 py-2.5" data-testid="minted-with">
      <div className="mb-1.5 font-mono text-[9.5px] uppercase tracking-wide text-faint">Minted with</div>
      <div className="flex flex-col gap-1.5 text-[12px]">
        <div className="flex items-baseline gap-3">
          <span className="w-20 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">Scopes</span>
          <span className="flex flex-wrap gap-1">
            {scopes.map((s) => (
              <span key={s} className="rounded border border-line-2 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-fg-2">
                {s}
              </span>
            ))}
          </span>
        </div>
        {isAgent && (
          <div className="flex items-baseline gap-3">
            <span className="w-20 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">MCP tools</span>
            <span className="flex flex-wrap gap-1">
              <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-fg-2">
                core
              </span>
              {tiers.map((t) => (
                <span key={t} className="rounded border border-accent/40 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-accent">
                  {TIER_LABEL[t] ?? t}
                </span>
              ))}
            </span>
          </div>
        )}
        <div className="flex items-baseline gap-3">
          <span className="w-20 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">Target</span>
          <span className="text-fg-2">
            {k.project_id ? `Pinned to ${projectName(k.project_id)}` : "Global — any project the owner can write"}
          </span>
        </div>
        <div className="flex items-baseline gap-3">
          <span className="w-20 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">Expires</span>
          <span className="text-fg-2">
            {k.expires_at ? new Date(k.expires_at).toLocaleDateString() : "No expiry"}
          </span>
        </div>
      </div>
    </div>
  );
}

/**
 * One of the two kinds of credential, kept visually apart.
 *
 * They are not variants of one thing: an agent key reads and writes a tenant's work, and a
 * sync credential can only push a code graph — but it also *is* a deployment's identity, so
 * revoking it does something an access list does not describe. Listing them together made
 * the second look like the first with a narrower scope.
 */
function KeyGroup({
  title,
  blurb,
  rows,
  empty,
  children,
}: {
  title: string;
  blurb: React.ReactNode;
  rows: ApiKey[];
  empty: string;
  children: (k: ApiKey) => React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">{title}</div>
      <p className="mb-2.5 max-w-[74ch] text-[11.5px] leading-relaxed text-muted">{blurb}</p>
      <div className="space-y-2">
        {rows.map((k) => (
          <div key={k.id} className="rounded-[11px] border border-line-2 bg-surface-2">
            {children(k)}
          </div>
        ))}
        {rows.length === 0 && <p className="text-[12.5px] text-faint">{empty}</p>}
      </div>
    </div>
  );
}

function StatusPill({ connected }: { connected: boolean }) {
  return (
    <span
      className={cn(
        "ml-auto flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide",
        connected ? "border-[#1c2620] bg-[rgba(95,208,122,0.06)] text-st-done" : "border-line-2 text-faint",
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", connected ? "bg-st-done" : "bg-faint")} />
      {connected ? "connected" : "not connected"}
    </span>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-3 text-[12.5px]">
      <span className="w-24 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className="text-fg-2">{value}</span>
    </div>
  );
}

/**
 * When a key was last used, or that it never has been.
 *
 * "never used" is the interesting state, not the boring one: a key nobody has used is
 * either about to be, or was minted and forgotten — and a forgotten credential is the one
 * worth revoking. Collapsing it into a dash alongside real dates hides the only row on the
 * page that asks a question.
 */
function lastUsedLabel(at: string | null): string {
  if (!at) return "never used";
  const iso = /(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`;
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 3600) return `used ${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `used ${Math.floor(s / 3600)}h ago`;
  return `used ${Math.floor(s / 86400)}d ago`;
}
