import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import * as React from "react";

import { api } from "@/lib/api";
import type { CodeRelation } from "@/lib/types";

const RELATIONS: CodeRelation[] = ["affects", "implements", "fixes", "tests", "references"];

/**
 * The work→code side of the bridge: shows the code paths a tracker item/request is explicitly
 * linked to, and lets you add (path + relation) or remove links. Reused by items and requests.
 */
export function LinkedCode({
  refId,
  projectId,
  variant = "list",
}: {
  refId: string;
  projectId?: string;
  /** "chips": compact path chips with the add-link form collapsed behind a Link button. */
  variant?: "list" | "chips";
}) {
  const qc = useQueryClient();
  const qkey = ["code-for", projectId, refId];
  const { data: rows = [] } = useQuery({ queryKey: qkey, queryFn: () => api.codeForRef(refId, projectId) });
  const [path, setPath] = React.useState("");
  const [adding, setAdding] = React.useState(false);
  const [relation, setRelation] = React.useState<CodeRelation>("affects");

  const invalidate = () => qc.invalidateQueries({ queryKey: qkey });
  const link = useMutation({
    mutationFn: () => api.codeLink({ ref_id: refId, path: path.trim(), relation }, projectId),
    onSuccess: () => { setPath(""); invalidate(); },
  });
  const unlink = useMutation({
    mutationFn: (p: string) => api.codeUnlink({ ref_id: refId, path: p }, projectId),
    onSuccess: invalidate,
  });

  const chips = variant === "chips";

  return (
    <div>
      {chips && rows.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {rows.map((r) => (
            <span
              key={r.path + r.relation}
              className="group inline-flex max-w-full items-center gap-1.5 rounded-md border border-line-2 bg-surface-2 px-2 py-0.5"
              title={r.node?.summary ?? "not described yet"}
            >
              <span className="flex-none font-mono text-[9px] text-purple-2">{r.relation}</span>
              <span className="min-w-0 truncate font-mono text-small text-fg-2">{r.path}</span>
              <button
                onClick={() => unlink.mutate(r.path)}
                className="flex-none text-faint hover:text-st-blocked"
                aria-label="Unlink"
              >
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      ) : rows.length === 0 ? (
        <p className="text-body text-faint">No code linked yet.</p>
      ) : (
        <div className="space-y-1.5">
          {rows.map((r) => (
            <div
              key={r.path + r.relation}
              className="group flex items-center gap-2 rounded-[9px] border border-line-2 bg-surface-2 px-2.5 py-1.5"
            >
              <span className="flex-none font-mono text-[9px] uppercase tracking-wide text-purple-2">{r.relation}</span>
              <span
                className="min-w-0 flex-1 truncate font-mono text-small text-fg-2"
                title={r.node?.summary ?? "not described yet"}
              >
                {r.path}
              </span>
              {!r.node && <span className="flex-none font-mono text-[9px] text-faint">undescribed</span>}
              <button
                onClick={() => unlink.mutate(r.path)}
                className="flex-none text-faint opacity-0 transition-opacity hover:text-st-blocked group-hover:opacity-100"
                aria-label="Unlink"
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      )}

      {chips && !adding && (
        <button
          onClick={() => setAdding(true)}
          className="mt-2 inline-flex items-center gap-1 text-small text-muted hover:text-fg-2"
        >
          <Plus size={12} /> Link
        </button>
      )}
      {(!chips || adding) && (
      <form
        onSubmit={(e) => { e.preventDefault(); if (path.trim() && !link.isPending) link.mutate(); }}
        className="mt-2 flex items-center gap-1.5"
      >
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="backend/app/services/x.py"
          className="h-8 min-w-0 flex-1 rounded-[8px] border border-control bg-surface-2 px-2 font-mono text-small outline-none placeholder:text-faint focus:border-control-hover"
        />
        <select
          value={relation}
          onChange={(e) => setRelation(e.target.value as CodeRelation)}
          className="h-8 flex-none rounded-[8px] border border-control bg-surface-2 px-1.5 text-small text-muted outline-none"
        >
          {RELATIONS.map((r) => (
            <option key={r} value={r}>{r}</option>
          ))}
        </select>
        <button
          type="submit"
          disabled={!path.trim() || link.isPending}
          className="flex h-8 flex-none items-center gap-1 rounded-[8px] border border-control bg-surface-2 px-2 text-small text-accent disabled:opacity-40"
        >
          <Plus size={13} /> Link
        </button>
      </form>
      )}
    </div>
  );
}
