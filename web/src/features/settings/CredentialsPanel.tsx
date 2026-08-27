/**
 * The credentials view (PRD-25 S5): every credential on the deployment, what uses it, and
 * what state it is in.
 *
 * Replaces `AiProvidersPanel`, which showed ONE PROJECT's providers as a table of
 * always-visible fields — so a half-filled row looked configured, and there was no way to see
 * what the deployment as a whole had.
 *
 * **A button and a dialog, not a table of blanks.** The dialog asks for exactly what a kind
 * needs: Ollama an endpoint, Anthropic a key, an OpenAI-compatible endpoint both. That mirrors
 * `_is_configured` server-side, so a saved credential is a working credential rather than a row
 * that validates and fails on the first real call.
 *
 * **The tags are the point.** `used_by` says which projects point at a credential, so a delete
 * refused with a 409 was predictable from the row. `falling_back` says which of those projects
 * are NOT actually being served by it — a distinction `used_by` alone cannot make, and the one
 * §4 insisted on when it required a fallback to announce itself.
 *
 * Everything shown here is read from the endpoint on every render. Nothing is derived locally:
 * a tag computed from a stale cache is the drift this view exists to remove.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useCredentials, useReindexStatus } from "@/lib/queries";
import type { Credential } from "@/lib/types";

/**
 * What each provider kind actually needs, mirroring `_is_configured` server-side.
 *
 * The dialog asks for these and refuses to save without them. Keeping the shape here rather
 * than inferring it from a generic form is what stops a half-filled credential being saved and
 * discovered at the first call.
 */
const NEEDS: Record<string, { endpoint: boolean; key: boolean }> = {
  ollama: { endpoint: true, key: false },
  anthropic: { endpoint: false, key: true },
  openai: { endpoint: false, key: true },
  openai_compat: { endpoint: true, key: true },
  grok: { endpoint: false, key: true },
};

function needsOf(kind: string) {
  return NEEDS[kind] ?? { endpoint: true, key: true };
}

/** Whether this credential may be offered as default or fallback (PRD-25 D-f). */
export function selectable(c: Credential): boolean {
  // `unreachable` IS selectable — it was asked and did not answer, which is a fact about the
  // world an operator may knowingly point at. `pending_validation` is not: nobody has ever
  // established that it works, so choosing it would assert something no one has checked.
  return c.state !== "pending_validation";
}

function StateBadge({ c }: { c: Credential }) {
  if (c.state === "valid") {
    return <span className="text-xs rounded px-1.5 py-0.5 bg-emerald-500/15 text-emerald-300">valid</span>;
  }
  if (c.state === "pending_validation") {
    return (
      <span className="text-xs rounded px-1.5 py-0.5 bg-amber-500/15 text-amber-300">
        pending validation
      </span>
    );
  }
  return (
    <span className="flex flex-col gap-0.5">
      <span className="text-xs rounded px-1.5 py-0.5 bg-rose-500/15 text-rose-300 w-fit">
        unreachable
      </span>
      {/* Never just "it failed". An operator told a credential is unreachable and not why has
          to go and reproduce it. */}
      {c.last_error ? (
        <span className="text-[11px] text-rose-300/80 max-w-md truncate" title={c.last_error}>
          {c.last_error}
        </span>
      ) : null}
    </span>
  );
}

function Tags({ c }: { c: Credential }) {
  const fallingBack = new Set(c.falling_back);
  return (
    <span className="flex flex-wrap gap-1 items-center">
      {c.is_default ? <span className="text-xs rounded px-1.5 py-0.5 bg-sky-500/15 text-sky-300">default</span> : null}
      {c.is_fallback ? <span className="text-xs rounded px-1.5 py-0.5 bg-violet-500/15 text-violet-300">fallback</span> : null}
      {c.is_embed ? <span className="text-xs rounded px-1.5 py-0.5 bg-teal-500/15 text-teal-300">embedding</span> : null}
      {c.used_by.map((p) => (
        <span
          key={p}
          className={cn(
            "text-xs rounded px-1.5 py-0.5",
            fallingBack.has(p)
              ? "bg-amber-500/15 text-amber-300 line-through decoration-amber-400/60"
              : "bg-white/5 text-white/70",
          )}
          title={fallingBack.has(p)
            ? `${p} points here but is NOT being served by it — resolution falls back to the deployment default`
            : `${p} uses this credential`}
        >
          {p}
        </span>
      ))}
      {c.used_by.length === 0 && !c.is_default && !c.is_fallback && !c.is_embed ? (
        <span className="text-xs text-white/40">unused</span>
      ) : null}
    </span>
  );
}

function AddDialog({ projectId, onDone }: { projectId: string; onDone: () => void }) {
  const [kind, setKind] = React.useState("anthropic");
  const [label, setLabel] = React.useState("");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [model, setModel] = React.useState("");
  const [error, setError] = React.useState("");
  const needs = needsOf(kind);

  const create = useMutation({
    mutationFn: () =>
      api.createCredential(projectId, {
        kind, label, base_url: baseUrl, api_key: apiKey, model,
      }),
    onSuccess: onDone,
    onError: (e: Error) => setError(e.message),
  });

  // The dialog refuses rather than letting the server discover it. Same predicate as
  // `_is_configured`, so "saved" means "usable".
  const missing = [
    needs.endpoint && !baseUrl.trim() ? "endpoint" : "",
    needs.key && !apiKey.trim() ? "API key" : "",
    !model.trim() ? "model" : "",
  ].filter(Boolean);

  return (
    <div className="rounded border border-white/10 p-3 flex flex-col gap-2" data-testid="add-credential">
      <div className="flex gap-2 items-center">
        <select
          aria-label="Provider kind"
          className="bg-transparent border border-white/10 rounded px-2 py-1 text-sm"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {Object.keys(NEEDS).map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <Input aria-label="Label" placeholder="label" value={label} onChange={(e) => setLabel(e.target.value)} />
      </div>
      {needs.endpoint ? (
        <Input aria-label="Endpoint" placeholder="https://…" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
      ) : null}
      {needs.key ? (
        <Input aria-label="API key" type="password" placeholder="sk-…" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
      ) : null}
      <Input aria-label="Model" placeholder="model" value={model} onChange={(e) => setModel(e.target.value)} />

      {missing.length ? (
        <p className="text-xs text-amber-300">{kind} needs {missing.join(", ")}</p>
      ) : null}
      {error ? <p className="text-xs text-rose-300">{error}</p> : null}

      <div className="flex gap-2">
        <Button disabled={missing.length > 0 || create.isPending} onClick={() => create.mutate()}>
          Add credential
        </Button>
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
    </div>
  );
}

function ReindexBanner({ projectId }: { projectId: string }) {
  const { data } = useReindexStatus(projectId);
  if (!data?.running) return null;
  const done = data.tables.reduce((n, t) => n + t.done, 0);
  const total = data.tables.reduce((n, t) => n + t.total, 0);
  return (
    <div className="rounded border border-amber-500/30 bg-amber-500/10 p-2 text-sm" role="status">
      <p>Re-indexing, {done} of {total}.</p>
      {/* The real cost of a re-index is not its duration. Saying so here rather than in a doc
          somebody finds afterwards is the point. */}
      <p className="text-xs text-amber-200/80">
        Search spans two embedding spaces until this finishes — results may be inconsistent.
      </p>
    </div>
  );
}

export function CredentialsPanel() {
  const { activeId: projectId } = useProjectCtx();
  const qc = useQueryClient();
  const { data, isLoading } = useCredentials(projectId);
  const [adding, setAdding] = React.useState(false);
  const [failure, setFailure] = React.useState("");

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["credentials", projectId] });
    setAdding(false);
  };

  const setDefaults = useMutation({
    mutationFn: (body: Parameters<typeof api.setScopeDefaults>[1]) => api.setScopeDefaults(projectId, body),
    onSuccess: refresh,
    onError: (e: Error) => setFailure(e.message),
  });
  const retry = useMutation({
    mutationFn: (id: string) => api.retryCredential(projectId, id),
    onSuccess: refresh,
    onError: (e: Error) => setFailure(e.message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteCredential(projectId, id),
    onSuccess: refresh,
    // A 409 naming every referencing project — surfaced verbatim, because it already says
    // exactly which projects to fix and rewording it would lose them.
    onError: (e: Error) => setFailure(e.message),
  });

  const credentials = data?.credentials ?? [];

  return (
    <section className="flex flex-col gap-3">
      <header className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-medium">Credentials</h2>
          <p className="text-xs text-white/50">
            Every provider configured on this deployment. Projects inherit the default unless
            they override it.
          </p>
        </div>
        <Button onClick={() => setAdding((v) => !v)}>Add provider</Button>
      </header>

      <ReindexBanner projectId={projectId} />
      {adding ? <AddDialog projectId={projectId} onDone={refresh} /> : null}
      {failure ? <p className="text-sm text-rose-300" role="alert">{failure}</p> : null}

      {isLoading ? <p className="text-sm text-white/50">Loading…</p> : null}

      {!isLoading && credentials.length === 0 ? (
        <p className="text-sm text-white/50">
          No credentials yet. Projects fall back to the offline stub until one is added.
        </p>
      ) : null}

      <ul className="flex flex-col gap-2">
        {credentials.map((c) => (
          <li key={c.id} className="rounded border border-white/10 p-3 flex flex-col gap-2"
              data-testid={`credential-${c.id}`}>
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-2">
                {/* The label leads: "anthropic" stopped being a unique name the moment two
                    keys could exist. */}
                <span className="font-medium">{c.label || c.id}</span>
                <span className="text-xs text-white/40">{c.kind}</span>
                <span className="text-xs text-white/40">{c.model}</span>
              </span>
              <StateBadge c={c} />
            </div>

            <Tags c={c} />

            <div className="flex flex-wrap gap-2">
              <Button
                variant="ghost"
                disabled={c.is_default || !selectable(c)}
                title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                onClick={() => setDefaults.mutate({ default_credential_id: c.id })}
              >
                Set default
              </Button>
              <Button
                variant="ghost"
                disabled={c.is_fallback || !selectable(c)}
                title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                onClick={() => setDefaults.mutate({ fallback_credential_id: c.id })}
              >
                Set fallback
              </Button>
              <Button variant="ghost" onClick={() => retry.mutate(c.id)}>Test connection</Button>
              <Button variant="ghost" onClick={() => remove.mutate(c.id)}>Delete</Button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
