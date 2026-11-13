/**
 * The New project wizard (GRPH-1584), local edition: a 960×640 split window with a graphic
 * panel on the left and a step rail on the right. Steps: ① Name & tag · ② Repository ·
 * ③ Model · ④ Review, then a ready card.
 *
 * Nothing is created until Review → Create project. The model choice is applied right after
 * the project exists, through the same writer the Quick Start model step uses.
 */
import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { saveModelCredential } from "@/features/onboarding/QuickStartDialog";
import { normalizeAddress } from "@/features/onboarding/quickStartProgress";
import { TagField, useTagField } from "@/features/onboarding/TagField";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { keys, useConfirmSetup } from "@/lib/queries";

export const PROJECT_ACCENTS = ["#c6f24e", "#a78bfa", "#7ca2ff", "#e0b34a", "#5fd07a", "#ff8f8f"];

type Step = "identity" | "repo" | "model" | "review";
const STEPS: Step[] = ["identity", "repo", "model", "review"];
const STEP_LABEL: Record<Step, string> = {
  identity: "Name & tag",
  repo: "Repository",
  model: "Model",
  review: "Review",
};
type Model = "offline" | "ollama" | "claude";
const MODELS: { id: Model; title: string; sub: string; desc: string }[] = [
  { id: "offline", title: "Offline", sub: "Stub", desc: "Runs as is. Chat is canned and nothing gets graded." },
  { id: "ollama", title: "Local", sub: "Ollama", desc: "A model server on your network." },
  { id: "claude", title: "Cloud", sub: "Claude", desc: "Your Anthropic API key." },
];
const MODEL_LABEL: Record<Model, string> = {
  offline: "Offline · Stub",
  ollama: "Local · Ollama",
  claude: "Cloud · Claude",
};
const COPY: Record<Step, [string, string]> = {
  identity: ["Name your project", "A project holds a tracker, agent memory, requests and PRDs. You can add more later."],
  repo: ["Point it at a repository", "Optional. Add the git remote now, or skip and add it later in Settings."],
  model: ["Pick a model", "Agents use this model for chat and grading. Change it later in Settings → AI providers."],
  review: ["Review and create", "Check the details. The tag can change later; keys already issued keep the old prefix."],
};

export const REMOTE_RE = /^(git@|https?:\/\/|ssh:\/\/)\S+$/;
const TAG_RE = /^[A-Z][A-Z0-9]{1,3}$/;

function repoSlug(repo: string): string {
  return repo.trim().replace(/^.*[:/]([^/]+\/[^/]+?)(\.git)?$/, "$1");
}

const LABEL = "mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint";

function Graphic({ step, tag, created, failed }: { step: Step; tag: string; created: boolean; failed: boolean }) {
  const T = tag || "PROJECT";
  const caption = created
    ? `CREATED · ${T}`
    : failed
      ? "NOT CREATED"
      : step === "identity"
        ? `PROJECT · ${tag || "UNNAMED"}`
        : step === "repo"
          ? `REPO → ${T}`
          : step === "model"
            ? `MODEL → ${T}`
            : `REVIEW · ${T}`;
  return (
    <div
      aria-hidden="true"
      className="hidden w-[380px] shrink-0 flex-col justify-between border-r border-line-2 bg-surface-2 p-6 md:flex"
    >
      <div className="flex items-center gap-2">
        <span className="font-semibold tracking-tight">Graphban</span>
        <span className="font-mono text-[10px] tracking-wide text-faint">LOCAL</span>
      </div>
      <div className="flex flex-col items-center gap-3">
        <div
          className={cn(
            "flex h-20 w-20 items-center justify-center rounded-[16px] border font-mono text-[18px] font-semibold",
            failed ? "border-st-blocked text-st-blocked" : created ? "border-accent text-accent" : "border-line-hover text-fg",
          )}
        >
          {tag || "—"}
        </div>
      </div>
      <span className="font-mono text-[10.5px] tracking-wide text-faint">{caption}</span>
    </div>
  );
}

export function NewProjectWizard({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const { setActiveId } = useProjectCtx();
  const confirm = useConfirmSetup();
  const [step, setStep] = React.useState<Step>("identity");
  const [name, setName] = React.useState("");
  const [accent, setAccent] = React.useState(PROJECT_ACCENTS[0]);
  const [repo, setRepo] = React.useState("");
  const [model, setModel] = React.useState<Model>("offline");
  const [ollamaUrl, setOllamaUrl] = React.useState("http://host.docker.internal:11434");
  const [claudeKey, setClaudeKey] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [createdId, setCreatedId] = React.useState("");
  const [ready, setReady] = React.useState(false);
  const [copied, setCopied] = React.useState(false);
  const tagField = useTagField(name);
  const titleRef = React.useRef<HTMLHeadingElement>(null);

  function resetAll() {
    setReady(false);
    setStep("identity");
    setName("");
    setAccent(PROJECT_ACCENTS[0]);
    setRepo("");
    setModel("offline");
    setOllamaUrl("http://host.docker.internal:11434");
    setClaudeKey("");
    setError("");
    setCreatedId("");
    setBusy(false);
    tagField.reset();
  }

  React.useEffect(() => {
    if (open) resetAll();
  }, [open]);

  React.useEffect(() => {
    titleRef.current?.focus();
  }, [step, ready]);

  const tagValid = TAG_RE.test(tagField.tag);
  const identityOk = !!name.trim() && tagValid && tagField.status.kind === "ok";
  const repoOk = !repo.trim() || REMOTE_RE.test(repo.trim());
  const modelOk = model === "offline" || (model === "ollama" ? !!ollamaUrl.trim() : claudeKey.trim().length > 8);
  const okFor: Record<Step, boolean> = { identity: identityOk, repo: repoOk, model: modelOk, review: true };
  const idx = STEPS.indexOf(step);
  const reachable = (k: Step) => STEPS.slice(0, STEPS.indexOf(k)).every((x) => okFor[x]);

  function go(k: Step) {
    if (busy || !reachable(k)) return;
    setError("");
    setStep(k);
  }

  async function create() {
    setBusy(true);
    setError("");
    let id = createdId;
    try {
      if (!id) {
        const p = await api.createProject({ name: name.trim(), tag: tagField.tag || undefined, accent });
        id = p.id;
        setCreatedId(id);
      }
      if (repo.trim()) await api.updateProject(id, { repo_url: repo.trim() });
      if (model === "ollama") await saveModelCredential(id, "ollama", { base_url: normalizeAddress(ollamaUrl) });
      else if (model === "claude") await saveModelCredential(id, "anthropic", { api_key: claudeKey.trim() });
      else await confirm.mutateAsync({ projectId: id, body: { decision: "offline" } });
      await qc.invalidateQueries({ queryKey: keys.projects });
      setReady(true);
    } catch {
      setError(
        id
          ? `${name.trim()} was created, but a follow-up step failed. Press Create project to retry it.`
          : "Could not create the project. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  function next() {
    if (!okFor[step] || busy) return;
    if (step === "review") void create();
    else setStep(STEPS[idx + 1]);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && e.target instanceof HTMLInputElement && !ready) {
      e.preventDefault();
      next();
    }
  }

  function another() {
    resetAll();
  }

  function openProject() {
    setActiveId(createdId);
    onOpenChange(false);
  }

  function copy() {
    try {
      void navigator.clipboard?.writeText("gban setup");
    } catch {
      /* clipboard unavailable */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  const title = ready ? `${name} is ready.` : COPY[step][0];
  const lede = ready ? "Next, connect the checkout on this machine." : COPY[step][1];

  const summary: [string, string, Step][] = [
    ["NAME", name, "identity"],
    ["TAG", tagField.tag, "identity"],
    ["ACCENT", accent, "identity"],
    ["REPOSITORY", repo.trim() || "None yet", "repo"],
    ["MODEL", MODEL_LABEL[model], "model"],
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex h-[640px] max-h-[92vh] w-[960px] max-w-[96vw] overflow-hidden p-0"
        onKeyDown={onKeyDown}
        aria-describedby="pw-desc"
      >
        <DialogTitle className="sr-only">New project</DialogTitle>
        <DialogDescription id="pw-desc" className="sr-only">
          Create a project in four steps.
        </DialogDescription>
        <Graphic step={step} tag={tagField.tag} created={ready} failed={!!error} />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex items-center gap-2 py-3.5 pl-6 pr-12">
            <span className="font-mono text-[10px] tracking-wide text-faint">NEW PROJECT</span>
            <span className="rounded-md border border-line-2 px-1.5 py-0.5 font-mono text-[10px] tracking-wide">LOCAL</span>
          </div>
          {!ready && (
            <ol aria-label="Steps" className="m-0 flex list-none gap-1.5 border-b border-line-2 px-6 pb-3.5">
              {STEPS.map((k, i) => {
                const cur = k === step;
                const ok = i < idx && okFor[k];
                const can = reachable(k) && !busy;
                return (
                  <li
                    key={k}
                    aria-current={cur ? "step" : undefined}
                    className={cn("flex min-w-0 flex-1 flex-col border-t-2 pt-2", cur ? "border-accent" : ok ? "border-line-hover" : "border-line-2")}
                  >
                    <button
                      type="button"
                      disabled={!can}
                      onClick={() => !cur && go(k)}
                      className={cn("flex flex-col gap-1 text-left", !can && "opacity-50")}
                    >
                      <span className="flex gap-1.5 font-mono text-[10px] tracking-wide text-faint">
                        <span className={cur ? "text-accent" : ""}>{`0${i + 1}`}</span>
                        {ok && <span className="text-st-done">✓</span>}
                      </span>
                      <span className={cn("text-[12px]", cur ? "font-semibold text-fg" : "text-muted")}>{STEP_LABEL[k]}</span>
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
          <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 pb-5 pt-5">
            <div className="flex flex-col gap-1.5">
              <h2 ref={titleRef} tabIndex={-1} className="text-[18px] font-semibold outline-none">
                {title}
              </h2>
              <p className="text-[13px] leading-relaxed text-muted">{lede}</p>
            </div>

            {!ready && step === "identity" && (
              <>
                <div>
                  <label htmlFor="pw-name" className={LABEL}>
                    Project name
                  </label>
                  <Input
                    id="pw-name"
                    value={name}
                    disabled={!!createdId}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Core Platform"
                  />
                </div>
                {createdId ? (
                  <p className="font-mono text-[12px] text-faint">Tag {tagField.tag} is fixed once the project exists.</p>
                ) : (
                  <TagField tag={tagField.tag} setTag={tagField.setTag} status={tagField.status} />
                )}
                <div>
                  <span id="pw-accent" className={cn(LABEL, "mb-2")}>
                    Accent
                  </span>
                  <div role="radiogroup" aria-labelledby="pw-accent" className="flex gap-2.5">
                    {PROJECT_ACCENTS.map((c) => (
                      <button
                        key={c}
                        type="button"
                        role="radio"
                        aria-checked={accent === c}
                        disabled={!!createdId}
                        onClick={() => setAccent(c)}
                        aria-label={`accent ${c}`}
                        className="h-7 w-7 rounded-[8px] transition-transform hover:scale-110"
                        style={{
                          background: c,
                          outline: accent === c ? "2px solid var(--color-fg)" : "none",
                          outlineOffset: "2px",
                        }}
                      />
                    ))}
                  </div>
                </div>
              </>
            )}

            {!ready && step === "repo" && (
              <>
                <div>
                  <label htmlFor="pw-repo" className={LABEL}>
                    Git remote
                  </label>
                  <Input
                    id="pw-repo"
                    value={repo}
                    onChange={(e) => setRepo(e.target.value)}
                    placeholder="git@github.com:acme/core-platform.git"
                    spellCheck={false}
                    aria-invalid={!repoOk}
                    className="font-mono"
                  />
                  <p role="status" className={cn("mt-1.5 text-[12px]", repoOk ? "text-faint" : "text-danger")}>
                    {repoOk
                      ? "SSH or HTTPS remote, as git remote -v prints it."
                      : "That doesn’t look like a git remote. Use the SSH or HTTPS form."}
                  </p>
                </div>
                <div className="rounded-[10px] border border-line-2 bg-surface-2 p-3">
                  <p className="text-body font-semibold">Why the remote matters</p>
                  <p className="mt-1 text-body leading-relaxed text-muted">
                    gban setup reads the git remote in your checkout and matches it here. Without it, each machine asks which project to use.
                  </p>
                </div>
              </>
            )}

            {!ready && step === "model" && (
              <>
                <fieldset className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <legend className="sr-only">Model</legend>
                  {MODELS.map((m) => (
                    <label
                      key={m.id}
                      className={cn(
                        "flex cursor-pointer flex-col rounded-[10px] border p-3",
                        model === m.id ? "border-accent/45 bg-accent/5" : "border-line-2 bg-surface-2",
                      )}
                    >
                      <span className="flex items-center gap-2">
                        <input type="radio" name="pw-model" value={m.id} checked={model === m.id} onChange={() => setModel(m.id)} />
                        <span className="text-[13px] font-semibold">{m.title}</span>
                        <span className="font-mono text-[10px] text-faint">{m.sub}</span>
                      </span>
                      <span className="mt-1 text-[11.5px] text-faint">{m.desc}</span>
                    </label>
                  ))}
                </fieldset>
                {model === "ollama" && (
                  <div>
                    <label htmlFor="pw-ollama" className={LABEL}>
                      Ollama URL
                    </label>
                    <Input id="pw-ollama" value={ollamaUrl} onChange={(e) => setOllamaUrl(e.target.value)} placeholder="http://localhost:11434" />
                    <p className="mt-1.5 text-[12px] text-faint">Under compose this is host.docker.internal. Natively it is localhost.</p>
                  </div>
                )}
                {model === "claude" && (
                  <div>
                    <label htmlFor="pw-claude" className={LABEL}>
                      Anthropic API key
                    </label>
                    <Input
                      id="pw-claude"
                      type="password"
                      autoComplete="off"
                      value={claudeKey}
                      onChange={(e) => setClaudeKey(e.target.value)}
                      placeholder="sk-ant-…"
                    />
                    <p className="mt-1.5 text-[12px] text-faint">Stored encrypted. Never shown again after you save.</p>
                  </div>
                )}
              </>
            )}

            {!ready && step === "review" && (
              <>
                <dl className="m-0">
                  {summary.map(([k, v, st], i) => (
                    <div key={k} className={cn("flex items-center gap-3 py-2.5", i > 0 && "border-t border-line-2")}>
                      <dt className="w-28 font-mono text-[10px] tracking-wide text-faint">{k}</dt>
                      <dd className="m-0 flex min-w-0 flex-1 items-center gap-2 truncate text-[13px]">
                        {k === "ACCENT" && <span className="h-3.5 w-3.5 rounded-[4px]" style={{ background: accent }} />}
                        {v}
                      </dd>
                      {createdId && st === "identity" ? (
                        <span className="text-[12px] text-faint">Created</span>
                      ) : (
                        <button
                          type="button"
                          aria-label={`Edit ${k.toLowerCase()}`}
                          disabled={busy}
                          onClick={() => go(st)}
                          className="text-[12px] text-muted hover:text-fg"
                        >
                          Edit
                        </button>
                      )}
                    </div>
                  ))}
                </dl>
                {error && (
                  <p role="alert" className="text-[12px] text-st-blocked">
                    {error}
                  </p>
                )}
              </>
            )}

            {ready && (
              <>
                <div className="flex items-center gap-3 rounded-[10px] border border-line-2 bg-surface-2 p-3">
                  <span
                    className="flex h-10 w-10 items-center justify-center rounded-[10px] font-mono text-[12px] font-semibold text-black"
                    style={{ background: accent }}
                  >
                    {tagField.tag}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] font-semibold">{name}</p>
                    <p className="font-mono text-small text-faint">
                      {tagField.tag} · {repo.trim() ? repoSlug(repo) : "no repository"}
                    </p>
                  </div>
                  <span className="text-st-done">✓</span>
                </div>
                <div>
                  <p className="mb-1.5 text-body text-muted">Run this in your checkout:</p>
                  <div className="flex items-center gap-2 rounded-[8px] border border-line-2 bg-surface-2 px-3 py-2 font-mono text-body">
                    <span className="flex-1">$ gban setup</span>
                    <button type="button" onClick={copy} className="text-[12px] text-muted hover:text-fg">
                      {copied ? "Copied" : "Copy"}
                    </button>
                  </div>
                  <p className="mt-1.5 text-[12px] text-faint">
                    It reads the git remote and connects this machine to {tagField.tag || "the project"}.
                  </p>
                </div>
              </>
            )}
          </div>

          <div className="flex items-center justify-between border-t border-line-2 px-6 py-3">
            {ready ? (
              <>
                <Button type="button" variant="outline" size="sm" onClick={another}>
                  Create another
                </Button>
                <Button type="button" size="sm" onClick={openProject}>
                  Open project
                </Button>
              </>
            ) : (
              <>
                <Button type="button" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
                  Cancel
                </Button>
                <div className="flex gap-2">
                  <Button type="button" variant="outline" size="sm" disabled={idx <= 0 || busy} onClick={() => go(STEPS[idx - 1])}>
                    Back
                  </Button>
                  {step === "repo" && !repo.trim() && (
                    <Button type="button" variant="outline" size="sm" onClick={() => setStep("model")}>
                      Skip
                    </Button>
                  )}
                  <Button type="button" size="sm" disabled={!okFor[step] || busy} onClick={next}>
                    {step === "review" ? (busy ? "Creating…" : "Create project") : "Next →"}
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
