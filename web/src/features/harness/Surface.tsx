import { useState } from "react";

import type { HarnessSurface, HarnessSurfaceGap, HarnessSurfaceKind } from "@/lib/types";

/**
 * Skills and MCP servers a fleet child would load, and where the harnesses differ.
 *
 * A missing report is its own sentence. An empty difference list is only shown once at
 * least two harnesses were fully checked — otherwise "they match" is what a reader hears
 * when the truth is "we could not check the others".
 */
export function SurfaceReport({ surface }: { surface?: HarnessSurface }) {
  if (!surface) {
    return (
      <section data-testid="harness-surface" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
        <Heading />
        <p data-testid="harness-surface-unreported" className="mt-1 text-body text-muted">
          This read did not include a surface report. That is not a finding that the harnesses match.
        </p>
      </section>
    );
  }
  if (!surface.reported) {
    return (
      <section data-testid="harness-surface" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
        <Heading />
        <p data-testid="harness-surface-unreported" className="mt-1 text-body text-muted">
          {surface.reason}
        </p>
      </section>
    );
  }
  return (
    <section data-testid="harness-surface" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <Heading />
      <p className="mt-1 font-mono text-[10.5px] text-faint">
        {surface.host}
        {surface.reported_at ? ` · ${surface.reported_at}` : ""}
      </p>
      {(surface.other_hosts ?? []).length > 0 && (
        <p className="mt-1 text-[12px] text-muted">
          {surface.other_hosts!.length === 1
            ? `Another report is on file from ${surface.other_hosts![0].host}. This panel shows the newest.`
            : `${surface.other_hosts!.length} other machines have reported. This panel shows the newest (${surface.host}).`}
        </p>
      )}
      <ul className="mt-2 flex flex-col gap-1.5">
        {surface.harnesses.map((harness) => (
          <li key={harness.vendor} className="text-body">
            <span className="font-mono">{harness.vendor}</span>
            <span className="text-muted">
              {" "}· skills {labelFor(harness.skills_status, harness.skills.length)}
              {" "}· mcp {labelFor(harness.mcps_status, harness.mcps.length)}
            </span>
            <Reason text={reasonFor(harness.skills_reason, harness.mcps_reason)} />
          </li>
        ))}
      </ul>
      <Kind title="Skills" kind={surface.skills} />
      <Kind title="MCP servers" kind={surface.mcps} />
      {(surface.notes ?? []).length > 0 && (
        <div className="mt-3" data-testid="harness-surface-notes">
          <h3 className="font-mono text-[10.5px] text-faint">NOT FULLY CHECKED</h3>
          <ul className="mt-1 flex flex-col gap-1">
            {surface.notes!.map((note) => (
              <li key={`${note.vendor}:${note.kind}`} className="text-[12px] text-muted">
                <span className="font-mono">{note.vendor}</span> {note.kind}: {note.status}
                {note.reason ? ` — ${note.reason}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function Heading() {
  return (
    <h2 className="font-mono text-[10.5px] text-faint">WHAT EACH HARNESS LOADS</h2>
  );
}

function Reason({ text }: { text: string }) {
  if (!text) return null;
  return <p className="mt-0.5 text-[12px] text-faint">{text}</p>;
}

function labelFor(status: string, count: number): string {
  if (status === "checked") return String(count);
  if (status === "partial") return `partial (${count} found)`;
  return "not checked";
}

function reasonFor(skills: string, mcps: string): string {
  if (!skills) return mcps;
  if (!mcps || mcps === skills) return skills;
  return `${skills} ${mcps}`;
}

function Kind({ title, kind }: { title: string; kind?: HarnessSurfaceKind | null }) {
  if (!kind) return null;
  const groups = group(kind.rows);
  return (
    <div className="mt-3">
      <h3 className="font-mono text-[10.5px] text-faint">{title.toUpperCase()}</h3>
      {kind.reason && kind.rows.length === 0 ? (
        <p className="mt-1 text-body text-muted">{kind.reason}</p>
      ) : kind.rows.length === 0 ? (
        <p className="mt-1 text-body text-muted">
          No {title.toLowerCase()} differ among {kind.compared.join(", ")}.
          {kind.partial.length > 0
            ? ` ${kind.partial.join(", ")} ${kind.partial.length === 1 ? "was" : "were"} only partly scanned, so a name not listed there is not counted as missing.`
            : ""}
        </p>
      ) : (
        <ul className="mt-1 flex flex-col gap-2">
          {groups.map((item) => (
            <li key={item.key} data-testid="harness-surface-gap">
              <p className="text-body">
                {item.label}{" "}
                <span className="font-mono text-[10.5px] text-faint">({item.names.length})</span>
              </p>
              <Names names={item.names} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Names({ names }: { names: string[] }) {
  const [open, setOpen] = useState(false);
  const shown = open ? names : names.slice(0, 8);
  return (
    <div>
      <ul className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
        {shown.map((name) => (
          <li key={name} className="font-mono text-[12px] text-muted">{name}</li>
        ))}
      </ul>
      {names.length > 8 && (
        <button
          type="button"
          className="mt-1 font-mono text-[10.5px] text-faint underline"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Show fewer" : `Show all ${names.length}`}
        </button>
      )}
    </div>
  );
}

function group(rows: HarnessSurfaceGap[]) {
  const groups = new Map<string, { key: string; label: string; names: string[] }>();
  for (const row of rows) {
    const key = [row.present.join(","), row.absent.join(","), row.disabled_on.join(",")].join("|");
    let item = groups.get(key);
    if (!item) {
      item = { key, label: gapLabel(row), names: [] };
      groups.set(key, item);
    }
    item.names.push(row.name);
  }
  return [...groups.values()];
}

function gapLabel(row: HarnessSurfaceGap): string {
  const parts: string[] = [];
  if (row.present.length) parts.push(`on ${row.present.join(", ")}`);
  else if (row.partial_present.length) parts.push(`seen by ${row.partial_present.join(", ")}`);
  if (row.disabled_on.length) parts.push(`disabled on ${row.disabled_on.join(", ")}`);
  if (row.absent.length) parts.push(`absent from ${row.absent.join(", ")}`);
  return parts.join("; ") || "differs";
}
