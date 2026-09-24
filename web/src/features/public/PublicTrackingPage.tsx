import * as React from "react";
import { useParams } from "react-router-dom";

import { PublicPageShell } from "./PublicPageShell";

interface TrackingData {
  title: string;
  type: string;
  status: string;
  votes: number;
  linked_status?: string | null;
  comments: { id: string; body: string; created_at: string }[];
}

const STATUS_COLOR: Record<string, string> = {
  new: "#8b949e",
  triaging: "#e0b34a",
  linked: "#7ca2ff",
  accepted: "#58a6ff",
  done: "#3fb950",
};

export function PublicTrackingPage() {
  const { trackToken } = useParams<{ trackToken: string }>();
  const [data, setData] = React.useState<TrackingData | null>(null);
  const [error, setError] = React.useState(false);

  React.useEffect(() => {
    if (!trackToken) return;
    fetch(`/api/public/t/${encodeURIComponent(trackToken)}`)
      .then((r) => {
        if (r.status === 404) {
          setError(true);
          return null;
        }
        return r.ok ? r.json() : null;
      })
      .then((d) => {
        if (d) setData(d);
        else setError(true);
      })
      .catch(() => setError(true));
  }, [trackToken]);

  return (
    <PublicPageShell>
      {error ? (
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">Not found</div>
          <p className="mt-2 text-[13px] text-muted">
            This tracking link is invalid or has expired.
          </p>
        </div>
      ) : data === null ? (
        <div className="py-20 text-center text-[13px] text-muted">Loading…</div>
      ) : (
        <>
          <div className="mb-6">
            <div className="flex items-center gap-2">
              <h1 className="text-[20px] font-bold tracking-tight">{data.title}</h1>
              <span
                className="rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide"
                style={{ color: STATUS_COLOR[data.status] ?? "#8b949e", borderColor: STATUS_COLOR[data.status] ?? "#8b949e" }}
              >
                {data.status}
              </span>
            </div>
            <div className="mt-1 flex items-center gap-3 text-[12px] text-muted">
              <span>{data.type}</span>
              <span>·</span>
              <span>{data.votes} votes</span>
              {data.linked_status && (
                <>
                  <span>·</span>
                  <span>Linked: {data.linked_status}</span>
                </>
              )}
            </div>
          </div>

          {data.comments.length > 0 ? (
            <div className="space-y-3">
              <div className="font-mono text-[10px] uppercase tracking-wide text-faint">
                Public comments
              </div>
              {data.comments.map((c) => (
                <div key={c.id} className="rounded-[10px] border border-line-2 bg-surface-2 p-3">
                  <div className="text-[13px] text-fg-2">{c.body}</div>
                  <div className="mt-1.5 font-mono text-[10px] text-faint">
                    {new Date(c.created_at).toLocaleDateString()}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="py-8 text-center text-[13px] text-muted">
              No public comments yet.
            </div>
          )}
        </>
      )}
    </PublicPageShell>
  );
}
