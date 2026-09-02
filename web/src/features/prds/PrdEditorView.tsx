import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronDown, Copy, Eye, FlaskConical, History, Link2, ListChecks, MessageCircleQuestion, Save, ShieldQuestion, Sparkles } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";

import { NoModelBanner } from "@/components/NoModelBanner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { lineDiff } from "@/lib/diff";
import { Markdown } from "@/lib/markdown";
import { publicApi } from "@/lib/publicApi";
import { keys, useGrillState, useIntentDiff, useItems, usePrd, usePrdVersions, useUpdateItem } from "@/lib/queries";
import type { Item, PrdStatus, PrdVersion, PrototypeVerdictOut } from "@/lib/types";

import { AssistantPanel } from "@/features/assistant/AssistantPanel";
import { GrillPanel } from "./GrillPanel";
import { PRD_SETTABLE_STATUSES, PRD_STATUS_META, prdStatusMeta } from "./meta";
import { ApprovalEval } from "./ApprovalEval";
import { ApprovedIsEarned, GrillProgress } from "./GrillProgress";
import { AcceptancePanel } from "./AcceptancePanel";
import { IntentDiff } from "./IntentDiff";

const AI_COMMANDS = [
  { key: "expand", label: "Expand" },
  { key: "risks", label: "Generate risks" },
  { key: "summarize", label: "Summarize" },
];

export function PrdEditorView() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data: prd } = usePrd(id);
  const { data: versions = [] } = usePrdVersions(id);
  const { data: grill } = useGrillState(id);
  const { data: diff } = useIntentDiff(id);

  const [title, setTitle] = React.useState("");
  const [body, setBody] = React.useState("");
  const [rightTab, setRightTab] = React.useState<"preview" | "history" | "coverage" | "grill" | "assistant" | "acceptance">("preview");
  const [diffVersion, setDiffVersion] = React.useState<PrdVersion | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [aiBusy, setAiBusy] = React.useState<string | null>(null);

  // Load draft when the PRD arrives / changes id.
  React.useEffect(() => {
    if (prd) {
      setTitle(prd.title);
      setBody(prd.body);
    }
  }, [prd?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!prd) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  const dirty = title !== prd.title || body !== prd.body;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: keys.prd(id) });
    qc.invalidateQueries({ queryKey: keys.prds });
  };

  async function save() {
    setSaving(true);
    try {
      await api.updatePrd(id, { title, body });
      refresh();
    } finally {
      setSaving(false);
    }
  }

  async function setStatus(status: PrdStatus) {
    await api.updatePrd(id, { status });
    refresh();
  }

  async function snapshot() {
    const note = window.prompt("Version note", "Version snapshot.");
    if (note === null) return;
    if (dirty) await api.updatePrd(id, { title, body });
    await api.snapshotPrd(id, note);
    refresh();
    qc.invalidateQueries({ queryKey: keys.prdVersions(id) });
  }

  async function runAi(command: string) {
    setAiBusy(command);
    try {
      const { text } = await api.prdAi(id, command);
      setBody((b) => `${b.trimEnd()}\n\n${text.trim()}\n`);
      setRightTab("preview");
    } finally {
      setAiBusy(null);
    }
  }


  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-3">
        <button onClick={() => navigate("/prds")} className="text-faint hover:text-fg">
          <ArrowLeft size={16} />
        </button>
        <span className="font-mono text-[11px] text-faint">{prd.id}</span>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="min-w-0 flex-1 bg-transparent text-[15px] font-semibold outline-none"
        />
        <StatusMenu status={prd.status} onChange={setStatus} complete={!!grill?.complete} />
        <span className="rounded-md bg-surface-4 px-2 py-1 font-mono text-[10px] text-muted-2">{prd.version}</span>
        <Button variant="outline" size="sm" onClick={snapshot}>
          <History size={13} />
          Snapshot
        </Button>
        <Button size="sm" onClick={save} disabled={!dirty || saving}>
          <Save size={13} />
          {saving ? "Saving…" : dirty ? "Save" : "Saved"}
        </Button>
      </div>

      {/* Toolbar */}
      <div className="flex flex-none items-center gap-2 border-b border-line px-5 py-2">
        <span className="mr-1 font-mono text-[10px] uppercase tracking-wide text-faint">AI</span>
        {AI_COMMANDS.map((c) => (
          <button
            key={c.key}
            onClick={() => runAi(c.key)}
            disabled={!!aiBusy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[#2a2440] bg-[rgba(167,139,250,0.08)] px-2.5 py-1 text-[11.5px] text-purple-2 transition-colors hover:border-[#3a3358] disabled:opacity-50"
          >
            <Sparkles size={12} />
            {aiBusy === c.key ? "…" : c.label}
          </button>
        ))}
        <button
          onClick={() => setRightTab("grill")}
          title="Interactively grill this PRD — the agent asks clarifying questions to sharpen it before building"
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(198,242,78,0.08)] px-2.5 py-1 text-[11.5px] text-accent transition-colors hover:border-[#2a3320]"
        >
          <MessageCircleQuestion size={12} />
          Grill
        </button>
        <div className="ml-auto flex items-center gap-2">
          <LinkItemsMenu prdId={id} linked={prd.linked} onChange={refresh} />
          <div className="flex items-center gap-1 rounded-lg border border-line-2 bg-surface-2 p-0.5">
            <TabBtn active={rightTab === "preview"} onClick={() => setRightTab("preview")} icon={<Eye size={12} />} label="Preview" />
            <TabBtn active={rightTab === "assistant"} onClick={() => setRightTab("assistant")} icon={<Sparkles size={12} />} label="Assistant" />
            <TabBtn active={rightTab === "grill"} onClick={() => setRightTab("grill")} icon={<MessageCircleQuestion size={12} />} label="Grill" />
            <TabBtn active={rightTab === "coverage"} onClick={() => setRightTab("coverage")} icon={<ListChecks size={12} />} label="Coverage" />
            <TabBtn active={rightTab === "acceptance"} onClick={() => setRightTab("acceptance")} icon={<ShieldQuestion size={12} />} label="Acceptance" />
            <TabBtn active={rightTab === "history"} onClick={() => setRightTab("history")} icon={<History size={12} />} label="History" />
          </div>
        </div>
      </div>

      <NoModelBanner className="mx-5 mt-2 flex-none" />

      {/* Body: editor | right pane */}
      <div className="grid min-h-0 flex-1 grid-cols-2">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          spellCheck={false}
          className="min-h-0 resize-none border-r border-line bg-surface/40 p-5 font-mono text-[12.5px] leading-relaxed text-fg-2 outline-none"
        />
        <div className="min-h-0 overflow-y-auto p-5">
          {rightTab === "preview" ? (
            <Markdown source={body} />
          ) : rightTab === "assistant" ? (
            <AssistantPanel entityType="prd" entityId={id} projectId={prd.project_id} />
          ) : rightTab === "grill" ? (
            <div className="flex min-h-0 flex-1 flex-col gap-3">
              {diff && <IntentDiff diff={diff} />}
              {grill && <GrillProgress state={grill} />}
              <ApprovalEval prdId={id} grillComplete={!!grill?.complete} />
              <div className="min-h-0 flex-1">
                <GrillPanel prdId={id} onApply={(b) => { setBody(b); setRightTab("preview"); }} />
              </div>
            </div>
          ) : rightTab === "coverage" ? (
            <CoveragePanel prdId={id} projectId={prd.project_id} onDecomposed={refresh} />
          ) : rightTab === "acceptance" ? (
            <AcceptancePanel prdId={id} />
          ) : (
            <VersionHistory
              versions={versions}
              currentVersion={prd.version}
              diffVersion={diffVersion}
              onSelect={setDiffVersion}
              draftBody={body}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function TabBtn({ active, onClick, icon, label }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11.5px] transition-colors",
        active ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function StatusMenu({
  status,
  onChange,
  complete,
}: {
  status: PrdStatus;
  onChange: (s: PrdStatus) => void;
  complete: boolean;
}) {
  const meta = prdStatusMeta(status);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="inline-flex items-center gap-1.5 rounded-md border border-line-2 px-2 py-1 hover:border-line-hover">
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.color }} />
          <span className="font-mono text-[10.5px] uppercase tracking-wide" style={{ color: meta.color }}>
            {meta.label}
          </span>
          <ChevronDown size={11} className="text-faint" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {/* `approved` is REACHED, not picked (PRD-15). Offering it would be a control
            whose every use the server refuses. */}
        {PRD_SETTABLE_STATUSES.map((s) => (
          <DropdownMenuItem key={s} onSelect={() => onChange(s)}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: PRD_STATUS_META[s].color }} />
            <span className="font-mono text-[11px] uppercase tracking-wide" style={{ color: PRD_STATUS_META[s].color }}>
              {PRD_STATUS_META[s].label}
            </span>
          </DropdownMenuItem>
        ))}
        <ApprovedIsEarned complete={complete} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function LinkItemsMenu({ prdId, linked, onChange }: { prdId: string; linked: string[]; onChange: () => void }) {
  const { data: items = [] } = useItems();
  async function toggle(itemId: string, add: boolean) {
    await api.linkPrd(prdId, itemId, add);
    onChange();
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="inline-flex items-center gap-1.5 rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted hover:text-fg">
          <Link2 size={12} />
          Linked · {linked.length}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-[320px] w-[300px] overflow-y-auto">
        <DropdownMenuLabel>Link tracker items</DropdownMenuLabel>
        {items.map((it) => {
          const on = linked.includes(it.id);
          return (
            <DropdownMenuItem
              key={it.id}
              onSelect={(e) => {
                e.preventDefault();
                toggle(it.id, !on);
              }}
            >
              <span className={cn("h-3 w-3 flex-none rounded border", on ? "border-accent bg-accent" : "border-line-hover")} />
              <span className="w-[46px] flex-none font-mono text-[10px] text-faint">{it.id}</span>
              <span className="min-w-0 flex-1 truncate">{it.title}</span>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function VersionHistory({
  versions,
  currentVersion,
  diffVersion,
  onSelect,
  draftBody,
}: {
  versions: PrdVersion[];
  currentVersion: string;
  diffVersion: PrdVersion | null;
  onSelect: (v: PrdVersion | null) => void;
  draftBody: string;
}) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Version history</div>
      <div className="space-y-1.5">
        {versions.map((v) => (
          <button
            key={v.id}
            onClick={() => onSelect(diffVersion?.id === v.id ? null : v)}
            className={cn(
              "flex w-full items-start gap-2.5 rounded-[10px] border p-2.5 text-left transition-colors",
              diffVersion?.id === v.id ? "border-line-hover bg-surface-3" : "border-line-2 bg-surface-2 hover:border-line-hover",
            )}
          >
            <span className="rounded bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted-2">{v.version}</span>
            <div className="min-w-0 flex-1">
              <div className="text-[12px] text-fg-2">{v.note}</div>
              <div className="font-mono text-[10px] text-faint">{v.date}</div>
            </div>
            {v.version === currentVersion && (
              <span className="font-mono text-[9px] uppercase text-accent">current</span>
            )}
          </button>
        ))}
      </div>

      {diffVersion && diffVersion.body && (
        <div className="mt-4">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
            Diff · {diffVersion.version} → draft
          </div>
          <div className="overflow-x-auto rounded-lg border border-line-2 bg-surface-2 p-2 font-mono text-[11px] leading-relaxed">
            {lineDiff(diffVersion.body, draftBody).map((op, i) => (
              <div
                key={i}
                className={cn(
                  "whitespace-pre-wrap px-1",
                  op.type === "add" && "bg-[rgba(95,208,122,0.12)] text-st-done",
                  op.type === "del" && "bg-[rgba(255,107,107,0.1)] text-st-blocked line-through",
                  op.type === "same" && "text-muted",
                )}
              >
                {op.type === "add" ? "+ " : op.type === "del" ? "- " : "  "}
                {op.text || " "}
              </div>
            ))}
          </div>
        </div>
      )}
      {diffVersion && !diffVersion.body && (
        <p className="mt-3 text-[12px] text-faint">This historical version has no stored body snapshot.</p>
      )}
    </div>
  );
}

export function CoveragePanel({ prdId, projectId, onDecomposed }: { prdId: string; projectId: string; onDecomposed: () => void }) {
  const qc = useQueryClient();
  const [busy, setBusy] = React.useState(false);
  const [protoOpen, setProtoOpen] = React.useState<string | null>(null);
  const { data: cov } = useQuery({
    queryKey: ["prd-coverage", prdId],
    queryFn: () => api.prdCoverage(prdId),
  });
  // The nudge names a count; the handoff acts on items. Coverage carries per-section
  // `item_ids`, and the tracker's list is already cached — no new endpoint needed to
  // know WHICH items in this section are still prototype-first.
  const { data: items = [] } = useItems(projectId);

  async function fillGaps() {
    setBusy(true);
    try {
      await api.decomposePrd(prdId, true);
      await qc.invalidateQueries({ queryKey: ["prd-coverage", prdId] });
      qc.invalidateQueries({ queryKey: keys.items });
      onDecomposed();
    } finally {
      setBusy(false);
    }
  }

  if (!cov) return <p className="text-[13px] text-muted">Loading coverage…</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[13px] text-fg-2">
            {cov.sections_with_tasks}/{cov.section_count} sections covered · {cov.percent_done}% done
          </div>
          <div className="mt-1 h-1.5 w-48 overflow-hidden rounded-full bg-surface-4">
            <div className="h-full rounded-full bg-accent" style={{ width: `${cov.percent_done}%` }} />
          </div>
          {!cov.shaped && (
            <div className="mt-1.5 font-mono text-[10.5px] text-faint">
              No sections yet — not a clean pass
            </div>
          )}
          {cov.shaped && cov.empty_sections.length > 0 && (
            <div className="mt-1.5 font-mono text-[10.5px] text-[#e0b34a]">
              {cov.empty_sections.length} empty — not a task gap
            </div>
          )}
          {cov.open_high_fidelity > 0 && (
            <div className="mt-1.5 font-mono text-[10.5px] text-[#e0b34a]">
              {cov.open_high_fidelity} open · needs a prototype (grill → prototype → grill)
            </div>
          )}
        </div>
        {cov.gaps.length > 0 && cov.status === "approved" && (
          <Button size="sm" onClick={fillGaps} disabled={busy}>
            <ListChecks size={13} />
            {busy ? "Creating…" : `Fill ${cov.gaps.length} gap${cov.gaps.length > 1 ? "s" : ""}`}
          </Button>
        )}
        {cov.gaps.length > 0 && cov.status !== "approved" && (
          <div className="max-w-[16rem] text-right font-mono text-[10.5px] text-faint">
            Fill gaps after the grill earns approved — a {cov.status} is not a task list
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        {cov.sections.map((s) => {
          const high = items.filter(
            (it) => s.item_ids.includes(it.id) && it.fidelity === "high" && it.status !== "done",
          );
          const open = protoOpen === s.section;
          return (
            <React.Fragment key={s.section}>
              <div
                className="flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface-2 px-3 py-2"
              >
                <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{s.section}</span>
                {s.open_high_fidelity > 0 && (
                  <button
                    onClick={() => setProtoOpen(open ? null : s.section)}
                    className={cn(
                      "rounded border px-1.5 py-px font-mono text-[9px] uppercase tracking-wide transition-colors",
                      open
                        ? "border-[#e0b34a] text-[#f0c95f]"
                        : "border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] text-[#e0b34a] hover:border-[#5a4a2a]",
                    )}
                    title="High-fidelity work here needs a prototype — start the handoff"
                  >
                    {s.open_high_fidelity} proto
                  </button>
                )}
                {s.empty && (
                  <span className="rounded border border-[rgba(224,179,74,0.3)] px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-[#e0b34a]">
                    empty
                  </span>
                )}
                {s.gap ? (
                  <span className="rounded border border-[rgba(224,179,74,0.3)] px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-[#e0b34a]">
                    no tasks
                  </span>
                ) : (
                  <span className="font-mono text-[10.5px] text-muted">
                    {s.done}/{s.item_count} done
                  </span>
                )}
              </div>
              {open && (
                <div className="ml-4 space-y-2 border-l border-[#3a2f1a] pl-3">
                  {high.map((it) => (
                    <PrototypeRow key={it.id} prdId={prdId} item={it} />
                  ))}
                  {high.length === 0 && (
                    <p className="text-[11.5px] text-faint">
                      No still-open high-fidelity item resolves to this section — the count
                      may be held by an item not yet linked here.
                    </p>
                  )}
                </div>
              )}
            </React.Fragment>
          );
        })}
        {cov.section_count === 0 && (
          <p className="text-[12.5px] text-faint">No `##` sections in this PRD yet.</p>
        )}
      </div>
    </div>
  );
}

/** One high-fidelity item, one handoff: emit → paste → look → verdict → (confirm flip).
 *
 *  Generation stays OUTSIDE this app on purpose — the design tool is where screens are
 *  made; what the loop was missing was the context going over and the conclusion coming
 *  back, which are exactly the two calls here. The fidelity flip is a button the author
 *  presses, never something the verdict route does by itself. */
export function PrototypeRow({ prdId, item }: { prdId: string; item: Item }) {
  const qc = useQueryClient();
  const updateItem = useUpdateItem();
  const [pack, setPack] = React.useState<string | null>(null);
  const [copied, setCopied] = React.useState(false);
  const [file, setFile] = React.useState<File | null>(null);
  const [verdict, setVerdict] = React.useState("");
  const [result, setResult] = React.useState<PrototypeVerdictOut | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");

  async function emit() {
    setBusy(true);
    setErr("");
    try {
      const out = await api.prdPrototypeEmit(prdId, { item_id: item.id });
      setPack(out.prompt_pack);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!pack) return;
    try {
      await navigator.clipboard.writeText(pack);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setErr("clipboard blocked — select the text and copy manually");
    }
  }

  async function submitVerdict() {
    if (!file) {
      setErr("attach the screenshot you looked at — the verdict alone re-enters the grill, and the artifact URL is what makes it checkable");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const att = await publicApi.uploadAttachment(file);
      if (!att) {
        setErr("screenshot upload failed (only png/jpeg/gif/webp are stored)");
        return;
      }
      const out = await api.prdPrototypeVerdict(prdId, {
        item_id: item.id,
        attachment_id: att.id,
        verdict,
      });
      setResult(out);
      setVerdict("");
      setFile(null);
      qc.invalidateQueries({ queryKey: ["prd-coverage", prdId] });
      qc.invalidateQueries({ queryKey: keys.items });
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  function confirmFlip() {
    if (!item.id) return;
    updateItem.mutate(
      { id: item.id, body: { fidelity: "low" } },
      {
        onSuccess: () => qc.invalidateQueries({ queryKey: ["prd-coverage", prdId] }),
        onError: (e) => setErr(String((e as Error).message || e)),
      },
    );
  }

  return (
    <div className="rounded-[10px] border border-line-2 bg-surface-2 p-2.5">
      <div className="flex items-center gap-2">
        <FlaskConical size={13} className="flex-none text-[#e0b34a]" />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">{item.title}</span>
        <span className="font-mono text-[10px] text-faint">{item.id}</span>
      </div>
      {pack === null ? (
        <div className="mt-2">
          <Button size="sm" variant="outline" onClick={emit} disabled={busy}>
            {busy ? "Preparing…" : "Prototype this"}
          </Button>
        </div>
      ) : (
        <div className="mt-2 space-y-2">
          <div className="text-[11.5px] text-muted">
            Paste into the design tool — one prompt, one screen:
          </div>
          <div className="relative">
            <pre className="max-h-40 overflow-auto rounded-lg border border-line-2 bg-surface-2 p-2 font-mono text-[10.5px] leading-relaxed text-muted">
              {pack}
            </pre>
            <button
              onClick={copy}
              className="absolute right-1.5 top-1.5 rounded border border-line-2 bg-surface p-1 text-faint hover:text-fg"
              title="Copy the prompt-pack"
            >
              {copied ? <Check size={12} className="text-st-done" /> : <Copy size={12} />}
            </button>
          </div>
          {result === null ? (
            <div className="space-y-2">
              <div className="text-[11.5px] text-muted">
                Looked at it? Bring the verdict back:
              </div>
              <input
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block w-full font-mono text-[10.5px] text-faint file:mr-2 file:rounded file:border file:border-line-2 file:bg-surface-4 file:px-2 file:py-1 file:text-[10.5px]"
              />
              <textarea
                value={verdict}
                onChange={(e) => setVerdict(e.target.value)}
                rows={2}
                placeholder="What the prototype settled, in words — this text is what the grill grades"
                className="w-full rounded-lg border border-line-2 bg-surface-2 p-2 text-[12px] text-fg-2 outline-none placeholder:text-faint focus:border-line-hover"
              />
              <Button size="sm" onClick={submitVerdict} disabled={busy}>
                {busy ? "Carrying…" : "Send verdict to grill"}
              </Button>
            </div>
          ) : (
            <div className="space-y-1.5 rounded-lg border border-[rgba(95,208,122,0.25)] bg-[rgba(95,208,122,0.06)] p-2">
              <div className="text-[11.5px] text-st-done">
                Verdict recorded (grill turn #{result.turn_seq}) · screenshot on the item.
              </div>
              {result.fidelity_proposal && item.fidelity === "high" && (
                <div className="flex items-center gap-2">
                  <span className="text-[11.5px] text-muted">
                    Settled in words now? Drop the prototype flag — your call, never automatic.
                  </span>
                  <Button size="sm" variant="outline" onClick={confirmFlip} disabled={updateItem.isPending}>
                    Set fidelity low
                  </Button>
                </div>
              )}
              <div className="text-[10.5px] text-faint">
                The next grill round grades this verdict against the open question — nothing is
                approved by this button alone.
              </div>
            </div>
          )}
        </div>
      )}
      {err && <p className="mt-1.5 text-[11px] text-st-blocked">{err}</p>}
    </div>
  );
}
