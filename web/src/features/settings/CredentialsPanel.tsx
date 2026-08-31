/**
 * The credentials view (PRD-25 S5): every credential on the deployment, what uses it, and
 * what state it is in.
 *
 * **One list, not two.** This replaced `AiProvidersPanel`, which was the only editor for the
 * legacy `providers` blob resolution used to read. S6 removed that step, so the old panel
 * became a second editor for configuration nothing consults, and it is gone.
 *
 * **A dialog, not an inline form.** Adding a credential starts by choosing a PROVIDER and only
 * then asks for details. One form with a kind dropdown makes the reader work out which fields
 * matter for the kind they picked.
 *
 * **What a kind needs comes from the registry**, not a table in this file — `auth` and the
 * endpoint default already say it, and a local copy drifts the first time a provider is added.
 *
 * **A health dot, not a chip.** State is a property OF the credential, not another tag next to
 * the tags saying what it is used for; putting it beside the model reads as "this thing, and
 * whether it works". Colour alone never carries it — every dot has a text label for anyone not
 * distinguishing red from green.
 *
 * **Controls are collapsed by default.** A list exists to be read; five buttons per row on a
 * deployment with a dozen credentials is a wall. The chevron opens the row that is being acted
 * on.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { keys, useCredentials, useProjects, useReindexStatus } from "@/lib/queries";
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

const HEALTH: Record<Credential["state"], { colour: string; label: string }> = {
  valid: { colour: "#4ade80", label: "valid" },
  pending_validation: { colour: "#fbbf24", label: "pending validation" },
  unreachable: { colour: "#f87171", label: "unreachable" },
};

function HealthDot({ c }: { c: Credential }) {
  const h = HEALTH[c.state];
  return (
    <span
      // Labelled, not colour-only: a dot that means nothing to a red-green colourblind reader
      // would be worse than the chip it replaced.
      role="img"
      aria-label={h.label}
      title={c.state === "unreachable" && c.last_error ? `${h.label}: ${c.last_error}` : h.label}
      className="inline-block h-2 w-2 flex-none rounded-full"
      style={{ background: h.colour, boxShadow: `0 0 6px ${h.colour}99` }}
    />
  );
}

function ProjectTag({ id, accent, fallingBack }: { id: string; accent: string; fallingBack: boolean }) {
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10.5px] uppercase tracking-wide text-faint">{label}</span>
      {children}
    </label>
  );
}

function TinyButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...props}
      className={cn("rounded border border-line-2 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wide text-muted hover:text-fg disabled:opacity-40 disabled:hover:text-muted", className)}
    >
      {children}
    </button>
  );
}

// ---- add ---------------------------------------------------------------------------------

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
    if (!open) { setPicked(null); setBaseUrl(""); setApiKey(""); setModel(""); setLabel(""); setError(""); }
  }, [open]);

  const create = useMutation({
    mutationFn: () => api.createCredential(projectId, {
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
        needs.endpoint && !baseUrl.trim() ? "an endpoint" : "",
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
              <button key={p.id} type="button"
                onClick={() => {
                  setPicked(p);
                  setModel(p.chat_model || "");
                  setBaseUrl(p.base_url || "");
                }}
                className="flex items-center gap-2 rounded border border-line px-3 py-2.5 text-left hover:border-accent/50">
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

// ---- edit --------------------------------------------------------------------------------

function EditCredentialDialog({
  projectId, credential, onOpenChange, onSaved,
}: {
  projectId: string; credential: Credential | null;
  onOpenChange: (v: boolean) => void; onSaved: () => void;
}) {
  const [apiKey, setApiKey] = React.useState("");
  const [model, setModel] = React.useState("");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    setApiKey("");
    setModel(credential?.model ?? "");
    setBaseUrl(credential?.base_url ?? "");
    setError("");
  }, [credential]);

  const save = useMutation({
    mutationFn: () => api.updateCredential(projectId, credential!.id, {
      // An empty key means "leave it alone" — sending "" would erase a working credential
      // because the operator opened the dialog to change the model.
      ...(apiKey.trim() ? { api_key: apiKey } : {}),
      model, base_url: baseUrl,
    }),
    onSuccess: () => { onSaved(); onOpenChange(false); },
    onError: (e: Error) => setError(e.message),
  });

  return (
    <Dialog open={credential !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit {credential?.label || credential?.id}</DialogTitle>
        </DialogHeader>
        {credential && (
          <div className="flex flex-col gap-3" data-testid="edit-form">
            <Field label="API key">
              <Input aria-label="API key" type="password" value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={credential.key_set ? "•••••••• (leave blank to keep)" : "sk-…"} />
            </Field>
            {credential.base_url && (
              <Field label="Endpoint URL">
                <Input aria-label="Endpoint" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                  className="font-mono text-[12px]" />
              </Field>
            )}
            <Field label="Model">
              <Input aria-label="Model" value={model} onChange={(e) => setModel(e.target.value)}
                className="font-mono text-[12px]" />
            </Field>
            {/* Saving re-probes server-side, so a corrected credential leaves `unreachable`
                and its retry budget resets. Worth saying, or the operator wonders whether to
                press Test connection afterwards. */}
            <p className="text-[10.5px] text-faint">Saving re-checks the provider.</p>
            {error && <p className="text-[11px] text-rose-300" role="alert">{error}</p>}
            <div className="flex gap-2">
              <Button disabled={save.isPending} onClick={() => save.mutate()}>Save</Button>
              <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---- per-project overrides ------------------------------------------------------------------

function AddRuleDialog({
  open, onOpenChange, credentials, projects, onSaved, onError,
}: {
  open: boolean; onOpenChange: (v: boolean) => void;
  credentials: Credential[]; projects: Project[];
  onSaved: () => void; onError: (e: Error) => void;
}) {
  const [pid, setPid] = React.useState("");
  const [credentialId, setCredentialId] = React.useState("");
  const [model, setModel] = React.useState("");

  // Only projects WITHOUT a rule. Offering one that already has a rule would make "add"
  // silently mean "replace".
  const available = projects.filter((p) => !p.credential_id);
  const chosen = credentials.find((c) => c.id === credentialId);

  React.useEffect(() => {
    if (!open) { setPid(""); setCredentialId(""); setModel(""); }
  }, [open]);

  const save = useMutation({
    mutationFn: () => api.setProjectCredential(pid, {
      credential_id: credentialId,
      // Empty means "use the credential's own model" — not an override of "".
      ...(model.trim() && model.trim() !== chosen?.model ? { model_override: model.trim() } : {}),
    }),
    onSuccess: () => { onSaved(); onOpenChange(false); },
    onError,
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Add an override rule</DialogTitle></DialogHeader>
        <div className="flex flex-col gap-3" data-testid="rule-form">
          <Field label="Project">
            <select data-testid="rule-project" value={pid} onChange={(e) => setPid(e.target.value)}
              className="w-full rounded border border-line-2 bg-transparent px-2 py-1.5 text-[12px]">
              <option value="">Choose a project…</option>
              {available.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
            </select>
          </Field>

          <Field label="Credential">
            <select data-testid="rule-credential" value={credentialId}
              onChange={(e) => {
                setCredentialId(e.target.value);
                // Seed the model from the credential, so the dialog shows what this rule will
                // actually use rather than an empty box the reader has to interpret.
                setModel(credentials.find((c) => c.id === e.target.value)?.model ?? "");
              }}
              className="w-full rounded border border-line-2 bg-transparent px-2 py-1.5 text-[12px]">
              <option value="">Choose a credential…</option>
              {credentials.map((c) => (
                // Provider AND model: two credentials can share a provider and differ only by
                // model, and a list showing just the provider cannot tell them apart.
                <option key={c.id} value={c.id}>
                  {(c.label || c.id)} — {c.kind} · {c.model}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Model">
            <Input aria-label="Model" value={model} onChange={(e) => setModel(e.target.value)}
              placeholder={chosen?.model || "the credential's model"}
              className="font-mono text-[12px]" />
            <p className="mt-1 text-[10.5px] text-faint">
              Leave as the credential's model unless this project needs a different one.
            </p>
          </Field>

          <div className="flex gap-2">
            <Button disabled={!pid || !credentialId || save.isPending} onClick={() => save.mutate()}>
              Add rule
            </Button>
            <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function OverrideRules({
  credentials, projects, onChanged, onError,
}: {
  credentials: Credential[]; projects: Project[];
  onChanged: () => void; onError: (e: Error) => void;
}) {
  const [adding, setAdding] = React.useState(false);

  const clear = useMutation({
    // Clearing the pointer AND the model together: a model override left behind would apply to
    // whatever the project inherits next, which is not what "remove this rule" means.
    mutationFn: (pid: string) =>
      api.setProjectCredential(pid, { credential_id: null, model_override: "" }),
    onSuccess: onChanged, onError,
  });

  // Only projects that HAVE a rule. Listing every project made the common case — most projects
  // inherit — into a wall of rows saying nothing.
  const rules = projects.filter((p) => p.credential_id);
  const byId = new Map(credentials.map((c) => [c.id, c]));

  return (
    <section className="flex flex-col gap-3" data-testid="override-rules">
      <header className="flex items-center justify-between">
        <div>
          <h3 className="text-[13px] font-medium text-fg">Override rules</h3>
          <p className="text-[11px] text-faint">
            Projects listed here use their own credential. Everything else uses the deployment default.
          </p>
        </div>
        <Button onClick={() => setAdding(true)}>Add rule</Button>
      </header>

      <AddRuleDialog open={adding} onOpenChange={setAdding} credentials={credentials}
        projects={projects} onSaved={onChanged} onError={onError} />

      {rules.length === 0 ? (
        <p className="text-[12px] text-muted">
          No overrides — every project uses the deployment default.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {rules.map((p) => {
            const c = byId.get(p.credential_id!);
            return (
              <li key={p.id} data-testid={`rule-${p.id}`}
                  className="flex items-center gap-3 rounded border border-line bg-surface-2/40 px-4 py-3">
                <span className={cn(chip, "border-transparent")}
                  style={{ color: p.accent, boxShadow: `0 0 0 1px ${p.accent}55, 0 0 6px ${p.accent}66`,
                           background: `${p.accent}14` }}>
                  {p.id}
                </span>
                <span className="text-[12px] text-muted">uses</span>
                {/* Provider AND model. A rule naming only the provider hides the thing most
                    often overridden — two projects sharing a key and wanting different
                    models is exactly what `model_override` is for. */}
                <span className="text-[13px] text-fg">{c?.label || p.credential_id}</span>
                <span className={cn(chip, "text-faint")}>{c?.kind ?? "unknown"}</span>
                <span className="font-mono text-[10.5px] text-muted-2">
                  {p.model_override || c?.model || ""}
                </span>
                {p.model_override && (
                  <span className={cn(chip, "border-amber-400/40 text-amber-300")}>model override</span>
                )}
                <TinyButton className="ml-auto" onClick={() => clear.mutate(p.id)}>Remove</TinyButton>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ---- re-index -------------------------------------------------------------------------------

function ReindexBanner({ projectId }: { projectId: string }) {
  const { data } = useReindexStatus(projectId);
  if (!data?.running) return null;
  const done = data.tables.reduce((n, t) => n + t.done, 0);
  const total = data.tables.reduce((n, t) => n + t.total, 0);
  return (
    <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3.5 py-2.5 text-[12px]" role="status">
      <p>Re-indexing, {done} of {total}.</p>
      <p className="text-[10.5px] text-amber-200/80">
        Search spans two embedding spaces until this finishes — results may be inconsistent.
      </p>
    </div>
  );
}

// ---- the panel --------------------------------------------------------------------------------

export function CredentialsPanel() {
  const { activeId: projectId } = useProjectCtx();
  const qc = useQueryClient();
  const { data, isLoading } = useCredentials(projectId);
  const { data: projects } = useProjects();
  const [adding, setAdding] = React.useState(false);
  const [editing, setEditing] = React.useState<Credential | null>(null);
  const [openRows, setOpenRows] = React.useState<Record<string, boolean>>({});
  const [failure, setFailure] = React.useState("");

  const accents = React.useMemo(() => {
    const m: Record<string, string> = {};
    for (const p of (projects ?? []) as Project[]) m[p.id] = p.accent;
    return m;
  }, [projects]);

  // **Both queries, because this panel reads both.** The credential cards come from
  // `useCredentials` and the override rules from `useProjects`, and a write touches both: a
  // pointer change alters a credential's `used_by` AND the project's own `credential_id`.
  //
  // Invalidating only the first is what made "Remove" delete the tag on the card above while
  // leaving the rule in the list — the same write, half-rendered.
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["credentials", projectId] });
    qc.invalidateQueries({ queryKey: keys.projects });
  };
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
    <div className="flex flex-col gap-6">
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
        <EditCredentialDialog projectId={projectId} credential={editing}
          onOpenChange={(v) => !v && setEditing(null)} onSaved={refresh} />
        {failure && <p className="text-[12px] text-rose-300" role="alert">{failure}</p>}

        {isLoading && <p className="text-[12px] text-muted">Loading…</p>}
        {!isLoading && credentials.length === 0 && (
          <p className="text-[12px] text-muted">
            No credentials yet. Projects fall back to the offline stub until one is added.
          </p>
        )}

        <ul className="flex flex-col gap-2">
          {credentials.map((c) => {
            const open = !!openRows[c.id];
            return (
              <li key={c.id} data-testid={`credential-${c.id}`}
                  className="rounded border border-line bg-surface-2/40">
                <button
                  type="button"
                  aria-expanded={open}
                  aria-label={`Actions for ${c.label || c.id}`}
                  onClick={() => setOpenRows((r) => ({ ...r, [c.id]: !r[c.id] }))}
                  className="flex w-full flex-wrap items-center gap-2.5 px-4 py-3 text-left"
                >
                  <ChevronRight size={14}
                    className={cn("flex-none text-faint transition-transform", open && "rotate-90")} />
                  {/* The label leads: "anthropic" stopped being a unique name the moment two
                      keys could exist. */}
                  <span className="text-[13px] font-medium text-fg">{c.label || c.id}</span>
                  <span className={cn(chip, "text-faint")}>{c.kind}</span>

                  {/* Health sits with the model: "this thing, and whether it works" — rather
                      than as another chip among the tags saying what it is used FOR. */}
                  <span className="flex items-center gap-1.5">
                    <HealthDot c={c} />
                    <span className="font-mono text-[10.5px] text-muted-2">{c.model}</span>
                  </span>

                  <span className="ml-auto flex flex-wrap items-center gap-1.5">
                    {c.is_default && <span className={cn(chip, "border-sky-400/40 text-sky-300")}>default</span>}
                    {c.is_fallback && <span className={cn(chip, "border-violet-400/40 text-violet-300")}>fallback</span>}
                    {c.is_embed && <span className={cn(chip, "border-teal-400/40 text-teal-300")}>embedding</span>}
                    {c.used_by.map((p) => (
                      <ProjectTag key={p} id={p} accent={accents[p] ?? "#8b8b8b"}
                        fallingBack={c.falling_back.includes(p)} />
                    ))}
                  </span>
                </button>

                {c.state === "unreachable" && c.last_error && (
                  <p className="border-t border-line px-4 py-2 text-[10.5px] text-rose-300/80">
                    {c.last_error}
                  </p>
                )}

                {open && (
                  <div className="flex flex-wrap gap-2 border-t border-line px-4 py-2.5 animate-fade">
                    <TinyButton disabled={c.is_default || !selectable(c)}
                      title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                      onClick={() => setDefaults.mutate({ default_credential_id: c.id })}>Set default</TinyButton>
                    <TinyButton disabled={c.is_fallback || !selectable(c)}
                      title={!selectable(c) ? "not validated yet — test the connection first" : undefined}
                      onClick={() => setDefaults.mutate({ fallback_credential_id: c.id })}>Set fallback</TinyButton>
                    <TinyButton onClick={() => setEditing(c)}>Edit</TinyButton>
                    <TinyButton onClick={() => retry.mutate(c.id)}>Test connection</TinyButton>
                    <TinyButton onClick={() => remove.mutate(c.id)}>Delete</TinyButton>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {credentials.length > 0 && (
        <OverrideRules credentials={credentials} projects={(projects ?? []) as Project[]}
          onChanged={refresh} onError={onError} />
      )}
    </div>
  );
}
