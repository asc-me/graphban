import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { errorDetail } from "@/lib/errors";
import { useArchiveProject, useProjectsIncludingArchived, useUnarchiveProject } from "@/lib/queries";

/** Settings → Project → Archive (GRPH-1955). A soft, reversible put-away for a finished walk or
 * test project: it leaves the project picker and `list_projects`, its items refuse claims and
 * writes, and its custom and adopted rules stop holding. Its tag, items and history stay readable
 * by id. Owner or admin only, with a reason; archived projects are listed here to unarchive. */
export function ProjectArchivePanel() {
  const { active } = useProjectCtx();
  const all = useProjectsIncludingArchived();
  const archive = useArchiveProject();
  const unarchive = useUnarchiveProject();
  const [reason, setReason] = React.useState("");
  const archived = (all.data ?? []).filter((p) => p.archived_at);

  return (
    <div className="mb-4" data-testid="project-archive">
      <div className="text-body font-medium text-fg-2">Archive</div>
      <p className="mb-2 text-[12px] text-faint">
        Archiving puts a finished project away: it leaves the project picker and agents' project
        lists, and nothing can be claimed, delegated, created or updated in it. Its tag, items and
        history stay readable, and you can unarchive it here. Only a project owner or admin can do
        this.
      </p>
      {active && !active.archived_at && (
        <form
          className="flex max-w-xl items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (reason.trim()) archive.mutate({ id: active.id, reason: reason.trim() }, { onSuccess: () => setReason("") });
          }}
        >
          <Input
            aria-label="Reason for archiving"
            placeholder="Why archive it? e.g. the walk is finished"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
          />
          <Button type="submit" variant="outline" disabled={!reason.trim() || archive.isPending}>
            Archive {active.tag}
          </Button>
        </form>
      )}
      {archive.isError && <p className="mt-1 text-body text-red-500">{errorDetail(archive.error, "Could not archive")}</p>}
      {archived.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[12px] font-medium text-fg-2">Archived projects</div>
          <ul className="space-y-1.5">
            {archived.map((p) => (
              <li key={p.id} className="flex items-center gap-2 text-body text-fg-2">
                <span className="font-mono text-[12px]">{p.tag}</span>
                <span>{p.name}</span>
                {p.archived_reason && <span className="text-faint">— {p.archived_reason}</span>}
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  disabled={unarchive.isPending}
                  onClick={() => unarchive.mutate(p.id)}
                >
                  Unarchive {p.tag}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {unarchive.isError && <p className="mt-1 text-body text-red-500">{errorDetail(unarchive.error, "Could not unarchive")}</p>}
    </div>
  );
}
