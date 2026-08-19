import { ArrowUp, Brain, Plus, Search, Sparkles, X } from "lucide-react";
import * as React from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useAddShard, useShards } from "@/lib/queries";
import type { ShardHit } from "@/lib/types";

export function AgentSidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <aside className="flex w-[360px] flex-none flex-col border-l border-line bg-surface/50">
      <div className="flex h-12 flex-none items-center justify-between border-b border-line px-4">
        <div className="flex items-center gap-2 text-[13px] font-semibold">
          <Brain size={15} className="text-purple" />
          Agent context
        </div>
        <button onClick={onClose} className="text-faint hover:text-fg">
          <X size={15} />
        </button>
      </div>

      <Tabs defaultValue="memory" className="flex min-h-0 flex-1 flex-col">
        <div className="px-4 pt-3">
          <TabsList className="w-full">
            <TabsTrigger value="memory" className="flex-1">
              Memory
            </TabsTrigger>
            <TabsTrigger value="agent" className="flex-1">
              Chat
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="memory" className="min-h-0 flex-1 focus:outline-none">
          <MemoryPanel />
        </TabsContent>
        <TabsContent value="agent" className="min-h-0 flex-1 focus:outline-none">
          <AgentChat />
        </TabsContent>
      </Tabs>
    </aside>
  );
}

function MemoryPanel() {
  const { activeId } = useProjectCtx();
  const { data: shards = [] } = useShards(activeId);
  const addShard = useAddShard(activeId);
  const [query, setQuery] = React.useState("");
  const [hits, setHits] = React.useState<ShardHit[] | null>(null);
  const [searching, setSearching] = React.useState(false);
  const [draft, setDraft] = React.useState("");

  async function runSearch(e: React.FormEvent) {
    e.preventDefault();
    // No project resolved means no scope to search. Sending an empty one would return
    // zero hits and render as "no matches" — an absence reading as a clean result, when
    // the truth is that nothing was searched at all.
    if (!activeId) return;
    if (!query.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      setHits(await api.searchMemory(activeId, query, 5));
    } finally {
      setSearching(false);
    }
  }

  async function addMemory(e: React.FormEvent) {
    e.preventDefault();
    if (!activeId || !draft.trim()) return;
    await addShard.mutateAsync({ text: draft.trim(), scope: "global" });
    setDraft("");
  }

  const list = hits
    ? hits.map((h) => ({ ...h.shard, score: h.score }))
    : shards.map((s) => ({ ...s, score: undefined as number | undefined }));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <form onSubmit={runSearch} className="relative flex-none px-4 py-3">
        <Search size={13} className="absolute left-7 top-1/2 -translate-y-1/2 text-muted opacity-60" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          disabled={!activeId}
          placeholder={
            activeId ? "Semantic search over memory…" : "No project selected — nothing to search"
          }
          className="h-9 w-full rounded-[9px] border border-line-2 bg-surface-2 pl-8 pr-3 text-[12.5px] outline-none focus:border-line-hover disabled:cursor-not-allowed disabled:text-faint"
        />
      </form>

      <div className="flex items-center justify-between px-4 pb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
        <span>{hits ? `${hits.length} matches` : `${shards.length} shards`}</span>
        {hits && (
          <button className="text-faint hover:text-fg" onClick={() => { setHits(null); setQuery(""); }}>
            clear
          </button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-3">
        {searching && <div className="py-6 text-center text-[12px] text-muted">Searching…</div>}
        <div className="flex flex-col gap-2">
          {list.map((s) => (
            <div
              key={s.id}
              className="rounded-[11px] border border-line-2 bg-surface-2 p-3 transition-colors hover:border-line-hover"
            >
              <div className="mb-1.5 flex items-center gap-2">
                <span
                  className={cn(
                    "rounded border px-1.5 py-px font-mono text-[9px] uppercase tracking-wide",
                    s.scope === "global"
                      ? "border-[rgba(167,139,250,0.3)] text-purple-2"
                      : "border-line-2 text-muted",
                  )}
                >
                  {s.scope}
                </span>
                <span className="font-mono text-[10px] text-faint">{s.source}</span>
                {s.score != null && (
                  <span className="ml-auto font-mono text-[10px] text-accent">{s.score.toFixed(2)}</span>
                )}
              </div>
              <p className="text-[12.5px] leading-relaxed text-fg-2">{s.text}</p>
            </div>
          ))}
        </div>
      </div>

      <form onSubmit={addMemory} className="flex-none border-t border-line p-3">
        <div className="flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface-2 px-2.5 focus-within:border-line-hover">
          <Plus size={14} className="text-muted" />
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Add a memory shard…"
            className="h-9 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-faint"
          />
          <button
            type="submit"
            disabled={!draft.trim() || addShard.isPending}
            className="rounded-md p-1 text-accent disabled:opacity-40"
          >
            <ArrowUp size={15} />
          </button>
        </div>
      </form>
    </div>
  );
}

function AgentChat() {
  const [messages, setMessages] = React.useState<{ role: "agent" | "user"; text: string }[]>([
    {
      role: "agent",
      text: "I've loaded your project state and memory shards. Ask me to summarize progress, recall a past decision, or suggest what to pick up next.",
    },
  ]);
  const [input, setInput] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const scrollRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    // Append the user turn plus an empty agent turn that streamed tokens fill in.
    setMessages((m) => [...m, { role: "user", text }, { role: "agent", text: "" }]);
    setInput("");
    setBusy(true);
    try {
      await api.chatStream(text, {
        onDelta: (delta) =>
          setMessages((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            copy[copy.length - 1] = { ...last, text: last.text + delta };
            return copy;
          }),
      });
    } catch {
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "agent", text: "Something went wrong reaching the backend." };
        return copy;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {messages.map((m, i) => (
          <div key={i} className={cn("flex gap-2.5", m.role === "user" && "flex-row-reverse")}>
            {m.role === "agent" && (
              <span className="mt-0.5 flex h-6 w-6 flex-none items-center justify-center rounded-full bg-[rgba(167,139,250,0.12)]">
                <Sparkles size={12} className="text-purple" />
              </span>
            )}
            <div
              className={cn(
                "max-w-[85%] whitespace-pre-wrap rounded-[12px] px-3 py-2 text-[12.5px] leading-relaxed",
                m.role === "agent"
                  ? "border border-line-2 bg-surface-2 text-fg-2"
                  : "bg-accent/90 text-bg",
              )}
            >
              {m.text}
            </div>
          </div>
        ))}
        {busy && <div className="pl-9 font-mono text-[11px] text-faint">thinking…</div>}
      </div>
      <form onSubmit={send} className="flex-none border-t border-line p-3">
        <div className="flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface-2 px-2.5 focus-within:border-line-hover">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask the agent…"
            className="h-9 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-faint"
          />
          <button type="submit" disabled={busy} className="rounded-md p-1 text-accent disabled:opacity-40">
            <ArrowUp size={15} />
          </button>
        </div>
      </form>
    </div>
  );
}
