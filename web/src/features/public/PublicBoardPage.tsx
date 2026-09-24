import * as React from "react";
import { useParams } from "react-router-dom";

import { PublicPageShell } from "./PublicPageShell";

interface BoardRow {
  id: string;
  title: string;
  type: string;
  status: string;
  votes: number;
  comments: { id: string; body: string; created_at: string }[];
  linked_status?: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  new: "#8b949e",
  triaging: "#e0b34a",
  linked: "#7ca2ff",
  accepted: "#58a6ff",
  done: "#3fb950",
};

export function PublicBoardPage({ kind }: { kind: "issues" | "requests" }) {
  const { token } = useParams<{ token: string }>();
  const [rows, setRows] = React.useState<BoardRow[] | null>(null);
  const [error, setError] = React.useState(false);

  React.useEffect(() => {
    if (!token) return;
    const endpoint = kind === "issues" ? "issues" : "requests";
    fetch(`/api/public/boards/${endpoint}?token=${encodeURIComponent(token)}`)
      .then((r) => {
        if (r.status === 404) {
          setError(true);
          return [];
        }
        return r.ok ? r.json() : [];
      })
      .then(setRows)
      .catch(() => {
        setError(true);
        setRows([]);
      });
  }, [token, kind]);

  const title = kind === "issues" ? "Issues" : "Requests";

  return (
    <PublicPageShell>
      {error ? (
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">Not found</div>
          <p className="mt-2 text-[13px] text-muted">
            This board does not exist or is not public.
          </p>
        </div>
      ) : rows === null ? (
        <div className="py-20 text-center text-[13px] text-muted">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">No published {kind} yet</div>
          <p className="mt-2 text-[13px] text-muted">
            The operator has not published any {kind} to this board.
          </p>
        </div>
      ) : (
        <>
          <div className="mb-6">
            <h1 className="text-[20px] font-bold tracking-tight">{title}</h1>
            <p className="mt-1 text-[12px] text-muted">
              Public {kind} board · {rows.length} published
            </p>
          </div>
          <div className="space-y-2">
            {rows.map((row) => (
              <BoardRowCard key={row.id} row={row} />
            ))}
          </div>
        </>
      )}
    </PublicPageShell>
  );
}

function BoardRowCard({ row }: { row: BoardRow }) {
  const [voted, setVoted] = React.useState(false);
  const [votes, setVotes] = React.useState(row.votes);

  async function handleVote() {
    if (voted) return;
    const resp = await fetch(`/api/public/requests/${row.id}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delta: 1 }),
    });
    if (resp.ok) {
      setVoted(true);
      setVotes((v) => v + 1);
    }
  }

  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 p-4">
      <div className="flex items-start gap-3">
        <button
          onClick={handleVote}
          disabled={voted}
          className="flex flex-none flex-col items-center gap-0.5 rounded-lg border border-line-2 bg-surface px-2.5 py-1.5 transition-colors hover:border-accent/40 disabled:opacity-50"
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className={voted ? "text-accent" : "text-faint"}>
            <path d="M8 2l6 10H2L8 2z" fill="currentColor" />
          </svg>
          <span className="font-mono text-[11px] text-fg-2">{votes}</span>
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-medium text-fg">{row.title}</span>
            <span
              className="flex-none rounded-md border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide"
              style={{ color: STATUS_COLOR[row.status] ?? "#8b949e", borderColor: STATUS_COLOR[row.status] ?? "#8b949e" }}
            >
              {row.status}
            </span>
            {row.linked_status && (
              <span className="font-mono text-[9px] text-faint">→ {row.linked_status}</span>
            )}
          </div>
          {row.comments.length > 0 && (
            <div className="mt-2 space-y-1.5 border-t border-line pt-2">
              {row.comments.map((c) => (
                <div key={c.id} className="text-[12px] text-fg-2">
                  {c.body}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
