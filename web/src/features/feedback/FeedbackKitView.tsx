import { Check, Copy } from "lucide-react";
import * as React from "react";

import { useProjectCtx } from "@/features/ProjectContext";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";
import { TYPE_META } from "@/lib/meta";
import { usePlatform } from "@/lib/queries";
import type { RequestType } from "@/lib/types";

import { FeedbackWidget } from "./FeedbackWidget";
import {
  ALL_TYPES,
  DEFAULT_CONFIG,
  toParams,
  type FeedbackConfig,
  type FeedbackMode,
  type FeedbackTheme,
  type LauncherPosition,
} from "./config";
import { inlineSnippet, launcherSnippet } from "./snippets";

const ACCENTS = ["#c6f24e", "#7ca2ff", "#a78bfa", "#5fd07a", "#ff8f8f", "#e0b34a"];
const RADII = [4, 8, 12, 20];
const THEMES: FeedbackTheme[] = ["dark", "light", "auto"];
const MODES: FeedbackMode[] = ["inline", "launcher"];

/** Feedback Kit — configure a themeable embeddable widget, preview it live,
 *  and copy the embed snippet (inline iframe or floating launcher). */
export function FeedbackKitView() {
  const { activeId } = useProjectCtx();
  const { data: platform } = usePlatform(activeId);
  const [cfg, setCfg] = React.useState<FeedbackConfig>(() => ({ ...DEFAULT_CONFIG, projectId: activeId }));
  const [copied, setCopied] = React.useState(false);
  const set = <K extends keyof FeedbackConfig>(k: K, v: FeedbackConfig[K]) =>
    setCfg((c) => ({ ...c, [k]: v }));

  React.useEffect(() => {
    setCfg((c) => (c.projectId ? c : { ...c, projectId: activeId }));
  }, [activeId]);

  // Spam protection is configured per-project in Settings; reflect the Turnstile sitekey
  // (if any) into the generated snippet.
  const sitekey = platform?.turnstile_sitekey ?? "";
  React.useEffect(() => {
    setCfg((c) => (c.turnstileSitekey === sitekey ? c : { ...c, turnstileSitekey: sitekey }));
  }, [sitekey]);

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const embedUrl = `${origin}/embed/feedback?${toParams(cfg)}`;
  const snippet = cfg.mode === "launcher" ? launcherSnippet(embedUrl, cfg) : inlineSnippet(embedUrl);

  function toggleType(t: RequestType) {
    setCfg((c) => ({
      ...c,
      types: c.types.includes(t) ? c.types.filter((x) => x !== t) : [...c.types, t],
    }));
  }

  async function copy() {
    if (!(await copyText(snippet))) return;
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Feedback Kit</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">
          A themeable, embeddable feedback widget with built-in duplicate detection. Configure, preview, and copy the snippet.
        </p>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-6 overflow-y-auto p-6 lg:grid-cols-[340px_1fr]">
        {/* Config panel */}
        <div className="flex flex-col gap-5">
          <Section label="Embed mode">
            <Segmented options={MODES} value={cfg.mode} onChange={(m) => set("mode", m)} />
            {cfg.mode === "launcher" && (
              <div className="mt-2.5 flex flex-col gap-2.5">
                <TextField label="Launcher label" value={cfg.launcherLabel} onChange={(v) => set("launcherLabel", v)} />
                <div>
                  <FieldLabel>Position</FieldLabel>
                  <Segmented
                    options={["bottom-right", "bottom-left"] as LauncherPosition[]}
                    value={cfg.position}
                    onChange={(p) => set("position", p)}
                  />
                </div>
              </div>
            )}
          </Section>

          <Section label="Theme">
            <Segmented options={THEMES} value={cfg.theme} onChange={(t) => set("theme", t)} />
          </Section>

          <Section label="Accent">
            <div className="flex flex-wrap items-center gap-2">
              {ACCENTS.map((a) => (
                <button
                  key={a}
                  onClick={() => set("accent", a)}
                  className={cn("h-7 w-7 rounded-full border-2", cfg.accent === a ? "border-fg" : "border-transparent")}
                  style={{ background: a }}
                  aria-label={a}
                />
              ))}
              <label
                className="flex h-7 items-center gap-2 rounded-md border border-line-2 bg-surface-2 px-2 font-mono text-[11px] text-muted"
                title="Custom color"
              >
                <input
                  type="color"
                  value={cfg.accent}
                  onChange={(e) => set("accent", e.target.value)}
                  className="h-4 w-4 cursor-pointer border-0 bg-transparent p-0"
                />
                {cfg.accent}
              </label>
            </div>
          </Section>

          <Section label="Corner radius">
            <div className="flex gap-1.5">
              {RADII.map((r) => (
                <button
                  key={r}
                  onClick={() => set("radius", r)}
                  className={cn(
                    "rounded-md border px-3 py-1.5 font-mono text-[11px] transition-colors",
                    cfg.radius === r ? "border-line-hover bg-surface-3 text-fg" : "border-line-2 bg-surface-2 text-muted hover:text-fg-2",
                  )}
                >
                  {r}px
                </button>
              ))}
            </div>
          </Section>

          <Section label="Enabled types">
            <div className="flex flex-wrap gap-1.5">
              {ALL_TYPES.map((t) => {
                const on = cfg.types.includes(t);
                return (
                  <button
                    key={t}
                    onClick={() => toggleType(t)}
                    className="rounded-md border px-2 py-1 font-mono text-[10px] uppercase tracking-wide transition-colors"
                    style={{
                      color: on ? TYPE_META[t].color : "#5c656e",
                      background: on ? TYPE_META[t].bg : "transparent",
                      borderColor: on ? TYPE_META[t].border : "#1e242a",
                    }}
                  >
                    {TYPE_META[t].label}
                  </button>
                );
              })}
            </div>
          </Section>

          <Section label="Copy">
            <div className="flex flex-col gap-2.5">
              <TextField label="Title" value={cfg.title} onChange={(v) => set("title", v)} />
              <TextField label="Subtitle" value={cfg.subtitle} onChange={(v) => set("subtitle", v)} />
              <TextField label="Button label" value={cfg.buttonLabel} onChange={(v) => set("buttonLabel", v)} />
              <TextField label="Thank-you message" value={cfg.successText} onChange={(v) => set("successText", v)} />
            </div>
          </Section>

          <Section label="Options">
            <div className="flex flex-col gap-2">
              <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-fg-2">
                <input type="checkbox" checked={cfg.showEmail} onChange={(e) => set("showEmail", e.target.checked)} />
                Collect email
              </label>
              <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-fg-2">
                <input type="checkbox" checked={cfg.attachments} onChange={(e) => set("attachments", e.target.checked)} />
                Allow screenshot attachments
              </label>
            </div>
            <p className="mt-2 text-[11px] text-faint">
              {cfg.turnstileSitekey ? "Cloudflare Turnstile is on for this project. " : ""}
              Spam protection (rate limit, captcha) is configured in{" "}
              <span className="text-muted">Settings → Integrations</span>.
            </p>
          </Section>

          <Section label="Embed snippet">
            <div className="relative">
              <pre className="max-h-64 overflow-auto rounded-lg border border-line-2 bg-surface-2 p-3 pr-10 font-mono text-[11px] leading-relaxed text-muted-2">
                {snippet}
              </pre>
              <button
                onClick={copy}
                className="absolute right-2 top-2 rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
                title="Copy"
              >
                {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
              </button>
            </div>
            {cfg.mode === "launcher" && (
              <p className="mt-2 text-[11px] text-faint">
                Paste before <code className="font-mono">&lt;/body&gt;</code>. Adds a floating “{cfg.launcherLabel}” button.
              </p>
            )}
          </Section>
        </div>

        {/* Live preview */}
        <div className="flex flex-col">
          <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-wide text-faint">
            Live preview
            <span className="rounded border border-line-2 px-1.5 py-px text-faint-2 normal-case">
              test mode — submissions aren’t saved
            </span>
          </div>
          <div
            className={cn(
              "flex flex-1 items-start justify-center rounded-[14px] border border-dashed border-line-2 p-8",
              // Show the widget against a surface that matches its theme so light mode is legible.
              cfg.theme === "light" ? "bg-white" : cfg.theme === "dark" ? "bg-[#0a0c0e]" : "bg-surface/40",
            )}
          >
            <FeedbackWidget key={JSON.stringify(cfg)} config={cfg} preview />
          </div>
        </div>
      </div>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">{label}</div>
      {children}
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <div className="mb-1 font-mono text-[9.5px] uppercase tracking-wide text-faint-2">{children}</div>;
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block">
      <FieldLabel>{label}</FieldLabel>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-8 w-full rounded-md border border-line-2 bg-surface-2 px-2.5 text-[12.5px] text-fg-2 outline-none focus:border-line-hover"
      />
    </label>
  );
}

function Segmented<T extends string>({ options, value, onChange }: { options: T[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-line-2 bg-surface-2 p-0.5">
      {options.map((o) => (
        <button
          key={o}
          onClick={() => onChange(o)}
          className={cn(
            "rounded-md px-3 py-1 text-[12px] capitalize transition-colors",
            value === o ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
          )}
        >
          {o.replace("-", " ")}
        </button>
      ))}
    </div>
  );
}
