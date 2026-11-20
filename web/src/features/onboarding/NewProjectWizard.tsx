/**
 * The New project wizard (GRPH-1584), local edition: a 960×640 split window with a graphic
 * panel on the left and a step rail on the right. Steps: ① Name & tag · ② Repository ·
 * ③ Maintenance · ④ Model · ⑤ Review, then a ready card.
 *
 * Nothing is created until Review → Create project. When Maintenance is on, the line is
 * created after the project exists (GRPH-1749). The model choice is applied right after
 * that, through the same writer the Quick Start model step uses.
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
import { type GraphicSpec, HeroDrawing, LogoMark, prefersReducedMotion, WizardKeyframes } from "@/features/onboarding/WizardGraphic";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { keys, useConfirmSetup } from "@/lib/queries";

/* Twelve accents, in hue order. Left out on purpose: operator orange (#ff8a3d) marks the
   operator plane, and the five status tokens — --color-st-done #5fd07a, --color-st-next
   #7ca2ff, --color-st-review #e0b34a, --color-st-blocked #ff6b6b, --color-st-backlog
   #8b949e — so a project tile never reads as a status. Mint, Sky, Indigo, Rose and Sand
   sit next to a status hue and are kept lighter than it. */
export const PROJECT_ACCENT_SWATCHES = [
  { hex: "#c6f24e", name: "Lime" },
  { hex: "#a6ebc4", name: "Mint" },
  { hex: "#4fd1c5", name: "Teal" },
  { hex: "#67d4f0", name: "Cyan" },
  { hex: "#a3cfff", name: "Sky" },
  { hex: "#9d9cff", name: "Indigo" },
  { hex: "#a78bfa", name: "Violet" },
  { hex: "#d58cf0", name: "Orchid" },
  { hex: "#f47fb4", name: "Pink" },
  { hex: "#ffb3c1", name: "Rose" },
  { hex: "#e6d2a8", name: "Sand" },
  { hex: "#efd76a", name: "Yellow" },
] as const;

export const PROJECT_ACCENTS = PROJECT_ACCENT_SWATCHES.map((swatch) => swatch.hex);

type Step = "identity" | "repo" | "line" | "model" | "review";
const STEPS: Step[] = ["identity", "repo", "line", "model", "review"];
const STEP_LABEL: Record<Step, string> = {
  identity: "Name & tag",
  repo: "Repository",
  line: "Maintenance",
  model: "Model",
  review: "Review",
};
type Model = "offline" | "ollama" | "claude";
const MODELS: { id: Model; title: string; sub: string; desc: string }[] = [
  { id: "offline", title: "Offline", sub: "stub", desc: "Runs as is. Chat is canned and nothing gets graded." },
  { id: "ollama", title: "Local", sub: "Ollama", desc: "A model server on your network." },
  { id: "claude", title: "Cloud", sub: "Claude", desc: "Your Anthropic API key." },
];
const MODEL_IDS = MODELS.map((m) => m.id);
const MODEL_LABEL: Record<Model, string> = {
  offline: "Offline · stub",
  ollama: "Local · Ollama",
  claude: "Cloud · Claude",
};
const LINE_CHOICES: { value: boolean; title: string; desc: string }[] = [
  {
    value: true,
    title: "Create the maintenance line",
    desc: "File bugs and issues against it from the tracker or triage. A drain can work it like a PRD.",
  },
  {
    value: false,
    title: "Not now",
    desc: "Every fix needs a PRD to be drained. You can add the line later.",
  },
];
const LINE_VALUES = LINE_CHOICES.map((choice) => choice.value);

function lineHow(tag: string): string[] {
  return [
    `Bugs and issues are filed against ${tag}-L1 instead of a PRD. Without a line, a fix with no PRD stays unscoped.`,
    "A fix goes on the line only with an acceptance clause and a file it touches, the same as a PRD item. One missing either stays unscoped and says what it needs.",
    "A drain runs on the line the way it runs on a PRD, and claims only the fixes that are ready. Security fixes carry a SECURITY chip.",
  ];
}

const COPY: Record<Step, [string, string]> = {
  identity: ["Name your project", "A project holds a tracker, agent memory, requests and PRDs. You can add more later."],
  repo: ["Point it at a repository", "Optional. Add the git remote now, or skip and add it later in Settings."],
  line: [
    "Add a maintenance line?",
    "An operations backlog for bugs and issues that don’t belong to a PRD. It works like a PRD, so a drain can run on it.",
  ],
  model: ["Pick a model", "Agents use this model for chat and grading. Change it later in Settings → AI providers."],
  review: ["Review and create", "Check the details. The tag can change later; keys already issued keep the old prefix."],
};

export const REMOTE_RE = /^(git@|https?:\/\/|ssh:\/\/)\S+$/;
const TAG_RE = /^[A-Z][A-Z0-9]{1,3}$/;

function repoSlug(repo: string): string {
  return repo.trim().replace(/^.*[:/]([^/]+\/[^/]+?)(\.git)?$/, "$1");
}

/** The WAI-ARIA radio group keyboard model (GRPH-1613, GRPH-1618). ArrowRight/ArrowDown select
 *  the next option and ArrowLeft/ArrowUp the previous, wrapping at the ends, on either axis as
 *  the pattern says; Home and End go to the first and last. Disabled options are skipped, and
 *  focus follows the checked option. A locked group ignores the keys. */
export function radioArrows<T>(
  e: React.KeyboardEvent<HTMLElement>,
  o: {
    values: readonly T[];
    current: T;
    set: (v: T) => void;
    locked?: boolean;
    disabled?: (v: T) => boolean;
  },
) {
  if (o.locked) return;
  const n = o.values.length;
  const usable = (i: number) => !o.disabled?.(o.values[i]);
  const from = o.values.indexOf(o.current);
  let i = -1;
  if (e.key === "Home" || e.key === "End") {
    const order = [...Array(n).keys()];
    if (e.key === "End") order.reverse();
    i = order.find(usable) ?? -1;
  } else {
    const step =
      e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    for (let k = 1; k <= n; k++) {
      const j = (((from + step * k) % n) + n) % n;
      if (usable(j)) {
        i = j;
        break;
      }
    }
  }
  e.preventDefault();
  if (i < 0 || i === from) return;
  o.set(o.values[i]);
  e.currentTarget.closest('[role="radiogroup"]')?.querySelectorAll<HTMLElement>('[role="radio"]')[i]?.focus();
}

const LABEL = "mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint";

/** The spec and caption the left panel draws for where the wizard is (GRPH-1587). */
export function graphicFor(o: {
  step: Step;
  name: string;
  tag: string;
  repo: string;
  repoOk: boolean;
  line: boolean;
  model: Model;
  busy: boolean;
  created: boolean;
  failed: boolean;
}): { spec: GraphicSpec; caption: string } {
  const T = o.tag || "PROJECT";
  const lineTag = o.tag || "TAG";
  if (o.created) return { spec: { kind: "single", from: "YOU", to: T, lit: true }, caption: `CREATED · ${T}` };
  if (o.failed) return { spec: { kind: "single", from: "YOU", to: T, broken: true }, caption: "NOT CREATED" };
  if (o.step === "identity") return { spec: { kind: "pulse", label: T }, caption: `PROJECT · ${o.name.trim() ? T : "UNNAMED"}` };
  if (o.step === "repo") {
    const has = !!o.repo.trim();
    return { spec: { kind: "single", from: "REPO", to: T, lit: has && o.repoOk }, caption: has ? `REPO → ${T}` : "REPO · NONE YET" };
  }
  if (o.step === "line") {
    if (o.line) {
      return {
        spec: { kind: "single", from: "FIXES", to: `${lineTag}-L1`, lit: true },
        caption: `LINE · ${lineTag}-L1 · MAINTENANCE`,
      };
    }
    return { spec: { kind: "single", from: "FIXES", to: lineTag, broken: true }, caption: "LINE · NONE · FIXES STAY UNSCOPED" };
  }
  if (o.step === "model") {
    const sel = MODELS.findIndex((m) => m.id === o.model);
    return {
      spec: { kind: "fan", from: T, to: MODELS.map((m) => m.sub.toUpperCase()), sel },
      caption: `MODEL · ${MODELS[sel].sub.toUpperCase()}`,
    };
  }
  return { spec: { kind: "single", from: "YOU", to: T }, caption: o.busy ? `CREATING · ${T}` : `REVIEW · ${T}` };
}

function Graphic({ spec, caption, accent, still }: { spec: GraphicSpec; caption: string; accent: string; still: boolean }) {
  return (
    <div aria-hidden="true" className="relative hidden w-[380px] shrink-0 overflow-hidden border-r border-line-2 bg-bg md:block">
      <div className="absolute inset-0">
        <HeroDrawing spec={spec} still={still} />
      </div>
      <div className="absolute left-[22px] top-5 flex items-center gap-2">
        <LogoMark />
        <span className="text-lead font-medium tracking-tight text-accent">Graphban</span>
        <span className="font-mono text-[10px] tracking-wide text-faint">LOCAL</span>
      </div>
      <div className="absolute bottom-5 left-[22px] right-[22px] flex items-center gap-2">
        <span data-testid="pw-accent-square" className="h-2 w-2 rounded-[2px]" style={{ background: accent }} />
        <span data-testid="pw-caption" className="font-mono text-[10.5px] tracking-wide text-faint-2">
          {caption}
        </span>
      </div>
    </div>
  );
}

function RadioDot({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn("flex h-4 w-4 items-center justify-center rounded-full border-[1.5px]", on ? "border-accent" : "border-control")}
    >
      <span className={cn("h-2 w-2 rounded-full", on ? "bg-accent" : "bg-transparent")} />
    </span>
  );
}

function AlertIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v5M12 16h.01" />
    </svg>
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
  const [line, setLine] = React.useState(false);
  const [lineMade, setLineMade] = React.useState(false);
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
  const nameRef = React.useRef<HTMLInputElement>(null);
  const [still] = React.useState(prefersReducedMotion);

  function resetAll() {
    setReady(false);
    setStep("identity");
    setName("");
    setAccent(PROJECT_ACCENTS[0]);
    setRepo("");
    setLine(false);
    setLineMade(false);
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

  // Step one lands on the name field; every other step (and the ready card) on its title.
  React.useEffect(() => {
    if (!ready && step === "identity" && nameRef.current && !nameRef.current.disabled) nameRef.current.focus();
    else titleRef.current?.focus();
  }, [step, ready]);

  const tagValid = TAG_RE.test(tagField.tag);
  const identityOk = !!name.trim() && tagValid && tagField.status.kind === "ok";
  const repoOk = !repo.trim() || REMOTE_RE.test(repo.trim());
  const modelOk = model === "offline" || (model === "ollama" ? !!ollamaUrl.trim() : claudeKey.trim().length > 8);
  const okFor: Record<Step, boolean> = { identity: identityOk, repo: repoOk, line: true, model: modelOk, review: true };
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
    // A second createLine is a 409 — the project already has one — so a later failure must
    // not run it again. The flag is what the retry consults, not the radio.
    let made = lineMade;
    let failedAt: "project" | "line" | "followup" = "project";
    try {
      if (!id) {
        const p = await api.createProject({ name: name.trim(), tag: tagField.tag || undefined, accent });
        id = p.id;
        setCreatedId(id);
      }
      failedAt = "followup";
      if (repo.trim()) await api.updateProject(id, { repo_url: repo.trim() });
      if (line && !made) {
        failedAt = "line";
        await api.createLine(id);
        made = true;
        setLineMade(true);
      }
      failedAt = "followup";
      if (model === "ollama") await saveModelCredential(id, "ollama", { base_url: normalizeAddress(ollamaUrl) });
      else if (model === "claude") await saveModelCredential(id, "anthropic", { api_key: claudeKey.trim() });
      else await confirm.mutateAsync({ projectId: id, body: { decision: "offline" } });
      await qc.invalidateQueries({ queryKey: keys.projects });
      setReady(true);
    } catch {
      setError(
        !id
          ? "Could not create the project. Please try again."
          : failedAt === "line"
            ? `${name.trim()} was created, but the maintenance line wasn’t. You can make it from the tracker’s Maintenance filter.`
            : `${name.trim()} was created, but a follow-up step failed. Press Create project to retry it.`,
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

  /** Closing after a partial failure still leaves a real project behind, so the switcher
   *  must re-read the list — the success path's invalidate never ran (GRPH-1612). */
  function close(next: boolean) {
    if (!next && createdId && !ready) void qc.invalidateQueries({ queryKey: keys.projects });
    onOpenChange(next);
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

  const T = tagField.tag;
  // [key, value, step it edits, mono value]
  const summary: [string, string, Step, boolean][] = [
    ["NAME", name, "identity", false],
    ["TAG", `${T} · ${T}-12, ${T}-R33`, "identity", true],
    ["ACCENT", accent, "identity", true],
    ["REPOSITORY", repo.trim() || "None yet", "repo", true],
    ["MAINTENANCE LINE", line ? `${T}-L1 · Maintenance` : "Not now", "line", line],
    ["MODEL", MODEL_LABEL[model], "model", false],
  ];
  const graphic = graphicFor({ step, name, tag: T, repo, repoOk, line, model, busy, created: ready, failed: !!error && !createdId });

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent
        className="flex h-[640px] max-h-[92vh] w-[960px] max-w-[96vw] overflow-hidden p-0"
        onKeyDown={onKeyDown}
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          nameRef.current?.focus();
        }}
        aria-describedby="pw-desc"
      >
        <DialogTitle className="sr-only">New project</DialogTitle>
        <DialogDescription id="pw-desc" className="sr-only">
          Create a project in five steps.
        </DialogDescription>
        <WizardKeyframes />
        <Graphic spec={graphic.spec} caption={graphic.caption} accent={accent} still={still} />
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
          <div
            key={ready ? "ready" : step}
            data-testid="pw-body"
            className="pw-anim flex min-h-0 flex-1 flex-col gap-[18px] overflow-y-auto px-6 pb-5 pt-[22px]"
            style={still ? undefined : { animation: "pwEnter .17s ease" }}
          >
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
                    ref={nameRef}
                    value={name}
                    disabled={!!createdId}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Core Platform"
                  />
                </div>
                {createdId ? (
                  <p className="font-mono text-[12px] text-faint">Tag {tagField.tag} is fixed once the project exists.</p>
                ) : (
                  <TagField tag={tagField.tag} setTag={tagField.setTag} status={tagField.status} compact />
                )}
                <div>
                  <span id="pw-accent" className={cn(LABEL, "mb-2")}>
                    Accent
                  </span>
                  <div role="radiogroup" aria-labelledby="pw-accent" className="flex flex-wrap gap-2">
                    {PROJECT_ACCENT_SWATCHES.map((swatch) => (
                      <button
                        key={swatch.hex}
                        type="button"
                        role="radio"
                        aria-checked={accent === swatch.hex}
                        tabIndex={accent === swatch.hex ? 0 : -1}
                        disabled={!!createdId}
                        onClick={() => setAccent(swatch.hex)}
                        onKeyDown={(e) =>
                          radioArrows(e, {
                            values: PROJECT_ACCENTS,
                            current: accent,
                            set: setAccent,
                            locked: !!createdId,
                          })
                        }
                        aria-label={swatch.name}
                        title={swatch.name}
                        className="h-7 w-7 rounded-[8px] transition-transform hover:scale-110"
                        style={{
                          background: swatch.hex,
                          // Selection is a box-shadow, not outline. An inline outline beats
                          // the global :focus-visible rule, so keyboard focus disappears.
                          // The 4px ring sits inside a 5px focus offset (design RING_ON).
                          // The 2px inner stop is the dialog surface (bg-surface-3).
                          boxShadow:
                            accent === swatch.hex
                              ? "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)"
                              : undefined,
                          outlineOffset: "5px",
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

            {!ready && step === "line" && (
              <>
                <div role="radiogroup" aria-label="Maintenance line" className="flex flex-col gap-2">
                  {LINE_CHOICES.map((choice) => {
                    const on = line === choice.value;
                    const key = choice.value ? `${T || "TAG"}-L1` : "";
                    return (
                      <div
                        key={choice.title}
                        className={cn(
                          "rounded-[10px] border",
                          on ? "border-accent/45 bg-accent/5" : "border-line bg-surface",
                        )}
                      >
                        <button
                          type="button"
                          role="radio"
                          aria-checked={on}
                          tabIndex={on ? 0 : -1}
                          onClick={() => setLine(choice.value)}
                          onKeyDown={(e) => radioArrows(e, { values: LINE_VALUES, current: line, set: setLine })}
                          className="grid w-full grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-[3px] px-3.5 py-3 text-left text-fg"
                        >
                          <RadioDot on={on} />
                          <span className="text-[13.5px] font-semibold">{choice.title}</span>
                          <span className={cn("font-mono text-small", on && key ? "text-accent" : "text-faint")}>{key}</span>
                          <span className="col-start-2 col-span-2 text-[12px] leading-snug text-muted">{choice.desc}</span>
                        </button>
                      </div>
                    );
                  })}
                </div>
                {line ? (
                  <div
                    data-testid="pw-line-how"
                    className="pw-anim flex flex-col gap-2 rounded-[10px] border border-line-2 bg-surface-2 p-3"
                    style={still ? undefined : { animation: "pwReveal .24s ease-out" }}
                  >
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-[10px] tracking-wide text-faint">HOW THE LINE WORKS</span>
                      <span className="flex-1" />
                      <span className="font-mono text-small text-accent">{`${T || "TAG"}-L1 · Maintenance`}</span>
                    </div>
                    {lineHow(T || "TAG").map((text, i) => (
                      <p key={text} className="grid grid-cols-[18px_minmax(0,1fr)] gap-2 text-body leading-relaxed text-fg-2">
                        <span className="font-mono text-[10.5px] text-faint">{i + 1}</span>
                        <span>{text}</span>
                      </p>
                    ))}
                  </div>
                ) : (
                  <p className="text-[12px] leading-relaxed text-faint">
                    Fixes with no PRD stay unscoped, and no drain picks them up. You can create the line later from the tracker’s Maintenance filter.
                  </p>
                )}
              </>
            )}

            {!ready && step === "model" && (
              <div role="radiogroup" aria-label="Model" className="flex flex-col gap-2">
                {MODELS.map((m) => {
                  const on = model === m.id;
                  return (
                    <div
                      key={m.id}
                      data-testid={`pw-model-${m.id}`}
                      className={cn("flex flex-col rounded-[10px] border", on ? "border-accent/45 bg-accent/5" : "border-line bg-surface")}
                    >
                      <button
                        type="button"
                        role="radio"
                        aria-checked={on}
                        tabIndex={on ? 0 : -1}
                        onClick={() => setModel(m.id)}
                        onKeyDown={(e) =>
                          radioArrows(e, {
                            values: MODEL_IDS,
                            current: model,
                            set: setModel,
                          })
                        }
                        className="grid grid-cols-[16px_minmax(0,1fr)] items-center gap-x-3 gap-y-[3px] px-3.5 py-3 text-left text-fg"
                      >
                        <RadioDot on={on} />
                        <span className="flex items-baseline gap-2">
                          <span className="text-[13.5px] font-semibold">{m.title}</span>
                          <span className="font-mono text-[10.5px] text-faint">{m.sub}</span>
                        </span>
                        <span />
                        <span className="text-[12px] leading-snug text-muted">{m.desc}</span>
                      </button>
                      {on && m.id === "ollama" && (
                        <div className="pw-anim flex flex-col gap-1.5 pb-3 pl-[42px] pr-3.5" style={still ? undefined : { animation: "pwReveal .24s ease-out" }}>
                          <label htmlFor="pw-ollama" className={cn(LABEL, "mb-0")}>
                            Ollama URL
                          </label>
                          <Input
                            id="pw-ollama"
                            value={ollamaUrl}
                            onChange={(e) => setOllamaUrl(e.target.value)}
                            placeholder="http://localhost:11434"
                            spellCheck={false}
                            className="h-[34px] bg-bg font-mono text-[12px]"
                          />
                          <p className="text-[11.5px] text-faint">Under compose this is host.docker.internal. Natively it is localhost.</p>
                        </div>
                      )}
                      {on && m.id === "claude" && (
                        <div className="pw-anim flex flex-col gap-1.5 pb-3 pl-[42px] pr-3.5" style={still ? undefined : { animation: "pwReveal .24s ease-out" }}>
                          <label htmlFor="pw-claude" className={cn(LABEL, "mb-0")}>
                            Anthropic API key
                          </label>
                          <Input
                            id="pw-claude"
                            type="password"
                            autoComplete="off"
                            value={claudeKey}
                            onChange={(e) => setClaudeKey(e.target.value)}
                            placeholder="sk-ant-…"
                            spellCheck={false}
                            className="h-[34px] bg-bg font-mono text-[12px]"
                          />
                          <p className="text-[11.5px] text-faint">Stored encrypted. Never shown again after you save.</p>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {!ready && step === "review" && (
              <>
                <dl data-testid="pw-summary" className="m-0 flex flex-col overflow-hidden rounded-[12px] border border-line bg-surface">
                  {summary.map(([k, v, st, mono], i) => (
                    <div
                      key={k}
                      className={cn("grid grid-cols-[96px_minmax(0,1fr)_auto] items-baseline gap-3 px-3.5 py-[11px]", i > 0 && "border-t border-line-2")}
                    >
                      <dt className="font-mono text-[10px] tracking-wide text-faint">{k}</dt>
                      <dd className="m-0 flex min-w-0 items-center gap-2">
                        {k === "ACCENT" && <span className="h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ background: accent }} />}
                        <span className={cn("truncate text-body", mono && "font-mono", v === "None yet" || v === "Not now" ? "text-faint" : "text-fg")}>{v}</span>
                      </dd>
                      {createdId && st === "identity" ? (
                        <span className="text-[12px] text-faint">Created</span>
                      ) : (
                        <button
                          type="button"
                          aria-label={`Edit ${k.toLowerCase()}`}
                          disabled={busy}
                          onClick={() => go(st)}
                          className="text-[12px] text-accent hover:text-accent-hi disabled:opacity-50"
                        >
                          Edit
                        </button>
                      )}
                    </div>
                  ))}
                </dl>
                {error && (
                  <p role="alert" className="flex items-center gap-[7px] text-body text-danger">
                    <AlertIcon />
                    {error}
                  </p>
                )}
              </>
            )}

            {ready && (
              <>
                <div className="flex items-center gap-3 rounded-[12px] border border-line bg-surface px-4 py-3.5">
                  <span
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px] font-mono text-small font-semibold text-bg"
                    style={{ background: accent }}
                  >
                    {tagField.tag}
                  </span>
                  <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <p className="truncate text-lead font-semibold">{name}</p>
                    <p className="font-mono text-small text-muted">
                      {tagField.tag} · {repo.trim() ? repoSlug(repo) : "no repository"}
                      {line ? ` · line ${tagField.tag}-L1` : ""}
                    </p>
                  </div>
                  <span className="font-mono text-[12px] text-st-done">✓</span>
                </div>
                <div className="flex flex-col gap-2">
                  <p className="text-[13px] text-fg-2">Run this in your checkout:</p>
                  <div className="flex h-[38px] items-center gap-2.5 rounded-[8px] border border-line-2 bg-surface pl-3 pr-1.5 font-mono text-[12px]">
                    <span className="flex-1 text-fg-2">
                      <span className="text-faint">$</span> gban setup
                    </span>
                    <button
                      type="button"
                      onClick={copy}
                      className={cn(
                        "h-[26px] rounded-[6px] border border-control bg-surface-2 px-2 font-mono text-small hover:border-control",
                        copied ? "text-st-done" : "text-fg-2",
                      )}
                    >
                      {copied ? "Copied" : "Copy"}
                    </button>
                  </div>
                  <p className="text-[12px] text-faint">
                    It reads the git remote and connects this machine to {tagField.tag || "the project"}.
                  </p>
                </div>
              </>
            )}
          </div>

          <footer data-testid="pw-footer" className="flex items-center gap-2 border-t border-line-2 py-3 pl-3.5 pr-4">
            {!ready && (
              <Button type="button" variant="ghost" size="sm" className="h-[34px] text-[13px]" onClick={() => close(false)}>
                Cancel
              </Button>
            )}
            <span className="flex-1" />
            {!ready && step === "repo" && !repo.trim() && (
              <Button type="button" variant="ghost" size="sm" className="h-[34px] text-[13px]" onClick={() => setStep(STEPS[idx + 1])}>
                Skip
              </Button>
            )}
            {!ready && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-[34px] border-control bg-transparent text-[13px]"
                disabled={idx <= 0 || busy}
                onClick={() => go(STEPS[idx - 1])}
              >
                <span aria-hidden="true">←</span> Back
              </Button>
            )}
            {ready && (
              <Button type="button" variant="outline" size="sm" className="h-[34px] border-control bg-transparent text-[13px]" onClick={another}>
                Create another
              </Button>
            )}
            {ready ? (
              <Button type="button" size="sm" className="h-[34px] px-[15px] text-[13px]" onClick={openProject}>
                Open project
              </Button>
            ) : (
              <Button type="button" size="sm" className="h-[34px] px-[15px] text-[13px]" disabled={!okFor[step] || busy} onClick={next}>
                {step === "review" ? (busy ? "Creating…" : "Create project") : "Next →"}
              </Button>
            )}
          </footer>
        </div>
      </DialogContent>
    </Dialog>
  );
}
