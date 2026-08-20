import {
  BarChart3,
  Inbox,
  ListChecks,
  Network,
  Radar,
  Users,
} from "lucide-react";
import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import {
  useCodeMap,
  useFleet,
  useGalaxy,
  useItems,
  useOrgs,
  usePrds,
  useShards,
} from "@/lib/queries";
import { ORG_BASE, projectPath } from "@/lib/routes";
import type { GalaxyEdge } from "@/lib/types";

/**
 * Screen 6 — Project home (PRD-21 D7).
 *
 * D7's whole content is that the project plane is the existing app, reused unchanged: a PM
 * driving the grill-to-decompose loop needs no new capability, only a place in the
 * hierarchy and a URL. So this adds no backend — it is a landing pad that says what is
 * here and routes into surfaces that already exist.
 *
 * The one piece of genuine information is the dependency strip, which reads the galaxy
 * from this project's point of view: what it depends on, and what depends on it. Both
 * directions matter, and only one of them is visible from inside the repo.
 */
export function ProjectHome() {
  const { active } = useProjectCtx();
  const { data: orgs = [] } = useOrgs();
  const projectId = active?.id ?? "";
  const { data: items = [] } = useItems(projectId);
  const { data: shards = [] } = useShards(projectId);
  const { data: map } = useCodeMap(projectId);
  const { data: prds = [] } = usePrds(projectId);
  const { data: fleet } = useFleet(projectId);
  const { data: galaxy } = useGalaxy(orgs[0]?.id);

  if (!active) return null;

  const nodeCount = map?.node_count ?? 0;
  const inFlight = items.filter((i) => i.status === "in_progress").length;
  const liveAgents = (fleet?.agents ?? []).filter((a) => a.state !== "offline").length;

  const edges = galaxy?.edges ?? [];
  const nameOf = (id: string) =>
    galaxy?.nodes.find((n) => n.id === id)?.tag ?? id;
  const dependsOn = edges.filter((e) => e.src === projectId);
  const dependedBy = edges.filter((e) => e.dst === projectId);

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: active.accent }} />
        <h1 className="text-[19px] font-semibold tracking-[-0.3px]">{active.name}</h1>
        <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[10px] text-muted">
          {active.tag}
        </span>
      </div>
      {active.description && (
        <p className="mt-1.5 max-w-[76ch] text-[12.5px] leading-relaxed text-muted">
          {active.description}
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 font-mono text-[10px] uppercase tracking-[0.05em]">
        <Count label="items" value={items.length} />
        <Count label="in flight" value={inFlight} tone={inFlight ? "text-accent" : undefined} />
        <Count label="prds" value={prds.length} />
        <Count label="memory shards" value={shards.length} />
        <Count label="graph nodes" value={nodeCount} />
        <Count
          label="agents live"
          value={liveAgents}
          tone={liveAgents ? "text-st-done" : undefined}
        />
      </div>

      {nodeCount === 0 && <NoGraphYet tag={active.tag} />}

      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {SURFACES.map((s) => (
          <Link
            key={s.to}
            to={projectPath(active.tag, s.to)}
            className="rounded-[13px] border border-line bg-surface-2 p-4 transition-colors hover:border-line-hover"
          >
            <div className="flex items-center gap-2.5">
              <span className="text-accent">{s.icon}</span>
              <span className="text-[13.5px] font-semibold">{s.label}</span>
            </div>
            <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">{s.desc}</p>
          </Link>
        ))}
      </div>

      <Dependencies
        dependsOn={dependsOn}
        dependedBy={dependedBy}
        nameOf={nameOf}
        knowsGalaxy={galaxy !== undefined}
      />
    </div>
  );
}

const SURFACES = [
  { to: "tracker", label: "Tracker", icon: <ListChecks size={15} />,
    desc: "One linear stream of work, in priority order." },
  { to: "prds", label: "PRDs", icon: <BarChart3 size={15} />,
    desc: "Draft, grill until approval is earned, then decompose into tracked items." },
  { to: "triage", label: "Triage", icon: <Radar size={15} />,
    desc: "What came in, beside the in-flight work it would collide with." },
  { to: "code", label: "Code graph", icon: <Network size={15} />,
    desc: "The structure agents describe as they work, and the arrows that leave the repo." },
  { to: "memory-review", label: "Memory", icon: <Inbox size={15} />,
    desc: "Lessons waiting for a human to vouch for them." },
  { to: "fleet", label: "Agents", icon: <Users size={15} />,
    desc: "Who is working here right now, what they hold, and for how long." },
];

function Count({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-faint-2">{label}</span>
      <span className={`text-[12.5px] ${tone ?? "text-fg-2"}`}>{value.toLocaleString()}</span>
    </span>
  );
}

/**
 * An empty code graph over a real repository, named as such.
 *
 * The same failure the graph view guards: nothing described looks identical to nothing to
 * describe, and the reassuring reading is the wrong one.
 */
function NoGraphYet({ tag }: { tag: string }) {
  return (
    <div className="mt-4 rounded-[13px] border border-st-review/30 bg-st-review/[0.06] px-3.5 py-3">
      <div className="text-[13px] font-semibold text-st-review">
        No deployment has pushed a code graph to this project
      </div>
      <p className="mt-1 max-w-[72ch] text-[12px] leading-relaxed text-st-review/80">
        Empty because nothing has arrived, not because{" "}
        <span className="font-mono text-fg-2">{tag}</span> has no structure. A linked box
        builds the graph locally and pushes summaries up — run{" "}
        <code className="font-mono text-[11.5px]">graphban link</code> on the machine with
        the checkout.
      </p>
    </div>
  );
}

/**
 * This project's place in the galaxy, both ways round.
 *
 * "What depends on me" is the half you cannot see from inside the repo, and it is the half
 * that decides whether a change here is safe. An org that has never pushed a manifest has
 * no answer to either question — which is different from the answer being "nothing", so
 * the two are not rendered alike.
 */
function Dependencies({
  dependsOn,
  dependedBy,
  nameOf,
  knowsGalaxy,
}: {
  dependsOn: GalaxyEdge[];
  dependedBy: GalaxyEdge[];
  nameOf: (id: string) => string;
  knowsGalaxy: boolean;
}) {
  if (!knowsGalaxy) return null;

  return (
    <section className="mt-5 rounded-[13px] border border-line bg-surface-2 p-4">
      <div className="flex items-center gap-2.5">
        <h2 className="flex-1 text-[14px] font-semibold">Cross-project dependencies</h2>
        <Link
          to={`${ORG_BASE}/galaxy`}
          className="font-mono text-[9px] uppercase tracking-[0.06em] text-st-next"
        >
          Open in galaxy →
        </Link>
      </div>

      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        <Direction
          title="DEPENDS ON"
          edges={dependsOn}
          other={(e) => nameOf(e.dst)}
          empty="Nothing internal — every dependency resolved to an external package."
        />
        <Direction
          title="DEPENDED ON BY"
          edges={dependedBy}
          other={(e) => nameOf(e.src)}
          empty="No sibling repo declares this one. Changes here reach nothing else in the org."
        />
      </div>
    </section>
  );
}

function Direction({
  title,
  edges,
  other,
  empty,
}: {
  title: string;
  edges: GalaxyEdge[];
  other: (e: GalaxyEdge) => string;
  empty: string;
}) {
  return (
    <div>
      <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
        {title}
      </div>
      {edges.length === 0 ? (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">{empty}</p>
      ) : (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {edges.map((e) => (
            <div
              key={e.id}
              className={`flex items-center gap-2 rounded-[7px] border px-2 py-1.5 ${
                e.fresh ? "border-line" : "border-dashed border-line opacity-60"
              }`}
            >
              <span className="font-mono text-[11px] text-fg-2">{other(e)}</span>
              {/* The evidence count, because an edge with more files behind it is a
                  stronger claim than one resting on a single line. */}
              <span className="font-mono text-[9.5px] text-faint">
                {e.evidence.length} file{e.evidence.length === 1 ? "" : "s"}
              </span>
              {!e.fresh && (
                <span className="font-mono text-[9px] uppercase tracking-[0.05em] text-st-review">
                  stale
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
