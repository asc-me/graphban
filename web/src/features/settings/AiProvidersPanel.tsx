import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight } from "lucide-react";
import * as React from "react";

import { NoModelBanner } from "@/components/NoModelBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { usePlatform } from "@/lib/queries";
import type { AiProvider, ProviderConfigUpdate } from "@/lib/types";

type Draft = { api_key: string; base_url: string; chat_model: string; embed_model: string };

/**
 * AI providers as a list: pick the active chat provider from the majors (Anthropic native +
 * OpenAI/Gemini/xAI/Groq/DeepSeek/Mistral via the OpenAI-compatible adapter) or a local/
 * self-hosted Ollama. API keys are entered here and stored write-only. The chat provider
 * switches at runtime; embeddings stay a deploy-time choice.
 */
export function AiProvidersPanel() {
  const { activeId } = useProjectCtx();
  const { data: cfg } = usePlatform(activeId);
  const { data: reg } = useQuery({ queryKey: ["ai-providers"], queryFn: () => api.aiProviders() });
  const qc = useQueryClient();
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, Draft>>({});

  const providers = reg?.providers ?? [];
  const active = cfg?.active_chat_provider ?? "";
  const stored = cfg?.provider_config ?? {};

  function draftFor(p: AiProvider): Draft {
    if (drafts[p.id]) return drafts[p.id];
    const s = stored[p.id];
    return {
      api_key: "",
      base_url: s?.base_url || p.base_url,
      chat_model: s?.chat_model || p.chat_model,
      embed_model: s?.embed_model || p.embed_model,
    };
  }
  function setField(p: AiProvider, field: keyof Draft, val: string) {
    setDrafts((d) => ({ ...d, [p.id]: { ...draftFor(p), [field]: val } }));
  }

  const save = useMutation({
    mutationFn: (p: AiProvider) => {
      const d = draftFor(p);
      const providersBody: Record<string, ProviderConfigUpdate> | undefined =
        p.id === "stub"
          ? undefined
          : { [p.id]: { api_key: d.api_key, base_url: d.base_url, chat_model: d.chat_model, embed_model: d.embed_model } };
      return api.saveProviders(activeId, { active_chat_provider: p.id, providers: providersBody });
    },
    onSuccess: (_r, p) => {
      setDrafts((d) => ({ ...d, [p.id]: { ...draftFor(p), api_key: "" } })); // clear the key input post-save
      qc.invalidateQueries({ queryKey: ["platform"] });
    },
  });

  return (
    <div className="mb-6 max-w-2xl">
      <div className="text-[14px] font-semibold">AI providers</div>
      <p className="mb-1.5 mt-0.5 text-[12.5px] text-muted">
        Choose the provider that drives agent chat &amp; extraction. Switches take effect immediately.
      </p>
      <p className="mb-3 text-[11.5px] text-faint">
        Graphban is a technical product (PRDs, code maps, code-aware chat) — <span className="text-fg-2">coding-oriented
        models</span> give noticeably better output than general chat models. For local Ollama, a{" "}
        <span className="font-mono text-muted-2">qwen2.5-coder</span> model is a good default.
      </p>

      <NoModelBanner withSettingsLink={false} className="mb-3" />

      <div className="space-y-2">
        {providers.map((p) => {
          const isActive = active === p.id;
          const s = stored[p.id];
          const open = openId === p.id;
          const d = draftFor(p);
          const status = isActive ? "active" : s?.key_set || (p.kind !== "stub" && s) ? "configured" : "";
          return (
            <div
              key={p.id}
              className={cn(
                "rounded-[12px] border bg-surface-2 transition-colors",
                isActive ? "border-accent/50" : "border-line-2 hover:border-line-hover",
              )}
            >
              <button
                onClick={() => setOpenId(open ? null : p.id)}
                className="flex w-full items-center gap-3 px-3.5 py-3 text-left"
              >
                <ChevronRight size={14} className={cn("flex-none text-faint transition-transform", open && "rotate-90")} />
                <span className="text-[13px] font-medium text-fg">{p.label}</span>
                <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-faint">
                  {p.kind === "openai" ? "OpenAI-compat" : p.kind}
                </span>
                {p.embeds && (
                  <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9px] uppercase tracking-wide text-muted-2">
                    embeds
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wide">
                  {status === "active" && (
                    <span className="flex items-center gap-1 text-accent">
                      <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                      active
                    </span>
                  )}
                  {status === "configured" && <span className="text-muted">configured</span>}
                </span>
              </button>

              {open && (
                <div className="animate-fade space-y-3 border-t border-line px-3.5 py-3">
                  {p.kind === "stub" ? (
                    <p className="text-[12px] text-muted">Deterministic offline provider — no configuration needed.</p>
                  ) : (
                    <>
                      {(p.kind === "openai" || p.kind === "ollama") && (
                        <Field label={p.kind === "ollama" ? "Endpoint URL" : "Base URL"}>
                          <Input value={d.base_url} onChange={(e) => setField(p, "base_url", e.target.value)}
                            placeholder={p.base_url} className="font-mono text-[12px]" />
                        </Field>
                      )}
                      <Field label={p.kind === "ollama" ? "Auth token (optional — for a Caddy-guarded endpoint)" : "API key"}>
                        <Input type="password" value={d.api_key} onChange={(e) => setField(p, "api_key", e.target.value)}
                          placeholder={s?.key_set ? "•••••••• (leave blank to keep)" : p.kind === "ollama" ? "bearer token" : "sk-…"} />
                      </Field>
                      <Field label="Chat model">
                        <Input value={d.chat_model} onChange={(e) => setField(p, "chat_model", e.target.value)}
                          placeholder={p.chat_model} className="font-mono text-[12px]" />
                        {p.kind === "ollama" && (
                          <p className="mt-1 text-[10.5px] text-faint">
                            Recommended: a coder model like <span className="font-mono">qwen2.5-coder</span> (or{" "}
                            <span className="font-mono">qwen2.5-coder:7b</span> for lighter hardware).
                          </p>
                        )}
                      </Field>
                      {p.embeds && (
                        <Field label="Embedding model">
                          <Input value={d.embed_model} onChange={(e) => setField(p, "embed_model", e.target.value)}
                            placeholder={p.embed_model} className="font-mono text-[12px]" />
                          <p className="mt-1 text-[10.5px] text-faint">
                            Deploy-time: switching the embedding model changes the vector dimension — set{" "}
                            <span className="font-mono">EMBED_PROVIDER={p.id}</span> + the matching{" "}
                            <span className="font-mono">EMBED_DIM</span> and re-embed. This field is used when that's set.
                          </p>
                        </Field>
                      )}
                    </>
                  )}
                  <div className="flex items-center gap-2 pt-0.5">
                    <Button size="sm" disabled={save.isPending} onClick={() => save.mutate(p)}>
                      {save.isPending && save.variables?.id === p.id ? (
                        "Saving…"
                      ) : isActive ? (
                        <>
                          <Check size={13} /> Save
                        </>
                      ) : (
                        "Use this provider"
                      )}
                    </Button>
                    {isActive && <span className="text-[11px] text-faint">Currently active.</span>}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[11.5px] text-faint">Active provider for <span className="text-fg-2">{activeId ?? "this project"}</span>. Keys are stored write-only and never shown again.</p>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{label}</div>
      {children}
    </div>
  );
}
