import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Link2 } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { SyncCredentialInstall } from "@/features/settings/SyncCredentialInstall";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import { keys, useApiKeys } from "@/lib/queries";
import { adminPath } from "@/lib/routes";
import { NavLink } from "react-router-dom";

/**
 * Hosted Settings → Sync / Link. This org is the cloud side of the link.
 *
 * The self-host panel (`SyncLinkPanel`) pastes a URL and a key. Mounting that here
 * told an operator already on the cloud to "connect this self-hosted instance" —
 * the wrong docs on the page that should mint the key the box pastes.
 */
export function CloudOrgLinkPanel() {
  const { active, projects } = useProjectCtx();
  const { data: apiKeys = [] } = useApiKeys();
  const qc = useQueryClient();
  const [name, setName] = React.useState("");
  const [projectId, setProjectId] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<{ plaintext: string; projectId: string } | null>(
    null,
  );

  const target = projectId || active?.id || projects[0]?.id || "";
  const syncKeys = apiKeys.filter((k) => k.scopes?.includes("sync") && !k.revoked);
  const projectName = (id: string | null) =>
    id ? (projects.find((p) => p.id === id)?.name ?? id) : "—";

  async function mint() {
    if (!name.trim()) return;
    if (!target) {
      setError("Pick a project — a link key is pinned to exactly one.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.createApiKey(name.trim(), target, null, ["sync"]);
      setCreated({ plaintext: res.plaintext, projectId: target });
      setName("");
      qc.invalidateQueries({ queryKey: keys.apiKeys });
    } catch (e) {
      setError(errorDetail(e, "Could not mint the link key."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight">Cloud link</h2>
        <p className="mt-1 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
          This org is the cloud side. A self-hosted box links here with a{" "}
          <span className="text-fg-2">link key</span> you mint below — a{" "}
          <code className="font-mono text-[11.5px] text-fg-2">sync</code>-scoped credential
          pinned to one project. Paste it on the box; this org never reaches in.
        </p>
      </div>

      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-3.5 flex items-center gap-2.5">
          <KeyRound size={16} className="text-accent" />
          <div className="text-[14px] font-semibold">Mint a link key</div>
        </div>
        <p className="mb-3 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
          The name is the deployment&rsquo;s identity on{" "}
          <NavLink to={adminPath("deployments")} className="text-fg-2 underline-offset-2 hover:underline">
            Deployments
          </NavLink>
          . Pin the project the box will push — a key handed to a fleet can only ever push
          there. Shown once.
        </p>
        {created && (
          <div className="mb-4 rounded-[11px] border border-accent/40 bg-[rgba(198,242,78,0.06)] p-3">
            <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-accent">
              Copy now — shown once
            </div>
            <code className="block overflow-x-auto font-mono text-[12px] text-fg-2">
              {created.plaintext}
            </code>
            <SyncCredentialInstall apiKey={created.plaintext} projectId={created.projectId} />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Key name (e.g. laptop — acme-core)"
            className="max-w-xs"
          />
          <select
            value={target}
            onChange={(e) => setProjectId(e.target.value)}
            className="rounded-md border border-line-2 bg-surface-3 px-2 py-1.5 text-[12px] text-muted"
            aria-label="Link key target project"
          >
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <Button size="sm" onClick={mint} disabled={busy || !name.trim()}>
            {busy ? "Minting…" : "Mint link key"}
          </Button>
        </div>
        {error && <p className="mt-2 text-[12px] text-st-blocked">{error}</p>}
      </div>

      <div className="rounded-[13px] border border-line-2 bg-surface-2 p-4">
        <div className="mb-3 flex items-center gap-2.5">
          <Link2 size={16} className="text-accent" />
          <div className="text-[14px] font-semibold">On the local box</div>
        </div>
        <ol className="max-w-[62ch] list-decimal space-y-2 pl-5 text-[12.5px] leading-relaxed text-muted">
          <li>
            Run <code className="font-mono text-[11.5px] text-fg-2">graphban link</code> with
            this org&rsquo;s URL and the plaintext, or paste both into the box&rsquo;s Settings
            → Cloud / Sync.
          </li>
          <li>
            The box builds the code graph and pushes summaries. Vectors stay on the box; this
            org re-embeds.
          </li>
          <li>
            After the first push, the key&rsquo;s name appears under Deployments. Nothing is
            linked until that paste happens.
          </li>
        </ol>
      </div>

      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
          Link keys in this org
        </div>
        {syncKeys.length === 0 ? (
          <p className="text-[12.5px] leading-relaxed text-muted">
            None minted yet. A box cannot link until one exists.
          </p>
        ) : (
          <div className="overflow-hidden rounded-[9px] border border-line bg-surface">
            {syncKeys.map((k) => (
              <div
                key={k.id}
                className="flex items-center gap-3 border-b border-line px-3.5 py-2 last:border-b-0"
              >
                <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{k.name}</span>
                <code className="font-mono text-[11px] text-faint">{k.prefix}…</code>
                <span className="font-mono text-[11px] text-muted">{projectName(k.project_id)}</span>
              </div>
            ))}
          </div>
        )}
        <p className="mt-2 text-[11px] leading-relaxed text-faint">
          Plaintext is never shown again. Rotate by minting a new key and pasting it on the box.
        </p>
      </div>
    </div>
  );
}
