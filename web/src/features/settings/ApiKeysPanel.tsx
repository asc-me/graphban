import { useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, Copy } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { CLIENTS, McpInstall, type KeyScope } from "@/features/settings/McpInstall";
import { SyncCredentialInstall } from "@/features/settings/SyncCredentialInstall";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { copyText } from "@/lib/clipboard";
import { errorDetail } from "@/lib/errors";
import { keys, useApiKeys } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { ApiKey } from "@/lib/types";

/**
 * Settings → API keys (2026-11-04 design).
 *
 * Two tabs: **Set up** (the `gban` commands, then hand-minting per kind) and **Keys** (the
 * registry, with row actions chosen by what minted the row). Agent keys talk to the MCP
 * endpoint; link (`sync`) keys only push a code graph (AL-219 D4); `gate` keys attest that
 * work was checked (GRPH-580) and must never reach the agent doing the work.
 */
type KeyKind = "agent" | "sync" | "gate";
type Tab = "setup" | "keys";

const INTEGRATION_DOCS = "https://github.com/asc-me/graphban/tree/main/docs";

/**
 * The optional MCP tool tiers (GRPH-571), mirroring `app/services/tool_tiers.TIER_PURPOSE`.
 *
 * Duplicated rather than fetched, and that is a real trade: the server is authoritative and
 * these can drift. The guard is that the server REFUSES an unknown tier with a 422 naming the
 * allowed set, so a stale entry here fails loudly at mint.
 */
const TOOL_TIERS: ReadonlyArray<readonly [string, string, string]> = [
  ["prd", "PRDs", "Authoring and grilling specs — write, grill, decompose, close. A coding agent reads specs; it does not write them."],
  ["codegraph", "Code graph writes", "Describing symbols and linking them. Reading the graph is core and always present."],
  ["fleet", "Fleet admin", "Running a fleet — allocation, roles, enrolment codes, waves. Being in a fleet needs a seat, not this tier."],
  ["misc", "Occasional", "Projects, digests, lessons, memory review. Rare, and none of it mid-task."],
];
const TIER_LABEL: Record<string, string> = Object.fromEntries(TOOL_TIERS.map(([id, label]) => [id, label]));

const GBAN_COMMANDS: ReadonlyArray<readonly [string, string]> = [
  ["gban login", "Once per machine, at a real terminal. An agent cannot do this."],
  ["gban setup", "From inside the repo — the directory names the project; --project wins. Mints the project key, writes Claude (~/.claude.json, per-project mcpServers) and Grok (~/.grok/config.toml, mcp_servers), installs the supervisor and the skills, and verifies. Restart the session afterwards: MCP servers are read at startup."],
  ["gban setup --auto", "Every project whose repo sits here or beside here: this directory, what is in it, and its siblings, one level. Two directories for one project, or one directory for two projects, are refused."],
];

const KIND_INFO: Record<KeyKind, {
  title: string; desc: string; mint: string; nameLabel: string; ph: string; hint: string; tag?: string;
  targetLabel?: string; targetNote?: string;
}> = {
  agent: {
    title: "Agent key", desc: "For an MCP client gban setup can’t write — global by default, pinned only if you choose.",
    mint: "Mint agent key", nameLabel: "Agent key name", ph: "Agent key name (e.g. claude-code)", hint: "Shown in this list and in gban keys.",
  },
  sync: {
    title: "Link key", desc: "Links one local deployment to one project. Pushes a code graph, nothing else.",
    mint: "Mint link key", nameLabel: "Link key name", ph: "Link key name (e.g. laptop — acme-core)", hint: "This is the deployment’s identity.",
    targetLabel: "Link key target project",
    targetNote: "A link key targets exactly one project, so a key handed to a fleet can only ever push there.",
  },
  gate: {
    title: "Gate key", desc: "For CI and integrations that attest checked work — GitHub, Swamp and others.",
    mint: "Mint gate key", nameLabel: "Gate key name", ph: "Gate key name (e.g. github-actions)", hint: "Name it after the CI that will hold it.",
    tag: "INTEGRATION", targetLabel: "Gate target project",
    targetNote: "A gate key attests in exactly one project, so a leaked CI secret cannot complete work elsewhere.",
  },
};

const kindOf = (k: ApiKey): KeyKind =>
  k.scopes?.includes("sync") ? "sync" : k.scopes?.includes("gate") ? "gate" : "agent";

/** The command a gban-minted key is replaced by. `--replace` is what makes swamp overwrite. */
const RERUN: Record<string, string> = {
  gban_setup: "gban setup",
  gban_swamp_setup: "gban swamp setup --replace",
};

const REVOKE_WHAT: Record<KeyKind, string> = {
  agent: "Every MCP client using this key stops on its next call. Nothing else is touched.",
  sync: "This unlinks the deployment that holds it. It stops pushing its code graph.",
  gate: "CI holding this key can no longer attest, so items it would have passed stay short of done.",
};
const ROTATE_WHERE: Record<KeyKind, string> = {
  agent: "~/.claude.json · ~/.grok/config.toml · ~/.cursor/mcp.json · ~/.codex/config.toml",
  sync: "",
  gate: "the CI secret GRAPHBAN_GATE_KEY",
};
const ROTATE_WHAT: Record<KeyKind, string> = {
  agent: "The old key stops working immediately. You’ll need to update the MCP config of every client using it.",
  sync: "The old key stops working immediately. You’ll need to re-link the deployment that holds it.",
  gate: "The old key stops working immediately. You’ll need to update the CI secret before the next run attests.",
};

/** "Nov 10, 2026" — the row/expiry format; the uppercase is CSS on the mono label. */
const shortDate = (d: Date) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

const fieldLabel = "mb-1 block text-body text-fg-2";
const selectCls = "w-[380px] max-w-full rounded-md border border-control bg-surface-3 px-2 py-1.5 text-[12px] text-muted";

function useCopied() {
  const [copied, setCopied] = React.useState<string | null>(null);
  const copy = (id: string, text: string) =>
    copyText(text).then((ok) => {
      if (!ok) return;
      setCopied(id);
      setTimeout(() => setCopied((c) => (c === id ? null : c)), 1500);
    });
  return { copied, copy };
}

function CopyButton({ label, text, id, copied, copy }: {
  label: string; text: string; id: string; copied: string | null; copy: (id: string, text: string) => void;
}) {
  return (
    <button
      aria-label={label}
      onClick={() => copy(id, text)}
      className="flex-none rounded-md border border-control bg-surface-3 p-1.5 text-muted hover:text-fg"
    >
      {copied === id ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
    </button>
  );
}

const chip = "rounded border px-1.5 py-px font-mono text-meta uppercase tracking-wide";

/**
 * What to do with a gate key, which is NOT what to do with an agent key. Routing it to
 * `McpInstall` would hand a completion-attesting credential to the agent doing the work —
 * the arrangement the scope exists to prevent. The consumer is `scripts/attest_ci.py`.
 *
 * `plaintext` is null on an existing row: the value is copyable only while it is in hand.
 */
export function GateCiTable({ plaintext, prefix }: { plaintext: string | null; prefix: string }) {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const [url, setUrl] = React.useState(origin);
  const { copied, copy } = useCopied();
  const shown = url.trim() || "<runner URL>";
  const cmd1 = `gh variable set GRAPHBAN_URL --repo <owner/repo> --body ${shown}`;
  const cmd2 = "gh secret set GRAPHBAN_GATE_KEY --repo <owner/repo>";
  const rows: Array<{ kind: string; name: string; value: string; copyable: boolean }> = [
    { kind: "VARIABLE", name: "GRAPHBAN_URL", value: shown, copyable: true },
    {
      kind: "SECRET", name: "GRAPHBAN_GATE_KEY",
      value: plaintext ?? `the key starting ${prefix} (shown once at mint)`, copyable: plaintext !== null,
    },
  ];
  return (
    <div className="mt-3 border-t border-line-2 pt-3" data-testid="gate-ci-table">
      <p className="mb-2 text-[12px] text-muted">
        Store in CI — <span className="text-fg-2">not</span> in an agent’s MCP config.{" "}
        <code className="font-mono text-small text-fg-2">scripts/attest_ci.py</code> reads these after the check that
        decides CI is green. The name and the value go in separate fields. An integration with its own setup (Swamp:{" "}
        <code className="font-mono text-small">gban swamp setup</code>) mints its key there — see{" "}
        <a className="text-fg-2 underline-offset-2 hover:underline" href={INTEGRATION_DOCS}>Integration docs</a>.
      </p>
      <label className="mb-1 block font-mono text-[10px] uppercase tracking-wide text-faint" htmlFor="ci-runner-url">
        URL your CI runner reaches
      </label>
      <Input id="ci-runner-url" value={url} onChange={(e) => setUrl(e.target.value)} className="mb-1 max-w-sm" />
      <p className="mb-2 text-small text-faint">
        Often not this page’s address, e.g. 127.0.0.1:8080 on the ledger box, or graphban.local:8080 from another
        machine. Check with <code className="font-mono">curl &lt;url&gt;/health</code> from the runner.
      </p>
      <table className="mb-2 w-full text-left text-[12px]">
        <thead>
          <tr className="font-mono text-[10px] uppercase tracking-wide text-faint">
            <th className="py-1 pr-3">Kind</th><th className="py-1 pr-3">Name</th><th className="py-1">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name} className="border-t border-line-2 align-top">
              <td className="py-1.5 pr-3 font-mono text-[10px] text-muted">{r.kind}</td>
              <td className="py-1.5 pr-3">
                <span className="flex items-center gap-1.5">
                  <code className="font-mono text-fg-2">{r.name}</code>
                  <CopyButton label={`Copy ${r.name} name`} text={r.name} id={`n-${r.name}`} copied={copied} copy={copy} />
                </span>
              </td>
              <td className="py-1.5">
                <span className="flex items-center gap-1.5">
                  <code className={cn("break-all font-mono", r.copyable ? "text-fg-2" : "text-muted")}>{r.value}</code>
                  {r.copyable && (
                    <CopyButton label={`Copy ${r.name} value`} text={r.value} id={`v-${r.name}`} copied={copied} copy={copy} />
                  )}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {[cmd1, cmd2].map((c, i) => (
        <div key={c} className="mb-1.5 flex items-center gap-2">
          <pre className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 p-2 font-mono text-small text-fg-2">{c}</pre>
          <CopyButton label={i === 0 ? "Copy gh variable command" : "Copy gh secret command"} text={c} id={`c${i}`} copied={copied} copy={copy} />
        </div>
      ))}
      <p className="text-small text-faint">
        This prompts for the value, so it never lands in shell history. Needs a self-hosted runner that can reach this
        URL, and a workflow that runs <code className="font-mono">scripts/attest_ci.py</code>.
      </p>
    </div>
  );
}

/** A global key cannot name its project, so the checkout does — in the repo's AGENTS.md. */
const agentsSnippet = (projectId: string) =>
  `## Graphban project\n\nproject_id: ${projectId}\n\nPass this project_id on every Graphban MCP call.`;

function AgentsSnippet({ projectId }: { projectId: string }) {
  const { copied, copy } = useCopied();
  const text = agentsSnippet(projectId);
  return (
    <div className="mt-3">
      <div className="mb-1 font-mono text-[10px] tracking-wide text-faint">AGENTS.md · {projectId}</div>
      <div className="flex items-start gap-2">
        <pre className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 p-2 font-mono text-small text-fg-2">{text}</pre>
        <CopyButton label="Copy AGENTS.md project" text={text} id="agents" copied={copied} copy={copy} />
      </div>
    </div>
  );
}

type Created = { plaintext: string; kind: KeyKind; projectId: string; scope: KeyScope; prefix: string };

function MintedBanner({ created, onClose }: { created: Created; onClose: () => void }) {
  const { copied, copy } = useCopied();
  return (
    <div className="mb-4 rounded-[11px] border border-accent/40 bg-[rgba(198,242,78,0.06)] p-3">
      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-accent">Copy now — shown once</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 overflow-x-auto font-mono text-[12px] text-fg-2">{created.plaintext}</code>
        <CopyButton label="Copy key" text={created.plaintext} id="key" copied={copied} copy={copy} />
      </div>
      <p className="mt-1 text-small text-faint">Never shown again. Closing this removes it from the page.</p>
      {created.kind === "sync" ? (
        <SyncCredentialInstall apiKey={created.plaintext} projectId={created.projectId} />
      ) : created.kind === "gate" ? (
        <GateCiTable plaintext={created.plaintext} prefix={created.prefix} />
      ) : (
        <>
          <McpInstall apiKey={created.plaintext} keyScope={created.scope} />
          {created.scope === "global" && created.projectId && <AgentsSnippet projectId={created.projectId} />}
        </>
      )}
      <div className="mt-3">
        <Button size="sm" onClick={onClose}>I’ve copied it — close</Button>
      </div>
    </div>
  );
}

function MintForm({ kind, onMinted, onCancel }: {
  kind: KeyKind; onMinted: (c: Created) => void; onCancel: () => void;
}) {
  const { active, projects } = useProjectCtx();
  const qc = useQueryClient();
  const info = KIND_INFO[kind];
  const [name, setName] = React.useState("");
  const [scope, setScope] = React.useState<KeyScope>("project");
  const [pin, setPin] = React.useState("");
  const [checkout, setCheckout] = React.useState(active?.id ?? "");
  const [tiers, setTiers] = React.useState<string[]>([]);
  const [expiry, setExpiry] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const pinned = kind !== "agent" || scope === "project";

  async function mint() {
    if (!name.trim()) return;
    setError(null);
    if (pinned && !pin) {
      setError(`Pick a project — ${kind === "agent" ? "a pinned agent key" : kind === "gate" ? "a gate key" : "a link key"} must target exactly one.`);
      return;
    }
    setBusy(true);
    try {
      // `["gate"]` alone mints a DEAD key: attest goes through `update_item`, which needs
      // `write`. `fleet.mint` carries read+write alongside `gate` for the same reason.
      const scopes = kind === "sync" ? ["sync"] : kind === "gate" ? ["read", "write", "gate"] : undefined;
      const res = await api.createApiKey(
        name.trim(), pinned ? pin : null, expiry, scopes, kind === "agent" ? tiers : undefined, "web");
      qc.invalidateQueries({ queryKey: keys.apiKeys });
      onMinted({
        plaintext: res.plaintext, kind, scope, prefix: res.prefix ?? "",
        projectId: pinned ? pin : checkout,
      });
    } catch (e) {
      setError(`The server refused the mint: ${errorDetail(e, "no reason given")}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-[11px] border border-line-2 bg-surface-2 p-3" data-testid={`mint-form-${kind}`}>
      <label className={fieldLabel} htmlFor={`name-${kind}`}>
        Name
      </label>
      <Input id={`name-${kind}`} aria-label={info.nameLabel} value={name} onChange={(e) => setName(e.target.value)} placeholder={info.ph} className="w-[380px] max-w-full" />
      <p className="mb-3 mt-1 text-small text-faint">{info.hint}</p>

      {kind === "agent" && (
        <div className="mb-3">
          <div className="mb-1 text-body text-fg-2">Scope</div>
          <div className="mb-1.5 flex gap-1.5" role="group" aria-label="Key scope">
            {([["project", "One project"], ["global", "Global"]] as const).map(([id, label]) => (
              <button
                key={id}
                aria-pressed={scope === id}
                onClick={() => setScope(id)}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
                  scope === id ? "border-accent/50 bg-surface-3 text-fg" : "border-control text-muted hover:text-fg-2",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="text-[11.5px] text-muted">
            {scope === "global"
              ? "One key in the user MCP config; each checkout names its project in AGENTS.md, and every call passes that project_id. A call that omits it is answered about whichever allowed project sorts first — a guess."
              : "Default. This key writes only the project you name. A harness with a per-user config (Grok’s default) will apply the pin in every repo that loads it."}
          </p>
          {scope === "global" && (
            <>
              <p className="mt-1 text-[11.5px] text-st-blocked">
                A global key can&apos;t supervise a fleet drain — the server refuses its records.
              </p>
              <label className={cn(fieldLabel, "mt-2")} htmlFor="checkout-project">
                Checkout project
              </label>
              <select
                id="checkout-project" value={checkout} onChange={(e) => setCheckout(e.target.value)}
                className={selectCls}
              >
                {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
              <p className="mt-1 text-small text-faint">Chooses the snippet, not the key. Paste it into that repo’s AGENTS.md.</p>
            </>
          )}
        </div>
      )}

      {pinned && (
        <div className="mb-3">
          <label className={fieldLabel} htmlFor={`pin-${kind}`}>
            {kind === "agent" ? "Pinned project" : "Target project"}
            <span className="text-faint"> · {kind === "agent" ? "required" : "exactly one"}</span>
          </label>
          <select
            id={`pin-${kind}`}
            aria-label={info.targetLabel ?? "Pinned project"}
            value={pin} onChange={(e) => setPin(e.target.value)}
            className={selectCls}
          >
            <option value="">Choose a project…</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          {info.targetNote && <p className="mt-1 text-[11.5px] text-muted">{info.targetNote}</p>}
        </div>
      )}

      {kind === "gate" && (
        <div className="mb-3">
          <div className="mb-1 text-body text-fg-2">Scopes</div>
          <div className="mb-1 flex gap-1.5">
            {["READ", "WRITE", "GATE"].map((s) => (
              <span key={s} className={cn(chip, s === "GATE" ? "border-accent/40 text-accent" : "border-line-2 text-fg-2")}>{s}</span>
            ))}
          </div>
          <p className="text-small text-faint">Fixed. Attest goes through <code className="font-mono">update_item</code>, which needs <code className="font-mono">write</code>.</p>
        </div>
      )}

      {kind === "agent" && (
        <div className="mb-3">
          <div className="mb-1 text-body text-fg-2">Extra tool tiers<span className="text-faint"> · optional</span></div>
          <p className="mb-1.5 text-[11.5px] text-muted">
            None selected means core-only — the default. A tier changes what <code className="font-mono">tools/list</code>{" "}
            advertises; a tool left out is still <em className="not-italic text-fg-2">callable</em>. Fixed at mint. The{" "}
            <span className="text-fg-2">core</span> tools are always shipped.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {TOOL_TIERS.map(([id, label, desc]) => {
              const on = tiers.includes(id);
              return (
                <button
                  key={id} title={desc} aria-pressed={on}
                  onClick={() => setTiers((cur) => (on ? cur.filter((x) => x !== id) : [...cur, id]))}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
                    on ? "border-accent/50 bg-surface-3 text-fg" : "border-dashed border-control text-muted hover:text-fg-2",
                  )}
                >
                  {on ? "✓" : "+"} {label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="mb-3">
        <label className={fieldLabel} htmlFor={`expiry-${kind}`}>
          Key expiry
        </label>
        <select
          id={`expiry-${kind}`}
          value={expiry ?? ""} onChange={(e) => setExpiry(e.target.value ? Number(e.target.value) : null)}
          className={selectCls}
        >
          <option value="">No expiry</option>
          <option value="30">Expires in 30 days</option>
          <option value="90">Expires in 90 days</option>
          <option value="365">Expires in 365 days</option>
        </select>
      </div>

      {/* The mint hazard (PRD-39 G6): an agent on an unnarrowed key can call `mint_enrolment`
          for a fresh identity, defeating the authorship ban. Stated here because this is where
          the all-in-one posture is actually chosen. */}
      {kind === "agent" && (
        <p className="mb-3 rounded-md border border-st-review/40 px-3 py-2 text-[12px] text-muted" data-testid="mint-hazard">
          An agent on this key can mint itself a fresh seat, and the authorship ban is keyed on identity. For a fleet,
          mint role-narrowed wave keys on <Link to="/fleet" className="text-fg-2 underline-offset-2 hover:underline">Fleet</Link>.
        </p>
      )}
      {error && <p role="alert" className="mb-2 text-[12px] text-st-blocked">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button size="sm" onClick={mint} disabled={!name.trim() || busy}>{info.mint}</Button>
      </div>
    </div>
  );
}

function SetUpTab({ openKind, setOpenKind, onMinted }: {
  openKind: KeyKind | null; setOpenKind: (k: KeyKind | null) => void; onMinted: (c: Created) => void;
}) {
  const { copied, copy } = useCopied();
  return (
    <div>
      <div className="mb-4 rounded-[11px] border border-line-2 bg-surface-2 p-3" data-testid="gban-card">
        <div className="mb-1 text-lead font-semibold text-fg">
          Set up a project with <code className="font-mono">gban</code>
        </div>
        <p className="mb-2 text-[12px] text-muted">
          This is how a repository gets a key. Run these at a terminal — this page does not do it, and this block mints nothing.
        </p>
        <div className="mb-3 rounded-[11px] border border-line-2 bg-surface-3">
          {GBAN_COMMANDS.map(([cmd, text], i) => (
            <div key={cmd} className={cn("px-3 py-2.5", i > 0 && "border-t border-line")}>
              <div className="mb-1 flex items-center gap-2">
                <code className="font-mono text-[12px] text-fg-2">{cmd}</code>
                <CopyButton label={`Copy ${cmd}`} text={cmd} id={`cmd${i}`} copied={copied} copy={copy} />
              </div>
              <p className="text-[11.5px] text-muted">{text}</p>
            </div>
          ))}
        </div>
        <div className="border-t border-line pt-3">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-faint">Not this page either</div>
          <p className="mb-1.5 text-[12px] leading-relaxed text-muted">
            <span className="text-fg-2">Wave key</span> — expires after <code className="font-mono">FLEET_KEY_DAYS</code>. Mint it with{" "}
            <code className="font-mono">gban keys mint</code> or on <Link to="/fleet" className="text-accent underline-offset-2 hover:underline">Fleet</Link>.
            End wave sweeps only keys tagged <code className="font-mono">fleet_wave</code>; a hand-minted key is never swept.
          </p>
          <p className="mb-1.5 text-[12px] leading-relaxed text-muted">
            <span className="text-fg-2">Integration gate keys</span> — GitHub, Swamp and other integrations that attest checked work.
            An integration’s own setup mints and stores its key (Swamp: <code className="font-mono">gban swamp setup</code>, into the
            Swamp vault), never into an MCP config or where <code className="font-mono">gban setup</code> wrote the agent key. An agent
            that can attest its own work turns the gate into theatre, and nothing errors when it does.{" "}
            <a className="text-accent underline-offset-2 hover:underline" href={INTEGRATION_DOCS}>Integration docs →</a>
          </p>
          <p className="text-[12px] text-muted">Which key is that agent on? <code className="font-mono">gban keys</code></p>
        </div>
      </div>

      <div className="mb-1 text-lead font-semibold text-fg">Mint a key by hand</div>
      <p className="mb-2 text-[12px] text-muted">Only when the CLI is the wrong tool. Each kind has its own form.</p>
      <div className="space-y-2">
        {(["agent", "sync", "gate"] as KeyKind[]).map((kind) => {
          const info = KIND_INFO[kind];
          const open = openKind === kind;
          return (
            <div key={kind} className={cn("rounded-[11px] border bg-surface-2 p-3", open ? "border-accent/40" : "border-line-2")}>
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[13px] font-semibold text-fg">{info.title}</span>
                    {info.tag && <span className={cn(chip, "border-st-next/40 text-st-next")}>{info.tag}</span>}
                  </div>
                  <div className="text-[11.5px] text-muted">{info.desc}</div>
                </div>
                {kind === "gate" && (
                  <a
                    href={INTEGRATION_DOCS}
                    className="flex h-8 flex-none items-center rounded-(--radius-control) border border-st-next/40 px-3 text-[12px] text-st-next hover:bg-surface-3"
                  >
                    Integration docs
                  </a>
                )}
                <Button
                  size="sm" variant={open ? "outline" : "default"} className="flex-none"
                  aria-expanded={open} aria-label={info.mint}
                  onClick={() => setOpenKind(open ? null : kind)}
                >
                  {open ? "Close" : "Mint key"}
                </Button>
              </div>
              {open && <MintForm kind={kind} onMinted={onMinted} onCancel={() => setOpenKind(null)} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * When a key was last used, or that it never has been. "never used" is the interesting
 * state: a forgotten credential is the one worth revoking, so it gets the amber, not a dash.
 */
function lastUsedLabel(at: string | null): string {
  if (!at) return "never used";
  const iso = /(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`;
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 3600) return `used ${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `used ${Math.floor(s / 3600)}h ago`;
  return `used ${Math.floor(s / 86400)}d ago`;
}

function MintedWith({ k, projectName }: { k: ApiKey; projectName: (id: string | null) => string }) {
  const scopes = k.scopes ?? [];
  const tiers = k.tool_tiers ?? [];
  const label = "w-20 flex-none font-mono text-[10px] uppercase tracking-wide text-faint";
  return (
    <div className="border-t border-line px-3 py-2.5" data-testid="minted-with">
      <div className="mb-1.5 font-mono text-meta uppercase tracking-wide text-faint">Minted with</div>
      <div className="flex flex-col gap-1.5 text-[12px]">
        <div className="flex items-baseline gap-3">
          <span className={label}>Scopes</span>
          <span className="flex flex-wrap gap-1">
            {scopes.map((s) => <span key={s} className={cn(chip, "border-line-2 text-fg-2")}>{s}</span>)}
          </span>
        </div>
        {kindOf(k) === "agent" && (
          <div className="flex items-baseline gap-3">
            <span className={label}>MCP tools</span>
            <span className="flex flex-wrap gap-1">
              <span className={cn(chip, "border-line-2 text-fg-2")}>core</span>
              {tiers.map((t) => (
                <span key={t} className={cn(chip, "border-accent/40 text-accent")}>{TIER_LABEL[t] ?? t}</span>
              ))}
              {/* The SERVER's count, never summed here from tier labels (a second copy of
                  TOOL_TIERS waiting to drift). An older server that omits it gets nothing. */}
              {typeof k.tool_count === "number" && (
                <span
                  className="self-center font-mono text-meta uppercase tracking-wide text-faint-2"
                  title="Tools in this key's tools/list. A tool outside it is still callable — tiers change what is advertised, not what is allowed."
                >
                  · {k.tool_count} tools
                </span>
              )}
            </span>
          </div>
        )}
        <div className="flex items-baseline gap-3">
          <span className={label}>Target</span>
          <span className="text-fg-2">
            {k.project_id ? `Pinned to ${projectName(k.project_id)}` : "Global — any project the owner can write"}
          </span>
        </div>
        <div className="flex items-baseline gap-3">
          <span className={label}>Expires</span>
          <span className="text-fg-2">{k.expires_at ? shortDate(new Date(k.expires_at)) : "No expiry"}</span>
        </div>
      </div>
    </div>
  );
}

type DialogKind = "revoke" | "rotate" | "rerun" | "relink";
type DialogState = { type: DialogKind; key: ApiKey } | null;

function Modal({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div role="dialog" aria-modal="true" aria-label={title}
           className="w-full max-w-md rounded-[14px] border border-line-2 bg-surface p-4">
        <div className="mb-2 text-lead font-semibold">{title}</div>
        {children}
      </div>
    </div>
  );
}

function KeyDialog({ dlg, projectName, onClose, onRelink }: {
  dlg: NonNullable<DialogState>; projectName: (id: string | null) => string;
  onClose: () => void; onRelink: () => void;
}) {
  const qc = useQueryClient();
  const k = dlg.key;
  const kind = kindOf(k);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [expiry, setExpiry] = React.useState<number | null>(k.expires_at ? 90 : null);
  const dateLabel = (d: Date) => d.toLocaleDateString();
  const newExpiry = expiry === null ? "none" : dateLabel(new Date(Date.now() + expiry * 86400000));
  const [rotated, setRotated] = React.useState<{ plaintext: string; prefix: string } | null>(null);
  const [revokedDone, setRevokedDone] = React.useState(false);
  const { copied, copy } = useCopied();
  const target = projectName(k.project_id);
  const rerunCmd = RERUN[k.origin ?? ""] ?? "";

  async function run(fn: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      qc.invalidateQueries({ queryKey: keys.apiKeys });
    } catch (e) {
      // Stays in the dialog: closing it on failure would read as the key having been dealt with.
      setError(errorDetail(e, fallback));
    } finally {
      setBusy(false);
    }
  }

  const summary = (
    <p className="mb-2 text-[11.5px] text-muted">
      <code className="font-mono">{k.prefix}…</code> · {KIND_INFO[kind].title} · {target}
    </p>
  );
  const errorLine = error && <p role="alert" className="mt-2 text-[12px] text-st-blocked">{error}</p>;

  if (dlg.type === "rotate") {
    if (rotated) {
      const claude = CLIENTS.find((c) => c.id === "claude")!.command!;
      const mcp = claude.build(`${window.location.origin}/api/mcp`, rotated.plaintext, k.project_id ? "project" : "global");
      return (
        <Modal title={`“${k.name}” rotated`}>
          <div className="rounded-[11px] border border-accent/40 bg-[rgba(198,242,78,0.06)] p-3">
            <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-accent">New key — shown once</div>
            <div className="flex items-center gap-2">
              <code className="flex-1 overflow-x-auto font-mono text-[12px] text-fg-2">{rotated.plaintext}</code>
              <CopyButton label="Copy new key" text={rotated.plaintext} id="new" copied={copied} copy={copy} />
            </div>
          </div>
          {kind === "gate" ? (
            <GateCiTable plaintext={rotated.plaintext} prefix={rotated.prefix} />
          ) : (
            <div className="mt-3 flex items-start gap-2">
              <pre className="min-w-0 flex-1 whitespace-pre-wrap break-all rounded-md border border-line-2 bg-surface-3 p-2 font-mono text-small text-fg-2">{mcp}</pre>
              <CopyButton label="Copy claude mcp add" text={mcp} id="mcp" copied={copied} copy={copy} />
            </div>
          )}
          <p className="mt-2 text-[11.5px] text-muted">
            {kind === "gate" ? "Update the secret in CI; the name stays GRAPHBAN_GATE_KEY." : "Other clients: open Connect on the new row."}{" "}
            The old key is revoked.
          </p>
          <div className="mt-3 flex justify-end"><Button size="sm" onClick={onClose}>I’ve copied it — close</Button></div>
        </Modal>
      );
    }
    return (
      <Modal title={`Rotate “${k.name}”`}>
        {summary}
        <p className="mb-2 text-[12px] text-muted">Mint a new secret and revoke this one.</p>
        <p className="mb-1 text-[12px] text-muted">
          Now <span className="text-fg-2">{k.expires_at ? dateLabel(new Date(k.expires_at)) : "No expiry"}</span>
        </p>
        <label className="mb-1 block font-mono text-[10px] uppercase tracking-wide text-faint" htmlFor="rotate-expiry">New key expiry</label>
        <div className="mb-3 flex items-center gap-3">
          <select
            id="rotate-expiry" value={expiry ?? ""} onChange={(e) => setExpiry(e.target.value ? Number(e.target.value) : null)}
            className={selectCls}
          >
            <option value="">No expiry</option>
            <option value="30">30 days from today</option>
            <option value="90">90 days from today</option>
            <option value="365">365 days from today</option>
          </select>
          <span className="text-[11.5px] text-muted">New expiry: {newExpiry}</span>
        </div>
        <div className="rounded-md border border-st-blocked/40 p-2">
          <span className="mr-2 font-mono text-[10px] uppercase tracking-wide text-st-blocked">Update</span>
          <span className="text-[12px] text-fg-2">
            {ROTATE_WHAT[kind]}{ROTATE_WHERE[kind] && <> Old value lives in: {ROTATE_WHERE[kind]}</>}
          </span>
        </div>
        <p className="mt-2 text-small text-faint">
          One server step: mints the replacement with the same name, scopes, project and tool tiers, and revokes this key.
        </p>
        {errorLine}
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={busy} onClick={() => run(async () => {
            const res = await api.rotateApiKey(k.id, expiry);
            setRotated({ plaintext: res.plaintext, prefix: res.prefix });
          }, "Could not rotate the key.")}>Rotate key</Button>
        </div>
      </Modal>
    );
  }

  if (dlg.type === "rerun") {
    if (revokedDone) {
      return (
        <Modal title={`“${k.name}” revoked`}>
          <p className="mb-2 text-[12px] text-muted">
            Nothing in {target} can use it now. Run this in the {target} repo to mint the replacement:
          </p>
          <div className="flex items-center gap-2">
            <pre className="flex-1 overflow-x-auto rounded-md border border-line-2 bg-surface-3 p-2 font-mono text-small text-fg-2">{rerunCmd}</pre>
            <CopyButton label={`Copy ${rerunCmd}`} text={rerunCmd} id="rerun" copied={copied} copy={copy} />
          </div>
          <div className="mt-3 flex justify-end"><Button size="sm" onClick={onClose}>Done</Button></div>
        </Modal>
      );
    }
    return (
      <Modal title={`Revoke “${k.name}”, then run ${rerunCmd}`}>
        {summary}
        <p className="text-[12px] text-fg-2">
          {k.origin === "gban_swamp_setup"
            ? "Swamp’s vault holds this key. Rotating here would leave the vault on a revoked key and mint a key nothing uses."
            : "Rotating here would leave a spare live key: gban setup finds the revoked key in its config and mints another."}{" "}
          So this page mints nothing. Revoke the key here, then run <code className="font-mono">{rerunCmd}</code> in the {target} repo.
          It mints the replacement and stores it where it stored this one.
        </p>
        {errorLine}
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          {/* The soft delete ONLY: a rotate here would mint a key that nothing stores. */}
          <Button size="sm" variant="danger" disabled={busy} onClick={() => run(async () => {
            await api.revokeApiKey(k.id);
            setRevokedDone(true);
          }, "Could not revoke the key.")}>Revoke key</Button>
        </div>
      </Modal>
    );
  }

  if (dlg.type === "relink") {
    return (
      <Modal title={`Unlink and relink “${k.name}”`}>
        {summary}
        <p className="text-[12px] text-fg-2">
          <span className="text-muted">Need a new link key?</span> A new link key registers as a new deployment identity.
          Link keys can’t be rotated: unlink this one, then mint a new key and relink. {REVOKE_WHAT.sync}
        </p>
        {errorLine}
        <div className="mt-3 flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button size="sm" variant="danger" disabled={busy} onClick={() => run(async () => {
            await api.revokeApiKey(k.id);
            onRelink();
          }, "Could not revoke the key.")}>Unlink and relink</Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Revoke “${k.name}”?`}>
      {summary}
      <p className="text-body text-fg-2">
        {REVOKE_WHAT[kind]} This cannot be undone — the row stays in the list, greyed, as history.
      </p>
      {errorLine}
      <div className="mt-3 flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={() => run(async () => {
          // The catch in `run` is the point: this was a bare await once, and a failing revoke
          // produced no toast, no error and no change (GRPH-788).
          await api.revokeApiKey(k.id);
          onClose();
        }, "Could not revoke the key.")}>Revoke key</Button>
      </div>
    </Modal>
  );
}

const GROUPS: ReadonlyArray<{ kind: KeyKind; title: string; blurb: string; empty: string }> = [
  { kind: "agent", title: "Agent keys", blurb: "Read and write items, memory, and claims over MCP.",
    empty: "Nothing can reach MCP until an agent key exists. Run gban setup in the repo, or mint one below." },
  { kind: "sync", title: "Link keys",
    blurb: "Push a code graph and nothing else. The name is the deployment’s identity; revoking it unlinks that deployment. Sync / Link mints the same object.",
    empty: "No deployment is linked." },
  { kind: "gate", title: "Gate keys",
    blurb: "Attest that work was checked, so an item may reach done. For CI or a reviewer — never the agent doing the work.",
    empty: "Nothing can attest, so completion depends on a reviewer signing off with a commit." },
];

function KeyRow({ k, projectName, onAction }: {
  k: ApiKey; projectName: (id: string | null) => string; onAction: (type: DialogKind) => void;
}) {
  const [details, setDetails] = React.useState(false);
  const [connect, setConnect] = React.useState(false);
  const kind = kindOf(k);
  const expired = k.expires_at ? new Date(k.expires_at) <= new Date() : false;

  // Which actions a row gets follows from what made it: a wave key is swept by End wave, a
  // link key is a deployment identity, and a gban-minted key is stored somewhere only that
  // command knows how to update.
  let actions: Array<[string, DialogKind]>;
  if (k.fleet_wave) actions = [["Revoke", "revoke"]];
  else if (kind === "sync") actions = [["Unlink and relink…", "relink"], ["Revoke", "revoke"]];
  else if (k.origin === "gban_setup" || k.origin === "gban_swamp_setup")
    actions = [["Revoke and rerun…", "rerun"], ["Revoke", "revoke"]];
  else actions = [["Rotate", "rotate"], ["Revoke", "revoke"]];

  return (
    <div className={cn("rounded-[11px] border border-line-2 bg-surface-2", k.revoked && "opacity-50")}>
      {/* Line 1 never wraps: the name side shrinks, the actions stay right-aligned on it. */}
      <div data-line="first" className="flex items-center gap-x-3 px-3 pt-2.5">
        <span className="max-w-[40%] flex-none truncate text-[13px] text-fg-2" title={k.name}>{k.name}</span>
        <code className="min-w-0 truncate font-mono text-small text-faint">{k.prefix}…</code>
        <span className={cn(chip, "flex-none", k.project_id ? "border-line-2 text-muted" : "border-[rgba(167,139,250,0.3)] text-purple-2")}>
          {k.project_id ? projectName(k.project_id) : "All projects"}
        </span>
        <span className={cn("ml-auto flex-none whitespace-nowrap font-mono text-[10px]", k.last_used ? "text-faint-2" : "text-amber-400")} title={k.last_used ?? undefined}>
          {lastUsedLabel(k.last_used)}
        </span>
        <button
          onClick={() => setDetails(!details)} aria-expanded={details} title="Minted with"
          className={cn("flex flex-none items-center gap-0.5 font-mono text-meta uppercase tracking-wide hover:text-fg", details ? "text-fg-2" : "text-faint")}
        >
          <ChevronRight size={12} className={cn("transition-transform", details && "rotate-90")} />
          Minted with
        </button>
        {!k.revoked && kind === "agent" && (
          <button className={cn("flex-none font-mono text-meta uppercase tracking-wide hover:text-fg", connect ? "text-accent" : "text-faint")}
                  onClick={() => setConnect(!connect)} title="Connect an agent (MCP setup)">
            Connect
          </button>
        )}
        {!k.revoked && kind === "gate" && (
          <button className={cn("flex-none font-mono text-meta uppercase tracking-wide hover:text-fg", connect ? "text-accent" : "text-faint")}
                  onClick={() => setConnect(!connect)}>
            CI secrets
          </button>
        )}
        {k.revoked ? (
          <span className="flex-none font-mono text-meta uppercase tracking-wide text-faint">revoked</span>
        ) : (
          actions.map(([label, type]) => (
            <Button key={label} size="sm" variant="outline" className="flex-none whitespace-nowrap" onClick={() => onAction(type)}>
              {label}
            </Button>
          ))
        )}
      </div>
      {/* Line 2: the meta items (expiry, wave tag, origin tag) sit under the name. */}
      <div data-line="meta" className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 pb-2.5 pt-1">
        <span className={cn("font-mono text-meta uppercase tracking-wide", expired ? "text-st-blocked" : "text-faint-2")}
              title={k.expires_at ? `Expires ${shortDate(new Date(k.expires_at))}` : undefined}>
          {!k.expires_at ? "no expiry" : expired ? <>expired <span>{shortDate(new Date(k.expires_at!))}</span></> : `expires ${shortDate(new Date(k.expires_at))}`}
        </span>
        {k.fleet_wave && (
          <span className="font-mono text-meta uppercase tracking-wide text-[color:var(--color-st-review)]"
                title="End wave sweeps this and never a hand-minted key">
            {k.fleet_wave} · swept by End wave
          </span>
        )}
        {k.origin === "gban_setup" && <span className={cn(chip, "border-line-2 text-muted")}>via gban setup</span>}
        {k.origin === "gban_swamp_setup" && <span className={cn(chip, "border-line-2 text-muted")}>via gban swamp setup</span>}
      </div>
      {details && <MintedWith k={k} projectName={projectName} />}
      {connect && kind === "agent" && (
        <div className="border-t border-line px-3 pb-3">
          <McpInstall apiKey="<YOUR_API_KEY>" keyPrefix={k.prefix} keyScope={k.project_id ? "project" : "global"} />
        </div>
      )}
      {connect && kind === "gate" && (
        <div className="border-t border-line px-3 pb-3">
          <GateCiTable plaintext={null} prefix={k.prefix} />
        </div>
      )}
    </div>
  );
}

function KeysTab({ rows, loading, error, onRetry, projectName, onAction }: {
  rows: ApiKey[]; loading: boolean; error: string | null; onRetry: () => void;
  projectName: (id: string | null) => string; onAction: (k: ApiKey, type: DialogKind) => void;
}) {
  if (loading) {
    return (
      <div role="status" aria-label="Loading keys" className="space-y-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Loading keys…</span>
        {[0, 1, 2].map((i) => <div key={i} className="h-10 animate-pulse rounded-[11px] border border-line-2 bg-surface-2" />)}
      </div>
    );
  }
  if (error !== null) {
    return (
      <div role="alert" className="rounded-[11px] border border-st-blocked/40 p-3">
        <div className="mb-1 font-mono text-[10px] uppercase tracking-wide text-st-blocked">Not loaded</div>
        <p className="mb-2 text-[12px] text-muted">
          Couldn’t load your keys. Nothing here means nothing is known, not that no key exists. {error}
        </p>
        <Button size="sm" onClick={onRetry}>Retry</Button>
      </div>
    );
  }
  return (
    <div>
      {GROUPS.map((g) => {
        // Revoked rows stay (greyed) — dropping them would make a deliberately revoked
        // credential look like one that never existed — but only live ones are counted.
        const inGroup = rows
          .filter((k) => kindOf(k) === g.kind)
          .sort((a, b) => Number(a.revoked) - Number(b.revoked) || Date.parse(b.created_at) - Date.parse(a.created_at));
        const live = inGroup.filter((k) => !k.revoked).length;
        return (
          <section key={g.kind} className="mb-5" aria-label={g.title}>
            <div className="mb-1 flex items-center gap-2">
              <h3 className="text-lead font-semibold text-fg">{g.title}</h3>
              <span className="font-mono text-[10px] text-faint" aria-label={`${g.title} count`}>{live}</span>
            </div>
            <p className="mb-2.5 max-w-[74ch] text-[11.5px] leading-relaxed text-muted">{g.blurb}</p>
            <div className="space-y-2">
              {inGroup.map((k) => (
                <KeyRow key={k.id} k={k} projectName={projectName} onAction={(t) => onAction(k, t)} />
              ))}
              {inGroup.length === 0 && (
                <div className="rounded-[11px] border border-dashed border-line-2 p-3">
                  <span className="mr-2 font-mono text-[10px] uppercase tracking-wide text-faint">None</span>
                  <span className="text-[12px] text-muted">{g.empty}</span>
                </div>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}

export function ApiKeysPanel() {
  const { data: apiKeys = [], isPending, isError, error, refetch } = useApiKeys();
  const { projects } = useProjectCtx();
  const [tab, setTab] = React.useState<Tab>("setup");
  const [openKind, setOpenKind] = React.useState<KeyKind | null>(null);
  const [created, setCreated] = React.useState<Created | null>(null);
  const [dlg, setDlg] = React.useState<DialogState>(null);
  const projectName = (id: string | null) => (id ? (projects.find((p) => p.id === id)?.name ?? id) : "All projects");
  const liveCount = apiKeys.filter((k) => !k.revoked).length;
  const tabs: Array<[Tab, string]> = [["setup", "Set up"], ["keys", "Keys"]];

  return (
    <div className="mb-6 max-w-2xl">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-title font-semibold">API keys</h2>
        <div className="flex items-center gap-3">
          {/* AI Providers' "Looking for API keys?" is the other direction. This page mints
              Graphban keys; LLM credentials (what the box runs on) live there. */}
          <Link to={settingsPath("deployment/providers")} className="text-[12px] font-normal text-muted transition-colors hover:text-fg-2">
            Looking for LLM credentials?
          </Link>
          <Link to="/fleet" className="text-[12px] font-normal text-muted transition-colors hover:text-fg-2">
            Looking for seats?
          </Link>
        </div>
      </div>
      <div role="tablist" className="mb-4 mt-3 flex gap-5 border-b border-line-2">
        {tabs.map(([id, label]) => (
          <button
            key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className={cn(
              "-mb-px flex items-center gap-1.5 border-b-2 px-0.5 pb-2 text-body transition-colors",
              tab === id ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg-2",
            )}
          >
            {label}
            {id === "keys" && <span className="font-mono text-[10px] text-faint" data-testid="keys-count">{liveCount}</span>}
          </button>
        ))}
      </div>
      {created && <MintedBanner created={created} onClose={() => setCreated(null)} />}
      {tab === "setup" ? (
        <SetUpTab openKind={openKind} setOpenKind={setOpenKind}
                  onMinted={(c) => { setCreated(c); setOpenKind(null); }} />
      ) : (
        <KeysTab
          rows={apiKeys} loading={isPending} error={isError ? errorDetail(error, "The server did not say why.") : null}
          onRetry={() => void refetch()} projectName={projectName}
          onAction={(key, type) => setDlg({ type, key })}
        />
      )}
      {dlg && (
        <KeyDialog
          dlg={dlg} projectName={projectName} onClose={() => setDlg(null)}
          onRelink={() => { setDlg(null); setTab("setup"); setOpenKind("sync"); }}
        />
      )}
    </div>
  );
}
