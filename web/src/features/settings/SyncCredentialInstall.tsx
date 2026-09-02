import { Check, Copy } from "lucide-react";
import * as React from "react";

import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";

type Method = "cli" | "web";

/**
 * Hand-off for a freshly minted `sync` credential (AL-219 D4). Two ways a local self-host
 * instance consumes it: the `graphban link` CLI (writes ~/.graphban/config.json) or the
 * local instance's own Settings → Sync/Link panel.
 *
 * `apiKey` is the one-time plaintext — this only renders right after creation, since the key
 * is never re-shown. `projectId` is the CLOUD project the credential is pinned to.
 */
export function SyncCredentialInstall({ apiKey, projectId }: { apiKey: string; projectId: string }) {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const [method, setMethod] = React.useState<Method>("cli");
  const [copied, setCopied] = React.useState(false);

  const cli = [
    "graphban link \\",
    `  --cloud-url ${origin} \\`,
    `  --api-key ${apiKey} \\`,
    `  --project ${projectId}`,
  ].join("\n");

  const web = [
    `Cloud URL      ${origin}`,
    `Sync API key   ${apiKey}`,
    `Project        ${projectId}`,
  ].join("\n");

  const snippet = method === "cli" ? cli : web;

  return (
    <div className="mt-3 rounded-[11px] border border-line-2 bg-surface-2 p-3">
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
        Link a local instance · link key
      </div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {(
          [
            ["cli", "graphban CLI"],
            ["web", "Local Settings → Sync/Link"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            onClick={() => {
              setMethod(id);
              setCopied(false);
            }}
            className={cn(
              "rounded-md border px-2 py-1 text-[11px] transition-colors",
              method === id ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="relative">
        <pre className="max-h-56 overflow-auto rounded-md border border-line-2 bg-surface px-2.5 py-2 pr-10 font-mono text-[10.5px] leading-relaxed text-fg-2">
          {snippet}
        </pre>
        <button
          onClick={() => copyText(snippet).then((ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)))}
          className="absolute right-1.5 top-1.5 rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
          title="Copy"
        >
          {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
        </button>
      </div>
      {method === "cli" ? (
        <p className="mt-1.5 text-[10.5px] text-faint">
          Run it where <span className="font-mono text-muted-2">DATABASE_URL</span> points at the local instance —
          e.g. <span className="font-mono text-muted-2">docker compose exec backend graphban link …</span>. Then{" "}
          <span className="font-mono text-muted-2">graphban sync</span> pushes the graph.
        </p>
      ) : (
        <p className="mt-1.5 text-[10.5px] text-faint">
          Paste these into the local instance's Settings → Sync/Link. The org field there is just a label.
        </p>
      )}
      <p className="mt-1 text-[10.5px] text-faint">
        The CLI's <span className="font-mono text-muted-2">--project</span> names the LOCAL project being pushed; the
        cloud target is resolved from this credential and is always{" "}
        <span className="font-mono text-muted-2">{projectId}</span>. Change it if the local id differs.
      </p>
      <p className="mt-1 text-[10.5px] text-faint">
        Least privilege: this key can only push a code graph to{" "}
        <span className="font-mono text-muted-2">{projectId}</span> — it can't read items, memory, or any other project.
      </p>
    </div>
  );
}
