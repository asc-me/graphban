import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Github, HardDrive, KeyRound, RefreshCw, ShieldCheck } from "lucide-react";
import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { MachinesPanel } from "@/features/settings/MachinesPanel";
import { CredentialsPanel } from "@/features/settings/CredentialsPanel";
import { GitopsPanel } from "@/features/settings/GitopsPanel";
import { LogExportPanel } from "@/features/settings/LogExportPanel";
import { UpdatesPanel } from "@/features/settings/UpdatesPanel";
import { ApiKeysPanel } from "@/features/settings/ApiKeysPanel";
import { CloudOrgLinkPanel } from "@/features/settings/CloudOrgLinkPanel";
import { SyncLinkPanel } from "@/features/settings/SyncLinkPanel";
import { useProjectCtx } from "@/features/ProjectContext";
import { copyText } from "@/lib/clipboard";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { keys, useConfig, useMembers, useMintIngestToken, usePlatform } from "@/lib/queries";
import { adminPath, settingsPath } from "@/lib/routes";
import type { PlatformConfig, Project } from "@/lib/types";
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
      // Log export is here and NOT under "This project": one collector endpoint, one exporter
      // and one queue for the whole instance, so a per-project panel would be N editors for a
      // single shared thing — the same mismatch GRPH-625 fixed for credentials. Exported
      // records span projects, so each record names its project instead (GRPH-966).
      { to: settingsPath("deployment/log-export"), label: "Log export" },
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
      { to: settingsPath("project/members"), label: "Members" },
      // Per user, not per project: the machines THIS account ran `gban setup` / `gban doctor` on.
      { to: settingsPath("account/machines"), label: "Machines" },
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
        <p className="mt-0.5 text-body text-muted">This project and this box.</p>
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
  if (pathname.startsWith(settingsPath("deployment/gitops"))) {
    return (
      <>
        <GitopsMovedCard />
        <GitopsPanel />
      </>
    );
  }
  if (pathname.startsWith(settingsPath("deployment/updates"))) return <UpdatesPanel />;
  if (pathname.startsWith(settingsPath("deployment/log-export"))) return <LogExportPanel />;
  if (pathname.startsWith(settingsPath("deployment/sync"))) return <SyncLinkPanel />;
  if (pathname.startsWith(settingsPath("project/mcp"))) return <McpToolsView />;
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
  if (pathname.startsWith(settingsPath("account/machines"))) return <MachinesPanel />;
  if (pathname.startsWith(settingsPath("account"))) return <AccountPanel />;
  return <CredentialsPanel />;
}

/**
 * Frame 7b (GRPH-1162): gitops is the Merge and Release stages of the Rules map now. The form
 * stays under the card on purpose: the `gitops.model` row is read-only on Rules (a preset rewrites
 * other fields) and links here, so this panel is the only place a self-host project sets its model.
 */
function GitopsMovedCard() {
  return (
    <div
      data-testid="gitops-moved"
      className="mb-6 flex max-w-2xl flex-col gap-1.5 rounded-[12px] border border-line bg-surface-2 p-4"
    >
      <span className="font-mono text-[10px] font-semibold tracking-[0.07em] text-muted-2">MOVED</span>
      <span className="text-[15px] font-semibold">Gitops moved into Rules.</span>
      <span className="text-[13px] text-muted">Your delivery contract is the Merge and Release stages.</span>
      <Link
        to="/rules?stage=merge"
        className="mt-1 inline-flex h-8 items-center self-start rounded-[8px] bg-accent px-3.5 text-body font-semibold text-[#0a0c0e]"
      >
        Open Rules · Merge
      </Link>
    </div>
  );
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
  { to: settingsPath("account/machines"), label: "Machines" },
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
        <p className="mt-0.5 text-body text-muted">Providers, integrations, project config, members, and API keys.</p>
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
  if (pathname.startsWith(settingsPath("deployment/log-export"))) {
    // Redirect, not the panel: the API 403s every log-export route on a hosted instance, so a
    // tenant who typed the URL would get a form that can only fail. Falling through to the
    // catch-all below is worse — that is AI providers wearing a log-export URL.
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  }
  if (pathname.startsWith(settingsPath("deployment/sync"))) return <CloudOrgLinkPanel />;
  if (pathname.startsWith(settingsPath("deployment/updates"))) return <UpdatesPanel />;
  if (pathname.startsWith(settingsPath("deployment/providers"))) return <CredentialsPanel />;
  if (pathname.startsWith(settingsPath("project/providers"))) {
    return <Navigate to={settingsPath("deployment/providers")} replace />;
  }
  if (pathname.startsWith(settingsPath("project/mcp"))) return <McpToolsView />;
  if (pathname.startsWith(settingsPath("project/integrations"))) return <IntegrationsPanel />;
  if (pathname.startsWith(settingsPath("project/api-keys"))) return <ApiKeysPanel />;
  if (pathname.startsWith(settingsPath("project/members"))) return <MembersPanel />;
  if (pathname === settingsPath("project") || pathname.startsWith(`${settingsPath("project")}/`)) {
    return <ProjectPanel />;
  }
  if (pathname.startsWith(settingsPath("account/machines"))) return <MachinesPanel />;
  if (pathname.startsWith(settingsPath("account"))) return <AccountPanel />;
  return <CredentialsPanel />;
}

function Section({ title, desc, extra, children }: {
  title: string; desc?: React.ReactNode; extra?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="mb-6 max-w-2xl">
      <div className="flex items-center justify-between gap-3">
        <div className="text-lead font-semibold">{title}</div>
        {extra}
      </div>
      {desc && <p className="mb-3 mt-0.5 text-body text-muted">{desc}</p>}
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
  const [mintedToken, setMintedToken] = React.useState<string | null>(null);
  const [tokenCopied, setTokenCopied] = React.useState(false);
  const mintIngestToken = useMintIngestToken();
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
          <div className="text-lead font-semibold">GitHub</div>
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
            <code className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface px-2.5 py-1.5 font-mono text-small text-muted-2">
              {origin}/api/public/github/webhook
            </code>
            <button
              className="rounded-md border border-control bg-surface-3 p-1.5 text-muted hover:text-fg"
              onClick={() =>
                copyText(`${origin}/api/public/github/webhook`).then(
                  (ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)),
                )
              }
            >
              {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            </button>
          </div>
          <p className="mt-1.5 text-small text-faint">
            Opened issues from the connected repo become tracker items <em>in this project</em>,
            each linked back to the GitHub issue.
          </p>
        </div>
      </div>

      {/* Google Drive */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-3 flex items-center gap-2.5">
          <HardDrive size={17} className="text-fg" />
          <div className="text-lead font-semibold">Google Drive</div>
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
                <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-small text-muted">
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
          <p className="mb-2 text-small text-faint">
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
          <p className="mt-1.5 text-small text-faint">
            Files outside these subfolders are ignored; deleting a mirror never deletes the PRD.
          </p>
        </div>
      </div>

      {/* Public surfaces (PRD-43 D4) */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-1 flex items-center gap-2.5">
          <ShieldCheck size={17} className="text-fg" />
          <div className="text-lead font-semibold">Public surfaces</div>
        </div>
        <p className="mb-3 text-[12px] text-muted">
          Each surface is independent. Intake is not a board. A token is not a publication.
          <code className="ml-1 font-mono text-small">public_share_enabled</code> is a derived read — on when any surface below is.
        </p>
        <div className="space-y-2.5">
          {([
            ["intake_enabled", "Intake", "Accept feedback via the public API"],
            ["public_form_enabled", "Public form", "Embeddable feedback form"],
            ["public_roadmap_enabled", "Roadmap", "Read-only public roadmap board"],
            ["public_issues_enabled", "Issues", "Public issues board"],
            ["public_requests_enabled", "Requests", "Public feature requests board"],
          ] as const).map(([key, label, desc]) => (
            <label key={key} className="flex items-start gap-2 text-[12px] text-fg-2">
              <input
                type="checkbox"
                checked={Boolean(cfg[key])}
                onChange={async (e) => {
                  await api.updateSurfaceFlags(activeId, { [key]: e.target.checked });
                  invalidate();
                }}
                className="mt-0.5 accent-accent"
              />
              <span>
                <span className="font-medium text-fg-2">{label}</span>
                <span className="ml-1.5 text-faint">— {desc}</span>
              </span>
            </label>
          ))}
          <label className="flex items-start gap-2 border-t border-line pt-2.5 text-[12px] text-fg-2">
            <input
              type="checkbox"
              checked={cfg.capture_identity}
              onChange={async (e) => {
                await api.updateSurfaceFlags(activeId, { capture_identity: e.target.checked });
                invalidate();
              }}
              className="mt-0.5 accent-accent"
            />
            <span>
              <span className="font-medium text-fg-2">Capture identity</span>
              <span className="ml-1.5 text-faint">— require account info on submissions</span>
            </span>
          </label>
        </div>
        {cfg.public_share_enabled && cfg.share_token && (
          <div className="mt-3 flex items-center gap-2 border-t border-line pt-3">
            <code className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 font-mono text-small text-fg-2">
              {origin}/embed/roadmap?token={cfg.share_token}
            </code>
            <button
              className="rounded-md border border-control bg-surface-3 p-1.5 text-muted hover:text-fg"
              onClick={() => copyText(`${origin}/embed/roadmap?token=${cfg.share_token}`).then((ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)))}
              title="Copy public roadmap link"
            >
              {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            </button>
          </div>
        )}
      </div>

      {/* Ingest token (PRD-43 D1) */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-1 flex items-center gap-2.5">
          <KeyRound size={17} className="text-fg" />
          <div className="text-lead font-semibold">Ingest token</div>
        </div>
        <p className="mb-3 text-[12px] text-muted">
          Bearer credential for <code className="font-mono text-small">POST /api/public/requests</code>.
          Rotating immediately invalidates the previous token.
        </p>
        {cfg.ingest_token_prefix ? (
          <div className="mb-3 flex items-center gap-2">
            <span className="font-mono text-small text-fg-2">{cfg.ingest_token_prefix}…</span>
          </div>
        ) : (
          <p className="mb-3 text-small text-faint">No token minted yet.</p>
        )}
        {mintedToken && (
          <div className="mb-3 rounded-md border border-accent/30 bg-accent/5 p-2.5">
            <p className="mb-1.5 text-small text-accent">
              Copy now — this is the only time the full token is shown.
            </p>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 font-mono text-small text-fg-2">
                {mintedToken}
              </code>
              <button
                className="flex-none rounded-md border border-control bg-surface-3 p-1.5 text-muted hover:text-fg"
                onClick={() =>
                  copyText(mintedToken).then(
                    (ok) => ok && (setTokenCopied(true), setTimeout(() => setTokenCopied(false), 1500)),
                  )
                }
              >
                {tokenCopied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
              </button>
            </div>
          </div>
        )}
        <Button
          size="sm"
          disabled={mintIngestToken.isPending}
          onClick={() =>
            mintIngestToken.mutate(activeId, {
              onSuccess: (data) => setMintedToken(data.token),
            })
          }
        >
          {mintIngestToken.isPending
            ? "Minting…"
            : cfg.ingest_token_prefix
              ? "Rotate token"
              : "Mint token"}
        </Button>
      </div>

      {/* Spam protection */}
      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-1 flex items-center gap-2.5">
          <ShieldCheck size={17} className="text-fg" />
          <div className="text-lead font-semibold">Spam protection</div>
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
          <p className="text-small text-faint">
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
    { key: "memory_llm_judge", label: "Use a model judge to assess memories", hint: "On: a configured System One decider judges first; otherwise the project's chat model; otherwise similarity. Structural vetoes (duplicates, rejected-alike) still win." },
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
          <label key={fl.key} className="flex cursor-pointer items-center gap-2.5 text-body text-fg-2">
            <input type="checkbox" className="accent-accent" checked={!!form[fl.key]} onChange={(e) => setForm((f) => ({ ...f, [fl.key]: e.target.checked }))} />
            {fl.label}
          </label>
        ))}
      </div>
      <div className="mb-4">
        <Label>Agent memory writes</Label>
        <div className="mt-1 space-y-2.5">
          {writeModes.map((m) => (
            <label key={m.value} className="flex cursor-pointer gap-2.5 text-body text-fg-2">
              <input
                type="radio"
                name="memory_write_mode"
                className="accent-accent mt-0.5"
                checked={(form.memory_write_mode ?? "review") === m.value}
                onChange={() => setForm((f) => ({ ...f, memory_write_mode: m.value }))}
              />
              <span>
                {m.label}
                <span className="mt-0.5 block text-small leading-snug text-faint">{m.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </div>
      <div className="mb-4">
        <Label>Memory auto-triage</Label>
        <div className="mt-1 space-y-2.5">
          {triageFlags.map((fl) => (
            <label key={fl.key} className={cn("flex cursor-pointer gap-2.5 text-body text-fg-2", fl.disabled && "cursor-not-allowed opacity-55")}>
              <input type="checkbox" className="accent-accent mt-0.5" disabled={fl.disabled} checked={!!form[fl.key]} onChange={(e) => setForm((f) => ({ ...f, [fl.key]: e.target.checked }))} />
              <span>
                {fl.label}
                {fl.hint && <span className="mt-0.5 block text-small leading-snug text-faint">{fl.hint}</span>}
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
    <div className="flex items-center gap-3 text-body">
      <span className="w-24 flex-none font-mono text-[10px] uppercase tracking-wide text-faint">{label}</span>
      <span className="text-fg-2">{value}</span>
    </div>
  );
}
