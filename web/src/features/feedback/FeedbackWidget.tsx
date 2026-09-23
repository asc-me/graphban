import { Check, Loader2, Paperclip, X } from "lucide-react";
import * as React from "react";

import { TYPE_META } from "@/lib/meta";
import { publicApi } from "@/lib/publicApi";
import type { DuplicateHit, RequestType } from "@/lib/types";

import { readableOn, resolveTheme, THEME_TOKENS, type FeedbackConfig } from "./config";

declare global {
  interface Window {
    turnstile?: {
      render: (el: HTMLElement, opts: { sitekey: string; callback: (t: string) => void; theme?: string }) => string;
    };
  }
}

/** Post a message to the embedding page (auto-resize, submit events). */
function postToHost(msg: Record<string, unknown>) {
  if (typeof window !== "undefined" && window.parent !== window) {
    window.parent.postMessage({ __graphban: true, ...msg }, "*");
  }
}

/**
 * Self-contained, themeable feedback widget. Runs unauthenticated against the
 * public endpoints — used standalone (/embed/feedback), in the generator preview
 * (with `preview` to mock submit), and inside the launcher popover.
 */
export function FeedbackWidget({ config, preview = false }: { config: FeedbackConfig; preview?: boolean }) {
  const types = config.types.length ? config.types : (["feedback"] as RequestType[]);
  const [type, setType] = React.useState<RequestType>(types[0]);
  const [title, setTitle] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [dups, setDups] = React.useState<DuplicateHit[]>([]);
  const [submitting, setSubmitting] = React.useState(false);
  const [doneRef, setDoneRef] = React.useState<string | null>(null);
  const [atts, setAtts] = React.useState<{ id: string; url: string; preview: string }[]>([]);
  const [uploading, setUploading] = React.useState(false);
  const [hp, setHp] = React.useState(""); // honeypot — real users never fill this
  const [tsToken, setTsToken] = React.useState("");
  const tsRef = React.useRef<HTMLDivElement>(null);
  const needsCaptcha = !!config.turnstileSitekey && !preview;

  async function addFiles(files: FileList | File[]) {
    const imgs = Array.from(files).filter((f) => f.type.startsWith("image/")).slice(0, 4 - atts.length);
    if (!imgs.length) return;
    setUploading(true);
    try {
      for (const f of imgs) {
        const res = await publicApi.uploadAttachment(f);
        if (res) setAtts((a) => [...a, { id: res.id, url: res.url, preview: URL.createObjectURL(f) }]);
      }
    } finally {
      setUploading(false);
    }
  }

  // Render Turnstile when a sitekey is configured (loads the script once).
  React.useEffect(() => {
    if (!needsCaptcha || !tsRef.current) return;
    const SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";
    const render = () => {
      if (window.turnstile && tsRef.current && !tsRef.current.hasChildNodes()) {
        window.turnstile.render(tsRef.current, {
          sitekey: config.turnstileSitekey,
          callback: setTsToken,
          theme: resolveTheme(config.theme),
        });
      }
    };
    if (window.turnstile) render();
    else if (!document.querySelector(`script[src="${SRC}"]`)) {
      const s = document.createElement("script");
      s.src = SRC;
      s.async = true;
      s.onload = render;
      document.head.appendChild(s);
    } else {
      const t = setInterval(() => window.turnstile && (clearInterval(t), render()), 200);
      return () => clearInterval(t);
    }
  }, [needsCaptcha, config.turnstileSitekey, config.theme]);

  const sourceUrl = React.useMemo(() => {
    if (typeof window === "undefined") return "";
    return new URLSearchParams(window.location.search).get("ref") || document.referrer || "";
  }, []);

  // Keep the embedding iframe sized to content (auto-resize).
  React.useEffect(() => {
    postToHost({ type: "resize", height: document.documentElement.scrollHeight });
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() =>
      postToHost({ type: "resize", height: document.documentElement.scrollHeight }),
    );
    ro.observe(document.body);
    return () => ro.disconnect();
  }, []);

  // Live duplicate detection, debounced on the title. (Read-only; safe in preview.)
  React.useEffect(() => {
    if (title.trim().length < 4) {
      setDups([]);
      return;
    }
    const t = setTimeout(async () => {
      setDups(await publicApi.duplicates(title.trim(), config.projectId));
    }, 350);
    return () => clearTimeout(t);
  }, [title, config.projectId]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || submitting) return;
    if (preview) {
      setDoneRef("R-preview");
      return;
    }
    setSubmitting(true);
    try {
      const res = await publicApi.submit(
        {
          type,
          title: title.trim(),
          detail: detail.trim(),
          email: email.trim(),
          project_id: config.projectId,
          source_url: sourceUrl,
          attachment_ids: atts.map((a) => a.id),
          turnstile_token: tsToken,
          hp,
        },
        config.ingestToken || undefined,
      );
      setDoneRef(res.request.id);
      postToHost({ type: "submitted", id: res.request.id });
    } catch {
      setDoneRef("error");
    } finally {
      setSubmitting(false);
    }
  }

  const t = THEME_TOKENS[resolveTheme(config.theme)];
  const style = {
    "--fb-accent": config.accent,
    "--fb-radius": `${config.radius}px`,
    color: t.text,
  } as React.CSSProperties;
  const card: React.CSSProperties = {
    background: t.surface,
    border: `1px solid ${t.border}`,
    borderRadius: "var(--fb-radius)",
    color: t.text,
  };
  const field: React.CSSProperties = {
    background: t.inputBg,
    border: `1px solid ${t.border}`,
    borderRadius: "var(--fb-radius)",
    color: t.text,
  };

  if (doneRef && doneRef !== "error") {
    return (
      <div style={style} className="w-full max-w-md">
        <div className="flex flex-col items-center gap-3 p-8 text-center" style={card}>
          <span
            className="flex h-11 w-11 items-center justify-center rounded-full"
            style={{ background: config.accent, color: readableOn(config.accent) }}
          >
            <Check size={22} />
          </span>
          <div className="text-[15px] font-semibold">{config.successText}</div>
          <div className="font-mono text-[12px]" style={{ color: t.muted }}>Tracked as {doneRef}</div>
        </div>
      </div>
    );
  }

  return (
    <div style={style} className="w-full max-w-md">
      <form
        onSubmit={submit}
        onPaste={(e) => {
          if (!config.attachments) return;
          const imgs = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
          if (imgs.length) addFiles(imgs);
        }}
        className="flex flex-col gap-3 p-5"
        style={card}
      >
        <div>
          <h2 className="text-[15px] font-semibold">{config.title}</h2>
          <p className="text-[12px]" style={{ color: t.muted }}>{config.subtitle}</p>
        </div>

        {/* Honeypot: hidden from real users; bots that fill it are rejected. */}
        <input
          type="text"
          tabIndex={-1}
          autoComplete="off"
          aria-hidden="true"
          value={hp}
          onChange={(e) => setHp(e.target.value)}
          style={{ position: "absolute", left: "-9999px", width: 1, height: 1, opacity: 0 }}
        />

        <div className="flex flex-wrap gap-1.5">
          {types.map((tp) => {
            const active = tp === type;
            const m = TYPE_META[tp];
            return (
              <button
                key={tp}
                type="button"
                onClick={() => setType(tp)}
                className="rounded-md border px-2 py-1 font-mono text-[10px] uppercase tracking-wide transition-colors"
                style={{
                  borderRadius: "var(--fb-radius)",
                  color: active ? m.color : t.muted,
                  background: active ? m.bg : "transparent",
                  borderColor: active ? m.border : t.border,
                }}
              >
                {m.label}
              </button>
            );
          })}
        </div>

        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Summary"
          className="h-9 px-3 text-[13px] outline-none"
          style={field}
        />

        {dups.length > 0 && (
          <div
            className="border p-2.5"
            style={{ borderRadius: "var(--fb-radius)", borderColor: "rgba(224,179,74,0.3)", background: "rgba(224,179,74,0.08)" }}
          >
            <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-[#e0b34a]">
              Possibly already reported
            </div>
            <div className="flex flex-col gap-1">
              {dups.slice(0, 3).map((d) => (
                <div key={`${d.kind}-${d.id}`} className="flex items-center gap-2 text-[12px]">
                  <span className="font-mono text-[10px]" style={{ color: t.faint }}>{d.id}</span>
                  <span className="min-w-0 flex-1 truncate">{d.title}</span>
                  <span className="font-mono text-[10px] text-[#e0b34a]">{Math.round(d.score * 100)}%</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <textarea
          value={detail}
          onChange={(e) => setDetail(e.target.value)}
          placeholder="Details (optional)"
          rows={3}
          className="resize-none px-3 py-2 text-[13px] outline-none"
          style={field}
        />

        {config.attachments && (
          <div className="flex flex-wrap items-center gap-2">
            {atts.map((a) => (
              <span key={a.id} className="relative">
                <img
                  src={a.preview}
                  alt="attachment"
                  className="h-11 w-11 rounded-md object-cover"
                  style={{ border: `1px solid ${t.border}` }}
                />
                <button
                  type="button"
                  onClick={() => setAtts((list) => list.filter((x) => x.id !== a.id))}
                  className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full"
                  style={{ background: t.surface, border: `1px solid ${t.border}`, color: t.muted }}
                  aria-label="Remove attachment"
                >
                  <X size={10} />
                </button>
              </span>
            ))}
            {atts.length < 4 && (
              <label
                className="flex h-11 cursor-pointer items-center gap-1.5 rounded-md px-3 text-[12px]"
                style={{ border: `1px dashed ${t.border}`, color: t.muted, borderRadius: "var(--fb-radius)" }}
              >
                {uploading ? <Loader2 size={13} className="animate-spin" /> : <Paperclip size={13} />}
                Screenshot
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  onChange={(e) => e.target.files && addFiles(e.target.files)}
                />
              </label>
            )}
          </div>
        )}

        {config.showEmail && (
          <input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Email (optional)"
            type="email"
            className="h-9 px-3 text-[13px] outline-none"
            style={field}
          />
        )}

        {needsCaptcha && <div ref={tsRef} className="min-h-[65px]" />}

        {doneRef === "error" && (
          <p className="text-[12px] text-st-blocked">Something went wrong. Please try again.</p>
        )}

        <button
          type="submit"
          disabled={!title.trim() || submitting || uploading || (needsCaptcha && !tsToken)}
          className="flex h-9 items-center justify-center gap-2 font-semibold transition-opacity disabled:opacity-50"
          style={{ background: config.accent, color: readableOn(config.accent), borderRadius: "var(--fb-radius)" }}
        >
          {submitting && <Loader2 size={14} className="animate-spin" />}
          {config.buttonLabel}
        </button>
      </form>
    </div>
  );
}
