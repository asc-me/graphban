import * as React from "react";
import { useParams } from "react-router-dom";

import type { RoadmapPhase } from "@/lib/types";

import { RoadmapBoard } from "@/features/roadmap/RoadmapBoard";
import { PublicPageShell } from "./PublicPageShell";

export function PublicRoadmapPage() {
  const { token } = useParams<{ token: string }>();
  const [phases, setPhases] = React.useState<RoadmapPhase[] | null>(null);
  const [error, setError] = React.useState(false);

  React.useEffect(() => {
    if (!token) return;
    fetch(`/api/public/roadmap?token=${encodeURIComponent(token)}`)
      .then((r) => {
        if (r.status === 404) {
          setError(true);
          return [];
        }
        return r.ok ? r.json() : [];
      })
      .then(setPhases)
      .catch(() => {
        setError(true);
        setPhases([]);
      });
  }, [token]);

  return (
    <PublicPageShell>
      {error ? (
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">Not found</div>
          <p className="mt-2 text-[13px] text-muted">
            This roadmap does not exist or is not public.
          </p>
        </div>
      ) : phases === null ? (
        <div className="py-20 text-center text-[13px] text-muted">Loading…</div>
      ) : phases.length === 0 ? (
        <div className="py-20 text-center">
          <div className="text-[16px] font-semibold text-fg">No roadmap published yet</div>
          <p className="mt-2 text-[13px] text-muted">
            The operator has not published a roadmap for this project.
          </p>
        </div>
      ) : (
        <>
          <div className="mb-6">
            <h1 className="text-[20px] font-bold tracking-tight">Roadmap</h1>
            <p className="mt-1 text-[12px] text-muted">Public read-only roadmap</p>
          </div>
          <RoadmapBoard phases={phases} />
        </>
      )}
    </PublicPageShell>
  );
}
