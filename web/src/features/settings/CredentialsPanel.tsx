/**
 * The credentials view (PRD-25 S5): every credential on the deployment, what uses it, and
 * what state it is in.
 *
 * **One list, not two.** This replaced `AiProvidersPanel` and for a while sat above it, because
 * that panel was the only way to edit the legacy `providers` blob resolution still read. S6
 * removed that step and migrated every blob into a credential row, so the old panel became a
 * second editor for configuration nothing consults — two lists of the same thing, disagreeing.
 * It is gone.
 *
 * **A dialog, not an inline form.** Adding a credential starts by choosing a PROVIDER, the same
 * way the old catalog presented them, and only then asks for details. The alternative — one form
 * with a kind dropdown — makes the reader work out which fields matter for the kind they picked.
 *
 * **What a kind needs comes from the registry**, not a table in this file. `auth` and the
 * endpoint default already say it server-side, and a local copy would be a second source of
 * truth that drifts the first time a provider is added.
 *
 * **The tags are the point.** `used_by` says which projects point at a credential, so a delete
 * refused with a 409 was predictable from the row. `falling_back` says which of those projects
 * are NOT actually being served by it — a distinction `used_by` alone cannot make.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useCredentials, useProjects, useReindexStatus } from "@/lib/queries";
import type { AiProvider, Credential, Project } from "@/lib/types";

/** Whether this credential may be offered as default or fallback (PRD-25 D-f). */
export function selectable(c: Credential): boolean {
  // `unreachable` IS selectable — it was asked and did not answer, which is a fact about the
  // world an operator may knowingly point at. `pending_validation` is not: nobody has ever
  // established that it works, so choosing it would assert something no one has checked.
  return c.state !== "pending_validation";
}

/** What this provider needs, read from the registry rather than restated here. */
export function needsOf(p: AiProvider): { endpoint: boolean; key: boolean } {
  return { endpoint: p.kind === "ollama", key: p.auth };
}

const chip =
  "rounded border border-line-2 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide";

function StateChip({ c }: { c: Credential }) {
  if (c.state === "valid") {
    return <span className={cn(chip, "border-accent/40 text-accent")}>valid</span>;
  }
  if (c.state === "pending_validation") {
    return <span className={cn(chip, "text-muted-2")}>pending</span>;
  }
  return (
    <span className={cn(chip, "border-rose-400/40 text-rose-300")} title={c.last_error || undefined}>
      unreachable
    </span>
  );
}

function ProjectTag({ id, accent, fallingBack }: { id: string; accent: string; fallingBack: boolean }) {
  // The glow carries the project's accent, so a credential's users are recognisable at a glance
  // rather than by reading ids. A project that is NOT being served keeps its colour but is
  // struck through — it still points here, it just is not getting it.
  return (
    <span
      className={cn(chip, "border-transparent", fallingBack && "line-through opacity-70")}
      style={{
        color: accent,
        boxShadow: `0 0 0 1px ${accent}55, 0 0 6px ${accent}66`,
        background: `${accent}14`,
      }}
      title={
        fallingBack
          ? `${id} points here but is NOT being served by it — resolution falls back to the deployment default`
          : `${id} uses this credential`
      }
    >
      {id}
    </span>
  );
}

function AddCredentialDialog({
  projectId, open, onOpenChange, onAdded,
}: { projectId: string; open: boolean; onOpenChange: (v: boolean) => void; onAdded: () => void }) {
  const { data: catalog } = useQuery({ queryKey: ["ai-providers"], queryFn: () => api.aiProviders() });
  const [picked, setPicked] = React.useState<AiProvider | null>(null);
  const [baseUrl, setBaseUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [model, setModel] = React.useState("");
  const [label, setLabel] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    if (!open) {
      setPicked(null); setBaseUrl(""); setApiKey(""); setModel(""); setLabel(""); setError("");
    }
  }, [open]);

  const create = useMutation({
    mutationFn: () =>
      api.createCredential(projectId, {
        kind: picked!.id, label, base_url: baseUrl, api_key: apiKey, model,
      }),
    onSuccess: () => { onAdded(); onOpenChange(false); },
    onError: (e: Error) => setError(e.message),
  });

  // The stub is not a credential — it is what you get when there are none.
  const choices = (catalog?.providers ?? []).filter((p) => p.kind !== "stub");
  const needs = picked ? needsOf(picked) : { endpoint: false, key: false };
  const missing = picked
    ? [
        needs.endpoint && !baseUrl.trim() && !picked.base_url ? "an endpoint" : "",
        needs.key && !apiKey.trim() ? "an API key" : "",
        !model.trim() && !picked.chat_model ? "a model" : "",
      ].filter(Boolean)
    : [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{picked ? `Add ${picked.label}` : "Add a provider"}</DialogTitle>
        </DialogHeader>

        {!picked ? (
          <div className="grid grid-cols-2 gap-2" data-testid="provider-picker">
            {choices.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => { setPicked(p); setModel(p.chat_model || ""); }}
                className="flex items-center gap-2 rounded border border-line px-3 py-2 text-left hover:border-accent/50"
              >
                <span className="text-[13px] font-medium text-fg">{p.label}</span>
                <span className={cn(chip, "ml-auto text-faint")}>
                  {p.kind === "openai" ? "OpenAI-compat" : p.kind}
                </span>
              </button>
            ))}
          </div>
        ) : (
          <div className="flex flex-col gap-3" data-testid="credential-form">
            <Field label="Label">
              <Input aria-label="Label" value={label} onChange={(e) => setLabel(e.target.value)}
                placeholder={`${picked.label} key`} />
            </Field>
            {needs.endpoint && (
              <Field label="Endpoint URL">
                <Input aria-label="Endpoint" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder={picked.base_url || "https://…"} className="font-mono text-[12px]" />
              </Field>
            )}
            {needs.key && (
              <Field label="API key">
                <Input aria-label="API key" type="password" value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…" />
              </Field>
            )}
            <Field label="Model">
              <Input aria-label="Model" value={model} onChange={(e) => setModel(e.target.value)}
                placeholder={picked.chat_model} className="font-mono text-[12px]" />
            </Field>

            {missing.length > 0 && (
              <p className="text-[11px] text-amber-300">{picked.label} needs {missing.join(" and ")}</p>
            )}
            {error && <p className="text-[11px] text-rose-300" role="alert">{error}</p>}

            <div className="flex gap-2">
              <Button disabled={missing.length > 0 || create.isPending} onClick={() => create.mutate()}>
                Add credential
              </Button>
              <Button variant="ghost" onClick={() => setPicked(null)}>Back</Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10.5px] uppercase tracking-wide text-faint">{label}</span>
      {children}
    </label>
  );
}

function ReindexBanner({ projectId }: { projectId: string }) {
  const { data } = useReindexStatus(projectId);
  if (!data?.running) return null;
  const done = data.tables.reduce((n, t) => n + t.done, 0);
  const total = data.tables.reduce((n, t) => n + t.total, 0);
  return (
    <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px]" role="status">
      <p>Re-indexing, {done} of {total}.</p>
      <p className="text-[10.5px] text-amber-200/80">
        Search spans two embedding spaces until this finishes — results may be inconsistent.
      </p>
    </div>
  );
}

export function CredentialsPanel() {
  const { activeId: projectId } = useProjectCtx();
  const qc = useQueryClient();
  const { data, isLoading } = useCredentials(projectId);
  const { data: projects } = useProjects();
  const [adding, setAdding] = React.useState(false);
  const [failure, setFailure] = React.useState("");

  const accents = React.useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of (projects ?? []) as Project[]) m[p.id] = p.accent;
    return m;
  }, [projects]);

  const refresh = () => qc.invalidateQueries({ queryKey: ["credentials", projectId] });
  const onError = (e: Error) => setFailure(e.message);

  const setDefaults = useMutation({
    mutationFn: (body: Parameters<typeof api.setScopeDefaults>[1]) => api.setScopeDefaults(projectId, body),
    onSuccess: refresh, onError,
  });
  const retry = useMutation({
    mutationFn: (id: string) => api.retryCredential(projectId, id), onSuccess: refresh, onError,
  });
  const remove = useMutation({
    // A 409 naming every referencing project — surfaced verbatim, because it already says
    // exactly which projects to fix and rewording it would lose them.
    mutationFn: (id: string) => api.deleteCredential(projectId, id), onSuccess: refresh, onError,
  });

  const credentials = data?.credentials ?? [];

  return (
    <section className="flex flex-col gap-3">
      <header className="flex items-center justify-between">
        <div>
          <h2 className="text-[13px] font-medium text-fg">Credentials</h2>
          <p className="text-[11px] text-faint">
            Every provider configured on this deployment. Projects inherit the default unless they override it.
          </p>
        </div>
        <Button onClick={() => setAdding(true)}>Add provider</Button>
      </header>

      <ReindexBanner projectId={projectId} />
      <AddCredentialDialog projectId={projectId} open={adding} onOpenChange={setAdding} onAdded={refresh} />
      {failure && <p className="text-[12px] text-rose-300" role="alert">{failure}</p>}

      {isLoading && <p className="text-[12px] text-muted">Loading…</p>}
      {!isLoading && credentials.length === 0 && (
        <p className="text-[12px] text-muted">
          No credentials yet. Projects fall back to the offline stub until one is added.
        </p>
      )}

      <ul className="flex flex-col gap-1.5">
        {credentials.map((c) => (
          <li key={c.id} data-testid={`credential-${c.id}`}
              className="rounded border border-line bg-surface-2/40">
            <div className="flex flex-wrap items-center gap-2 px-3.5 py-2">
              {/* The label leads: "anthropic" stopped being a unique name the moment two keys
                  could exist. */}
              <span className="text-[13px] font-medium text-fg">{c.label || c.id}</span>
              <span className={cn(chip, "text-faint")}>{c.kind}</span>
              <span className="font-mono text-[10px] text-muted-2">{c.model}</span>

              <span className="ml-auto flex flex-wrap items-center gap-1.5">
                <StateChip c={c} />
                {c.is_default && <span className={cn(chip, "border-sky-400/40 text-sky-300")}>default</span>}
                {c.is_fallback && <span className={cn(chip, "border-violet-400/40 text-violet-300")}>fallback</span>}
                {c.is_embed && <span className={cn(chip, "border-teal-400/40 text-teal-300")}>embedding</span>}
                {c.used_by.map((p) => (
                  <ProjectTag key={p} id={p} accent={accents[p] ?? "#8b8b8b"}
                    fallingBack={c.falling_back.includes(p)} />
                ))}
              </span>
            </div>

            {c.state === "unreachable" && c.last_error && (
              // Never just "it failed". An operator told a credential is unreachable and not
              // why has to go and reproduce it.
              <p className="border-t border-line px-3.5 py-1.5 text-[10.5px] text-rose-300/80">
                {c.last_error}
              </p>
            )}

            <div className="flex flex-wrap gap-1.5 border-t border-line px-3.5 py-1.5">
              <TinyButton disabled={c.is_default || !selectable(c)}
                title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                onClick={() => setDefaults.mutate({ default_credential_id: c.id })}>Set default</TinyButton>
              <TinyButton disabled={c.is_fallback || !selectable(c)}
                title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                onClick={() => setDefaults.mutate({ fallback_credential_id: c.id })}>Set fallback</TinyButton>
              <TinyButton onClick={() => retry.mutate(c.id)}>Test connection</TinyButton>
              <TinyButton onClick={() => remove.mutate(c.id)}>Delete</TinyButton>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TinyButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...props}
      className="rounded border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted hover:text-fg disabled:opacity-40 disabled:hover:text-muted"
    >
      {children}
    </button>
  );
}
