import { Info } from "lucide-react";
import { Link } from "react-router-dom";

import { useProjects } from "@/lib/queries";

/**
 * Screen F — Integrations, drawn as a matrix rather than a list of on/off cards.
 *
 * The reason is a scope mismatch worth designing around rather than past: GitHub and
 * Google Drive connections exist, but `PlatformConfig` is **one row per project**. There
 * is no org-level connection to toggle. So the honest shape of this screen is
 * connector × project, and the per-project rows are the weight of it.
 *
 * Jira, Linear, Confluence and Trello are PRD-23 and carry their own chip; GitHub and
 * Google Drive do not, because those two are real.
 */
const CONNECTORS = [
  { id: "github", label: "GitHub", backed: true },
  { id: "gdrive", label: "Google Drive", backed: true },
  { id: "jira", label: "Jira", backed: false },
  { id: "linear", label: "Linear", backed: false },
  { id: "confluence", label: "Confluence", backed: false },
  { id: "trello", label: "Trello", backed: false },
];

export function OrgIntegrations() {
  const { data: projects = [] } = useProjects();

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <div className="mb-4 flex gap-2.5 rounded-[10px] border border-line bg-surface px-3 py-2.5">
        <Info size={13} className="mt-0.5 shrink-0 text-faint" />
        <span className="max-w-[80ch] text-[11.5px] leading-relaxed text-muted">
          An integration connects to <span className="text-fg-2">one project</span>, not to the
          org — connection settings live per project. This page is the matrix of which projects
          have which connector; connecting one happens on that project's settings.
        </span>
      </div>

      <div className="overflow-x-auto rounded-[13px] border border-line bg-surface-2">
        <div style={{ minWidth: 640 }}>
          <div className="flex items-center gap-3 border-b border-line bg-surface px-3.5 py-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
            <span className="w-[160px] shrink-0">CONNECTOR</span>
            <span className="min-w-0 flex-1">PROJECTS</span>
          </div>
          {CONNECTORS.map((c) => (
            <div key={c.id} className="flex items-start gap-3 border-b border-line px-3.5 py-3">
              <span className="flex w-[160px] shrink-0 flex-col gap-1.5">
                <span className="text-[12.5px] text-fg-2">{c.label}</span>
                {!c.backed && (
                  <span className="w-fit rounded border border-purple/30 px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.05em] text-purple">
                    not backed
                  </span>
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-wrap gap-1.5">
                {!c.backed ? (
                  <span className="text-[11.5px] leading-relaxed text-muted">
                    PRD-23. A connected tracker raises a question of authority — which system owns
                    an item's status — that this screen does not settle.
                  </span>
                ) : projects.length === 0 ? (
                  <span className="font-mono text-[10.5px] text-faint-2">no projects yet</span>
                ) : (
                  projects.map((p) => (
                    <Link
                      key={p.id}
                      to="/settings"
                      className="inline-flex items-center gap-1.5 rounded-[5px] border border-line px-1.5 py-px hover:border-line-hover"
                    >
                      <span
                        className="h-[5px] w-[5px] shrink-0 rounded-sm"
                        style={{ background: p.accent }}
                      />
                      <span className="font-mono text-[10.5px] text-fg-2">{p.tag}</span>
                      <span className="font-mono text-[9px] uppercase text-faint">configure</span>
                    </Link>
                  ))
                )}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
