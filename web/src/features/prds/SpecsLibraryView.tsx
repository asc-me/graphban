import * as React from "react";
import { Link, useNavigate } from "react-router-dom";

import { PlannerEmpty, PlannerError, PrdListSkeleton } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import type { SpecLibrary, SpecLibraryDocument, SpecLibraryPrd, SpecScreen, SyncReport } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSpecLibrary, useSyncStorage } from "@/lib/queries";

import type { ConflictRecordState } from "./ConflictRecordView";
import { prdStatusMeta } from "./meta";
import { HashChip, PROMPT_WORD } from "./SpecReaderView";
import { rowSyncMeta, SyncStoragePanel } from "./SyncStoragePanel";

/**
 * Specs library (GRPH-1318 / GRPH-P66). One list of the project's PRDs, its preamble, and
 * the design set's constraints and screen prompts. The text filter narrows that loaded
 * list by id and title. Selecting a row stays on this page and opens the detail beside it.
 * Open leaves for the reader. A conflict row's only Open is Open conflict.
 */

type Selection =
  | { kind: "preamble" }
  | { kind: "prd"; id: string }
  | { kind: "constraints"; setId: string }
  | { kind: "screen"; setId: string; screen: string };

function hit(q: string, title: string, ids: string[]): boolean {
  if (!q) return true;
  const n = q.toLowerCase();
  return title.toLowerCase().includes(n) || ids.some((id) => id.toLowerCase().includes(n));
}

function promptWord(status: string): string {
  return PROMPT_WORD[status as SpecScreen["prompt"]] ?? status;
}

/** The sync report Open conflict hands to the conflict record. Prefer the live preview. */
function reportFor(id: string, row: SpecLibraryPrd | undefined, live: SyncReport | null): SyncReport {
  if (live && (live.conflicts.includes(id) || live.conflict_details?.some((c) => c.prd_id === id))) {
    return live;
  }
  const fact = row?.conflict;
  return {
    folder: live?.folder ?? "",
    prds_dir: live?.prds_dir ?? "",
    exported: [],
    imported: [],
    updated_db: [],
    updated_file: [],
    conflicts: [id],
    conflict_details: fact
      ? [{
          prd_id: id,
          status: row?.status ?? "",
          ledger_hash: fact.ledger_hash,
          file_hash: fact.file_hash,
          file_name: fact.file_name,
          byte_length: fact.byte_length,
          path: fact.path,
        }]
      : [],
    export_status: live?.export_status ?? "ok",
    in_sync: live?.in_sync ?? 0,
    read_failures: live?.read_failures ?? [],
  };
}

function ConflictCount({ ids, onReport }: { ids: string[]; onReport: (id: string) => SyncReport }) {
  if (ids.length === 0) return null;
  const label = ids.length === 1 ? "1 conflict" : `${ids.length} conflicts`;
  if (ids.length === 1) {
    const id = ids[0];
    const state: ConflictRecordState = { report: onReport(id) };
    return (
      <Link
        to={`/prds/conflict/${id}`}
        state={state}
        data-testid="conflict-count"
        className="font-mono text-[12px] text-danger underline-offset-2 hover:underline"
      >
        {label}
      </Link>
    );
  }
  return (
    <span data-testid="conflict-count" className="font-mono text-[12px] text-danger">
      {label}
    </span>
  );
}

export function SpecsLibraryView() {
  const { activeId } = useProjectCtx();
  const lib = useSpecLibrary(activeId);
  const sync = useSyncStorage(activeId);
  const navigate = useNavigate();
  const [filter, setFilter] = React.useState("");
  const [selected, setSelected] = React.useState<Selection | null>(null);
  const data = lib.data;

  function openConflict(id: string) {
    const row = data?.prds.find((p) => p.id === id);
    const state: ConflictRecordState = { report: reportFor(id, row, sync.data?.report ?? null) };
    navigate(`/prds/conflict/${id}`, { state });
  }

  const waiting = !activeId || lib.isLoading;

  let body: React.ReactNode;
  if (waiting) {
    body = <PrdListSkeleton rows={6} />;
  } else if (lib.isError || !data) {
    body = <PlannerError title="Specs unavailable" onRetry={() => lib.refetch()} />;
  } else if (data.prds.length === 0 && data.preamble == null && data.documents.length === 0) {
    body = (
      <PlannerEmpty
        title="No specs yet"
        description="PRDs, the preamble, and design-set documents will list here."
        action={
          <Link to="/prds/new" className={cn(buttonVariants({ size: "sm" }))}>
            New PRD
          </Link>
        }
      />
    );
  } else {
    body = (
      <LibraryBody
        data={data}
        filter={filter}
        selected={selected}
        onSelect={setSelected}
        onOpenConflict={openConflict}
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="specs-library">
      <PlaceHeader
        viewName="Specs"
        purpose="PRDs, the preamble, and design-set documents. The filter narrows this list by id and title."
        action={
          <div className="flex items-center gap-3">
            {data && (
              <ConflictCount
                ids={data.conflict_ids}
                onReport={(id) => reportFor(id, data.prds.find((p) => p.id === id), sync.data?.report ?? null)}
              />
            )}
            <Link to="/prds/new" className={cn(buttonVariants({ size: "sm" }))}>
              New PRD
            </Link>
          </div>
        }
      />
      <div className="border-b border-line px-5 py-2.5">
        <Input
          aria-label="Filter specs by id or title"
          placeholder="Filter by id or title"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {body}
        {activeId && !lib.isLoading && (
          <SyncStoragePanel
            projectId={activeId}
            data={sync.data}
            isLoading={sync.isLoading}
            isError={sync.isError}
            onRetry={() => sync.refetch()}
          />
        )}
      </div>
    </div>
  );
}

function LibraryBody({
  data,
  filter,
  selected,
  onSelect,
  onOpenConflict,
}: {
  data: SpecLibrary;
  filter: string;
  selected: Selection | null;
  onSelect: (s: Selection) => void;
  onOpenConflict: (id: string) => void;
}) {
  const q = filter.trim();
  const preambleHit = data.preamble != null && hit(q, data.preamble.title, [data.preamble.id]);
  const showPreambleAbsence = data.preamble == null && !q;
  const prds = data.prds.filter((p) => hit(q, p.title, [p.id]));
  const constraints = data.documents.filter(
    (d) => d.kind === "constraints" && hit(q, d.title, [d.set_id, d.prd_id]),
  );
  const screens = data.documents.filter(
    (d) => d.kind === "screen" && hit(q, d.title, [d.set_id, d.prd_id, d.screen]),
  );
  const any = preambleHit || showPreambleAbsence || prds.length > 0 || constraints.length > 0 || screens.length > 0;

  return (
    <div className="grid items-start gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_minmax(280px,360px)]">
      <div className="space-y-5">
        {!any && <p className="text-body text-muted">No specs match this filter.</p>}
        {(preambleHit || showPreambleAbsence) && (
          <Group heading="Preamble">
            {showPreambleAbsence ? (
              <p className="text-body text-muted" data-testid="no-preamble">
                No preamble in this project.
              </p>
            ) : data.preamble ? (
              <RowButton
                testId="spec-row-preamble"
                pressed={selected?.kind === "preamble"}
                onClick={() => onSelect({ kind: "preamble" })}
              >
                <Id title={data.preamble.id} />
                <Title>{data.preamble.title}</Title>
                <span className="font-mono text-[10px] text-muted-2">v{data.preamble.version}</span>
                <HashChip label="hash" hash={data.preamble.content_hash} />
              </RowButton>
            ) : null}
          </Group>
        )}
        {prds.length > 0 && (
          <Group heading="PRDs">
            <ul className="space-y-1.5">
              {prds.map((p) => (
                <li key={p.id}>
                  <PrdRow prd={p} pressed={selected?.kind === "prd" && selected.id === p.id} onSelect={() => onSelect({ kind: "prd", id: p.id })} />
                </li>
              ))}
            </ul>
          </Group>
        )}
        {constraints.length > 0 && (
          <Group heading="Set constraints">
            <ul className="space-y-1.5">
              {constraints.map((d) => (
                <li key={d.set_id}>
                  <DocRow
                    doc={d}
                    pressed={selected?.kind === "constraints" && selected.setId === d.set_id}
                    onSelect={() => onSelect({ kind: "constraints", setId: d.set_id })}
                  />
                </li>
              ))}
            </ul>
          </Group>
        )}
        {screens.length > 0 && (
          <Group heading="Screen prompts">
            <ul className="space-y-1.5">
              {screens.map((d) => (
                <li key={`${d.set_id}:${d.screen}`}>
                  <DocRow
                    doc={d}
                    pressed={selected?.kind === "screen" && selected.setId === d.set_id && selected.screen === d.screen}
                    onSelect={() => onSelect({ kind: "screen", setId: d.set_id, screen: d.screen })}
                  />
                </li>
              ))}
            </ul>
          </Group>
        )}
      </div>
      <Detail data={data} selected={selected} onOpenConflict={onOpenConflict} />
    </div>
  );
}

function Group({ heading, children }: { heading: string; children: React.ReactNode }) {
  const id = `specs-group-${heading.replace(/\s+/g, "-").toLowerCase()}`;
  return (
    <section aria-labelledby={id}>
      <h2 id={id} className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
        {heading}
      </h2>
      {children}
    </section>
  );
}

function RowButton({
  testId,
  pressed,
  onClick,
  children,
}: {
  testId: string;
  pressed: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-[12px] border px-3 py-2 text-left",
        pressed ? "border-control-hover bg-surface-3" : "border-control bg-surface-2 hover:border-control-hover",
      )}
    >
      {children}
    </button>
  );
}

function Id({ title }: { title: string }) {
  return <span className="font-mono text-small text-faint">{title}</span>;
}

function Title({ children }: { children: React.ReactNode }) {
  return <span className="min-w-0 flex-1 truncate text-[13.5px] text-fg-2">{children}</span>;
}

function PrdRow({ prd, pressed, onSelect }: { prd: SpecLibraryPrd; pressed: boolean; onSelect: () => void }) {
  const meta = prdStatusMeta(prd.status);
  const sync = rowSyncMeta(prd.sync_state);
  return (
    <RowButton testId={`spec-row-prd-${prd.id}`} pressed={pressed} onClick={onSelect}>
      <Id title={prd.id} />
      <Title>{prd.title}</Title>
      <span className="font-mono text-[10px] text-muted-2">{prd.version}</span>
      {prd.baseline_hash ? (
        <HashChip label="baseline" hash={prd.baseline_hash} />
      ) : (
        <span data-testid="no-baseline" className="font-mono text-[10.5px] text-faint">
          no baseline
        </span>
      )}
      <HashChip label="live" hash={prd.content_hash} />
      <span className="font-mono text-[10px] uppercase tracking-wide" style={{ color: meta.color }}>
        {meta.label}
      </span>
      <span data-testid={`row-sync-${prd.id}`} className={"font-mono text-[10px] " + sync.className}>
        {sync.label}
      </span>
    </RowButton>
  );
}

function DocRow({ doc, pressed, onSelect }: { doc: SpecLibraryDocument; pressed: boolean; onSelect: () => void }) {
  const testId = doc.kind === "screen" ? `spec-row-screen-${doc.set_id}-${doc.screen}` : `spec-row-constraints-${doc.set_id}`;
  const word = doc.kind === "screen" ? promptWord(doc.status) : doc.status;
  return (
    <RowButton testId={testId} pressed={pressed} onClick={onSelect}>
      <Id title={doc.set_id} />
      <Title>{doc.title}</Title>
      <span className="font-mono text-[10px] text-faint">{doc.prd_id}</span>
      <span className="font-mono text-[10.5px] text-muted">{word}</span>
      {doc.hash ? <HashChip label="hash" hash={doc.hash} /> : <span className="font-mono text-[10.5px] text-faint">no hash yet</span>}
    </RowButton>
  );
}

function Detail({
  data,
  selected,
  onOpenConflict,
}: {
  data: SpecLibrary;
  selected: Selection | null;
  onOpenConflict: (id: string) => void;
}) {
  const navigate = useNavigate();
  const prd = selected?.kind === "prd" ? data.prds.find((p) => p.id === selected.id) : undefined;
  const doc =
    selected?.kind === "constraints"
      ? data.documents.find((d) => d.kind === "constraints" && d.set_id === selected.setId)
      : selected?.kind === "screen"
        ? data.documents.find((d) => d.kind === "screen" && d.set_id === selected.setId && d.screen === selected.screen)
        : undefined;

  let content: React.ReactNode = (
    <p className="text-body text-muted" data-testid="spec-detail-empty">
      Select a spec to see its status, hash, and sync.
    </p>
  );
  if (selected?.kind === "preamble" && data.preamble) {
    const pre = data.preamble;
    content = (
      <>
        <Kind>Preamble</Kind>
        <h2 className="text-[15px] font-semibold text-fg">{pre.title}</h2>
        <Fact label="Version" value={`v${pre.version}`} />
        <div className="mt-2">
          <HashChip label="hash" hash={pre.content_hash} />
        </div>
        <OpenButton onClick={() => navigate("/prds/preamble")}>Open</OpenButton>
      </>
    );
  } else if (prd) {
    const meta = prdStatusMeta(prd.status);
    const sync = rowSyncMeta(prd.sync_state);
    const conflict = prd.sync_state === "conflict";
    content = (
      <>
        <Kind>PRD</Kind>
        <h2 className="text-[15px] font-semibold text-fg">{prd.title}</h2>
        <p className="font-mono text-small text-faint">{prd.id}</p>
        <Fact label="Status" value={meta.label} />
        <Fact label="Version" value={prd.version} />
        <Fact label="Sync" value={sync.label} />
        <div className="mt-2 flex flex-wrap gap-2">
          <HashChip label="live" hash={prd.content_hash} />
          {prd.baseline_hash ? (
            <HashChip label="baseline" hash={prd.baseline_hash} />
          ) : (
            <span data-testid="no-baseline" className="font-mono text-[10.5px] text-faint">
              no baseline
            </span>
          )}
        </div>
        {conflict && (
          <div className="mt-3 space-y-1" data-testid="conflict-detail">
            {prd.conflict && (
              <>
                <Fact label="Ledger hash" value={prd.conflict.ledger_hash} mono testId="conflict-ledger" />
                <Fact label="File hash" value={prd.conflict.file_hash} mono testId="conflict-file" />
                <Fact label="Path" value={prd.conflict.path} mono testId="conflict-path" />
              </>
            )}
            <p className="text-small text-muted" data-testid="conflict-note">
              An approved PRD takes no body from the folder.
            </p>
          </div>
        )}
        <OpenButton onClick={() => (conflict ? onOpenConflict(prd.id) : navigate(`/prds/${prd.id}`))}>
          {conflict ? "Open conflict" : "Open"}
        </OpenButton>
      </>
    );
  } else if (doc && selected?.kind === "constraints") {
    content = (
      <>
        <Kind>Set constraints</Kind>
        <h2 className="text-[15px] font-semibold text-fg">{doc.title}</h2>
        <p className="font-mono text-small text-faint">{doc.set_id} · {doc.prd_id}</p>
        <Fact label="Status" value={doc.status} />
        <Fact label="Set" value={doc.set_status} />
        {doc.hash && (
          <div className="mt-2">
            <HashChip label="hash" hash={doc.hash} />
          </div>
        )}
        <OpenButton onClick={() => navigate(`/prds/constraints/${doc.set_id}`)}>Open</OpenButton>
      </>
    );
  } else if (doc && selected?.kind === "screen") {
    content = (
      <>
        <Kind>Screen prompt</Kind>
        <h2 className="text-[15px] font-semibold text-fg">{doc.title}</h2>
        <p className="font-mono text-small text-faint">{doc.set_id} · {doc.prd_id}</p>
        <Fact label="Status" value={promptWord(doc.status)} />
        {doc.hash ? (
          <div className="mt-2">
            <HashChip label="hash" hash={doc.hash} />
          </div>
        ) : (
          <p className="mt-2 font-mono text-[10.5px] text-faint">no hash yet</p>
        )}
        <OpenButton onClick={() => navigate(`/prds/constraints/${doc.set_id}?screen=${encodeURIComponent(doc.screen)}`)}>
          Open
        </OpenButton>
      </>
    );
  }

  return (
    <aside data-testid="spec-detail" className="rounded-[12px] border border-line-2 bg-surface-2 p-4 lg:sticky lg:top-4">
      {content}
    </aside>
  );
}

function Kind({ children }: { children: React.ReactNode }) {
  return <div className="font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function Fact({ label, value, mono, testId }: { label: string; value: string; mono?: boolean; testId?: string }) {
  return (
    <p className="mt-1 text-small text-muted">
      {label}{" "}
      <span data-testid={testId} className={mono ? "break-all font-mono text-[12px] text-fg-2" : "text-fg-2"}>
        {value}
      </span>
    </p>
  );
}

function OpenButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <Button type="button" size="sm" className="mt-4" data-testid="spec-open" onClick={onClick}>
      {children}
    </Button>
  );
}
