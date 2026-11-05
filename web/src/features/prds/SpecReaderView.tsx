import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import * as React from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useProjectCtx } from "@/features/ProjectContext";
import { api, type SpecBodyState, type SpecDesignSet, type SpecScreen } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Markdown } from "@/lib/markdown";
import { keys, useItems, useMe, useSpecDesignSet, useSpecPreamble } from "@/lib/queries";

/**
 * The Spec reader (GRPH-1319 / GRPH-P66). One reader, three documents: a PRD (the editor in
 * `PrdEditorView`, which keeps its grill / coverage / acceptance / assistant views), the
 * project preamble, and the set constraints of one design set. The last two get preview and
 * history only, so nothing reachable from them can write either document.
 *
 * Color: the approved green (`st-done`) is used for the status `approved` and the word
 * `baseline` on a hash, nowhere else. Every other state word is readable as a word.
 */

export type SpecKind = "PRD" | "Preamble" | "Set constraints";

/** The reader header: names the document kind; Back returns to Specs. */
export function ReaderHeader({ kind, children }: { kind: SpecKind; children?: React.ReactNode }) {
  const navigate = useNavigate();
  return (
    <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-3" data-testid="spec-reader-header">
      <button onClick={() => navigate("/prds")} className="inline-flex items-center gap-1 text-small text-faint hover:text-fg">
        <ArrowLeft size={16} />
        Specs
      </button>
      <span className="rounded border border-control px-1.5 py-px font-mono text-meta uppercase tracking-wide text-muted" data-testid="spec-reader-kind">
        {kind}
      </span>
      {children}
    </div>
  );
}

export function shortHash(h: string): string {
  const bare = h.replace(/^sha256:/, "");
  return bare.slice(0, 12);
}

/** A hash, labeled. `baseline` is the only label drawn in the approved color. */
export function HashChip({ label, hash }: { label: "baseline" | "live" | "hash"; hash: string }) {
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[10.5px]" title={hash} data-testid={`hash-${label}`}>
      <span className={label === "baseline" ? "text-st-done" : "text-faint"}>{label}</span>
      <span className="text-muted-2">{shortHash(hash)}</span>
    </span>
  );
}

/** The detail an ApiError carries, without the JSON wrapper. */
export function errText(e: unknown): string {
  const msg = String((e as Error)?.message ?? e);
  try {
    const parsed = JSON.parse(msg);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // not JSON — the raw text is the message
  }
  return msg;
}

function isNotFound(e: unknown): boolean {
  return (e as { status?: number } | null)?.status === 404;
}

/** A body that is not `stored` is said, never drawn as a blank document. */
export function BodyView({ state, body, absent }: { state: SpecBodyState; body: string | null; absent: string }) {
  if (state === "pending") {
    return <p className="text-body text-muted" data-testid="body-pending">The body is pending: the row exists and its text has not landed yet. This is not an empty spec.</p>;
  }
  if (state === "unreadable") {
    return <p className="text-body text-st-blocked" data-testid="body-unreadable">The stored body could not be read back to this row's hash. That is a read failure, not an empty spec.</p>;
  }
  if (state === "absent" || body === null) {
    return <p className="text-body text-muted" data-testid="body-absent">{absent}</p>;
  }
  if (body.trim() === "") {
    return <p className="text-body text-muted" data-testid="body-empty">The stored body is empty, and its hash matches.</p>;
  }
  return <Markdown source={body} />;
}

function NotFound({ kind, what }: { kind: SpecKind; what: string }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ReaderHeader kind={kind} />
      <div className="flex flex-1 items-center justify-center text-[13px] text-muted" data-testid="spec-not-found">
        {what} was not found.
      </div>
    </div>
  );
}

type DocTab = "preview" | "history";

function DocTabs({ tab, onTab }: { tab: DocTab; onTab: (t: DocTab) => void }) {
  return (
    <div className="flex items-center gap-1 rounded-lg border border-line-2 bg-surface-2 p-0.5" data-testid="spec-doc-tabs">
      {(["preview", "history"] as DocTab[]).map((t) => (
        <button
          key={t}
          data-tab={t}
          onClick={() => onTab(t)}
          className={cn("rounded-md px-2 py-1 text-[11.5px] capitalize", tab === t ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2")}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- preamble

export function PreambleReaderView() {
  const { activeId } = useProjectCtx();
  const qc = useQueryClient();
  const { data: pre, error, isLoading } = useSpecPreamble(activeId);
  const [tab, setTab] = React.useState<DocTab>("preview");
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [cites, setCites] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");

  if (isLoading) return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  if (error && !isNotFound(error)) {
    return <NotFound kind="Preamble" what={`The preamble could not be read (${errText(error)}); it`} />;
  }
  if (!pre) return <NotFound kind="Preamble" what="The project preamble" />;

  function startEdit() {
    if (!pre) return;
    // An unreadable body starts from the sections the ledger holds, said as such below.
    setDraft(pre.body ?? pre.sections.map((s) => (s.heading ? `## ${s.heading}\n${s.text}` : s.text)).join("\n\n"));
    setCites(pre.cites_prd_id);
    setErr("");
    setEditing(true);
  }

  async function save() {
    setBusy(true);
    setErr("");
    try {
      const out = await api.putPreamble(activeId, draft, cites.trim());
      qc.setQueryData(keys.specPreamble(activeId), out);
      qc.invalidateQueries({ queryKey: keys.specLibrary(activeId) });
      setEditing(false);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="preamble-reader">
      <ReaderHeader kind="Preamble">
        <span className="min-w-0 flex-1 truncate text-[15px] font-semibold">{pre.title || "Preamble"}</span>
        <span className="rounded-md bg-surface-4 px-2 py-1 font-mono text-[10px] text-muted-2" data-testid="preamble-version">
          v{pre.version}
        </span>
        <HashChip label="hash" hash={pre.content_hash} />
        {editing ? (
          <>
            <Button variant="outline" size="sm" onClick={() => setEditing(false)} disabled={busy}>Cancel</Button>
            <Button size="sm" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</Button>
          </>
        ) : (
          <Button variant="outline" size="sm" onClick={startEdit}>Edit</Button>
        )}
      </ReaderHeader>
      <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-2 text-small text-muted">
        <span>Cites {pre.cites_prd_id} at <HashChip label="baseline" hash={pre.prd_hash} /></span>
        <div className="ml-auto"><DocTabs tab={tab} onTab={setTab} /></div>
      </div>
      {err && <p className="mx-5 mt-2 text-small text-st-blocked" data-testid="spec-error">{err}</p>}
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {pre.stale_sets.length > 0 && (
          <div className="mb-4 rounded-[10px] border border-line-2 bg-surface-2 p-3" data-testid="preamble-stale-sets">
            <div className="mb-1 text-small text-fg-2">
              Design sets that pinned an older preamble version are stale until they are compiled again:
            </div>
            {pre.stale_sets.map((s) => (
              <div key={s.id} className="font-mono text-small text-muted">
                {s.id} · {s.prd_id} · pinned v{s.preamble_version} · {s.status === "stale" ? `stale (${s.stale_reason})` : s.status}
              </div>
            ))}
          </div>
        )}
        {editing ? (
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-small text-muted">
              Cites PRD
              <input value={cites} onChange={(e) => setCites(e.target.value)} className="rounded border border-control bg-surface-2 px-2 py-0.5 font-mono text-small text-fg-2 outline-none" />
            </label>
            {pre.body_state !== "stored" && (
              <p className="text-small text-muted">Starting from the sections the ledger holds; the stored text did not read back to its hash.</p>
            )}
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
              data-testid="preamble-editor"
              className="h-[60vh] w-full resize-none rounded-lg border border-control bg-surface/40 p-3 font-mono text-body text-fg-2 outline-none"
            />
          </div>
        ) : tab === "preview" ? (
          <BodyView state={pre.body_state} body={pre.body} absent="No preamble text has been written." />
        ) : (
          <div className="space-y-1.5" data-testid="preamble-history">
            {[...pre.history].reverse().map((h) => (
              <div key={h.version} className="flex items-center gap-2 rounded-[10px] border border-control bg-surface-2 p-2.5">
                <span className="rounded bg-surface-4 px-1.5 py-0.5 font-mono text-[10px] text-muted-2">v{h.version}</span>
                <HashChip label="hash" hash={h.content_hash} />
                <span className="font-mono text-[10px] text-faint">{h.created_by}</span>
                {h.governing && <span className="ml-auto font-mono text-[9px] uppercase text-fg-2">current</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- set constraints

export const PROMPT_WORD: Record<SpecScreen["prompt"], string> = {
  none: "no prompt yet",
  compiled: "compiled, not accepted",
  accepted: "accepted prompt",
  stale: "stale",
};

export function SetConstraintsReaderView() {
  const { setId = "" } = useParams();
  const [search] = useSearchParams();
  const screenQ = search.get("screen") ?? "";
  const qc = useQueryClient();
  const { activeId } = useProjectCtx();
  const { data: me } = useMe();
  const { data: ds, error, isLoading } = useSpecDesignSet(setId);
  const [tab, setTab] = React.useState<DocTab>("preview");
  const [busy, setBusy] = React.useState<string | null>(null);
  const [err, setErr] = React.useState("");

  if (isLoading) return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  if (!ds) {
    return <NotFound kind="Set constraints" what={error && !isNotFound(error) ? `Design set ${setId} could not be read (${errText(error)}); it` : `Design set ${setId}`} />;
  }

  async function run(what: string, fn: () => Promise<SpecDesignSet>) {
    setBusy(what);
    setErr("");
    try {
      qc.setQueryData(keys.specDesignSet(setId), await fn());
      qc.invalidateQueries({ queryKey: keys.specLibrary(activeId) });
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(null);
    }
  }

  const person = me?.name || me?.email || "";

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="constraints-reader">
      <ReaderHeader kind="Set constraints">
        <span className="min-w-0 flex-1 truncate text-[15px] font-semibold">
          {ds.id} <span className="font-mono text-small text-faint">for {ds.prd_id}</span>
        </span>
        {ds.constraints_hash && <HashChip label="hash" hash={ds.constraints_hash} />}
        <span className="font-mono text-[10.5px] uppercase tracking-wide text-fg-2" data-testid="constraints-acceptance">
          {ds.constraints_accepted ? "accepted" : "not accepted"}
        </span>
        <Button variant="outline" size="sm" disabled title="A candidate body is accepted as written, not hand-edited" data-testid="constraints-edit">
          Edit
        </Button>
        <Button
          size="sm"
          data-testid="constraints-accept"
          disabled={!!busy || ds.constraints_accepted || ds.body_state !== "stored" || !person}
          onClick={() => run("accept", () => api.acceptSetConstraints(setId))}
        >
          {busy === "accept" ? "Accepting…" : person ? `Accept as ${person}` : "Accept (no person in this session)"}
        </Button>
      </ReaderHeader>
      <div className="flex flex-none items-center gap-3 border-b border-line px-5 py-2 text-small text-muted">
        <span data-testid="constraints-note">
          A candidate body is accepted as written, not hand-edited. A later change to the constraints body returns the set to not accepted.
        </span>
        <div className="ml-auto"><DocTabs tab={tab} onTab={setTab} /></div>
      </div>
      {err && <p className="mx-5 mt-2 text-small text-st-blocked" data-testid="spec-error">{err}</p>}
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {tab === "preview" ? (
          <div className="space-y-5">
            <BodyView state={ds.body_state} body={ds.body} absent="No set constraints have been written for this set." />
            <DesignSetScreens ds={ds} busy={busy} current={screenQ} onCompile={(screen) => run(`compile:${screen}`, () => api.compileScreen(setId, screen))} />
          </div>
        ) : (
          <div className="space-y-1.5" data-testid="constraints-history">
            {ds.history.length === 0 && <p className="text-body text-muted">These constraints have never been accepted.</p>}
            {[...ds.history].reverse().map((h, i) => (
              <div key={i} className="flex items-center gap-2 rounded-[10px] border border-control bg-surface-2 p-2.5">
                <HashChip label="hash" hash={h.constraints_hash} />
                <span className="text-small text-fg-2">accepted by {h.person}</span>
                <span className="ml-auto font-mono text-[9px] uppercase text-muted">{h.current ? "current" : "superseded"}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Each screen: its prompt state and whether a compile is refused.
 *  `current` is the screen named by `?screen=` when Specs opened this set. */
export function DesignSetScreens({ ds, busy, onCompile, current }: {
  ds: SpecDesignSet;
  busy?: string | null;
  onCompile?: (screen: string) => void;
  current?: string;
}) {
  return (
    <div data-testid="design-set-screens">
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
        Screens · set {ds.status}{ds.status === "stale" && ds.stale_reason ? ` (${ds.stale_reason})` : ""}
      </div>
      {ds.screens.length === 0 && <p className="text-body text-muted">This set lists no screens yet.</p>}
      <div className="space-y-1.5">
        {ds.screens.map((s) => {
          const on = !!current && current === s.screen;
          return (
            <div
              key={s.screen}
              className={cn(
                "flex items-center gap-2 rounded-[10px] border bg-surface-2 px-3 py-2",
                on ? "border-control-hover" : "border-line-2",
              )}
              data-testid="design-screen"
              data-screen={s.screen}
              data-current={on ? "true" : undefined}
              aria-current={on ? "true" : undefined}
            >
              <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{s.screen}</span>
              <span className="font-mono text-[10.5px] text-muted" data-testid="screen-prompt">{PROMPT_WORD[s.prompt]}</span>
              {s.compile_refused.length > 0 ? (
                <span className="font-mono text-[10.5px] text-muted" data-testid="screen-compile">
                  compile refused: {s.compile_refused.join("; ")}
                </span>
              ) : onCompile ? (
                <Button size="sm" variant="outline" disabled={!!busy} onClick={() => onCompile(s.screen)} data-testid="screen-compile">
                  {busy === `compile:${s.screen}` ? "Compiling…" : "Compile"}
                </Button>
              ) : null}
            </div>
          );
        })}
      </div>
      {ds.constraints_accepted && ds.screens.some((s) => s.prompt === "stale") && (
        <p className="mt-2 text-small text-muted" data-testid="stale-until-compiled">
          Accepting the constraints does not refresh a stale prompt: it stays stale until it is compiled again.
        </p>
      )}
    </div>
  );
}

/** The PRD's design set, summarized for the PRD reader, with Open design set. */
export function DesignSetSummary({ ds }: { ds: SpecDesignSet }) {
  const navigate = useNavigate();
  const n = (p: SpecScreen["prompt"]) => ds.screens.filter((s) => s.prompt === p).length;
  return (
    <div className="flex items-center gap-2 text-small text-muted" data-testid="prd-design-set">
      <span>Design set {ds.id} · {ds.status}</span>
      <span data-testid="prd-design-set-counts">
        {n("accepted")} accepted · {n("stale")} stale · {n("none")} no prompt yet{n("compiled") ? ` · ${n("compiled")} compiled, not accepted` : ""}
      </span>
      <button className="text-fg-2 underline-offset-2 hover:underline" onClick={() => navigate(`/prds/constraints/${ds.id}`)}>
        Open design set
      </button>
    </div>
  );
}

/** The PRD's linked items, with done ones hideable. */
export function LinkedItemsList({ projectId, linked }: { projectId: string; linked: string[] }) {
  const { data: items = [] } = useItems(projectId);
  const [hideDone, setHideDone] = React.useState(false);
  const rows = items.filter((it) => linked.includes(it.id));
  const shown = hideDone ? rows.filter((it) => it.status !== "done") : rows;
  return (
    <div data-testid="prd-linked-items">
      <div className="mb-2 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">Linked items · {linked.length}</span>
        <label className="ml-auto flex items-center gap-1.5 text-small text-muted">
          <input type="checkbox" checked={hideDone} onChange={(e) => setHideDone(e.target.checked)} data-testid="hide-done" />
          Hide done
        </label>
      </div>
      {linked.length === 0 ? (
        <p className="text-body text-muted">No items are linked to this PRD.</p>
      ) : shown.length === 0 && hideDone && rows.length === linked.length ? (
        <p className="text-body text-muted" data-testid="all-done">Every linked item is done.</p>
      ) : (
        <div className="space-y-1">
          {shown.map((it) => (
            <div key={it.id} className="flex items-center gap-2 text-small">
              <span className="w-[72px] flex-none font-mono text-[10px] text-faint">{it.id}</span>
              <span className="min-w-0 flex-1 truncate text-fg-2">{it.title}</span>
              <span className="font-mono text-[10px] text-muted">{it.status}</span>
            </div>
          ))}
          {rows.length < linked.length && (
            <p className="text-small text-muted">{linked.length - rows.length} linked id(s) are not in this project's item list.</p>
          )}
        </div>
      )}
    </div>
  );
}
