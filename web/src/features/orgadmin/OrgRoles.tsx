import { Inert, NotAvailable, SPECULATIVE_ENABLED, SpeculativeHeader } from "./Speculative";

/**
 * Screen C — Roles & permissions. Speculative; see the rev2 prompt set for why.
 *
 * Graphban already has three role systems, and a fourth has to say how it relates to all
 * three:
 *
 * | System | Values | Enforced |
 * | --- | --- | --- |
 * | Org role | owner / admin / member | `authz.require_org_admin` |
 * | Project access | write / read / none | `authz.can_read` / `can_write`, per project |
 * | Fleet agent role | planner / worker / reviewer | `fleet.ROLES`, at call time, by credential |
 *
 * The five roles the first design drew — OWNER, ADMIN, PRODUCT, DEVELOPER, ANALYST —
 * collapse two different things: the first two are authority, the last three are job
 * function. Permissions attach to authority; a job title tells a human who to ask. Both
 * are useful and they are not the same field.
 *
 * So this renders the choice rather than pre-empting it, and nothing here writes.
 */
export function OrgRoles() {
  if (!SPECULATIVE_ENABLED) return <NotAvailable />;
  return (
    <div className="pb-16 pt-5">
      <SpeculativeHeader
        title="Roles & permissions"
        blocker="Roles are hardcoded strings, not rows — nothing stores a role definition and no endpoint writes one. D8 (changing a member's role at all) is unbuilt, so this sits on top of a write that does not exist yet."
      >
        Two shapes this could take. They are not the same size, and Design cannot settle
        which — both are drawn so the choice is made against pictures rather than prose.
      </SpeculativeHeader>

      <div className="grid gap-4 lg:grid-cols-2">
        <Variant
          tag="VARIANT 1"
          title="Named presets over the axes that exist"
          body="A role is a name plus a preset: one org role, and a default per-project access level. Developer = member + write. Analyst = member + read. No new enforcement and no policy engine — the role is a shortcut for two fields that are already checked on every request."
          cost="Cheap. A lookup table and a form."
          rows={[
            ["Owner", "owner · write everywhere", "built-in"],
            ["Admin", "admin · write everywhere", "built-in"],
            ["Member", "member · per-project", "built-in"],
            ["Developer", "member · write by default", "preset"],
            ["Analyst", "member · read by default", "preset"],
          ]}
        />
        <Variant
          tag="VARIANT 2"
          title="Custom roles with a permission matrix"
          body="A role is a row and permissions are checkboxes. Every checkbox must name a real enforcement point — the nine that exist are: read a project, write a project, invite members, administer the org, assign project access, mint an API key, mint an enrolment seat, approve a memory shard, publish a PRD baseline. A tenth checkbox with nothing behind it is the failure this whole set is written to prevent."
          cost="A permissions table, a policy layer, and a migration."
          rows={[
            ["Owner", "all nine · not editable", "built-in"],
            ["Admin", "eight of nine", "built-in"],
            ["Member", "read + write project", "built-in"],
            ["Release manager", "4 selected", "custom"],
          ]}
        />
      </div>

      <div className="mt-5 rounded-[13px] border border-line bg-surface px-4 py-3">
        <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          NOT THE SAME AS FLEET ROLES
        </div>
        <p className="mt-2 max-w-[80ch] text-[12px] leading-relaxed text-muted">
          These are roles for <span className="text-fg-2">people</span>. Agent roles — planner,
          worker, reviewer — are assigned per agent in Fleet and enforced by credential at call
          time, so no change on this screen can grant or remove one. Blurring the two would put a
          human role in front of a gate that exists to stop an agent reviewing its own work.
        </p>
      </div>
    </div>
  );
}

function Variant({
  tag,
  title,
  body,
  cost,
  rows,
}: {
  tag: string;
  title: string;
  body: string;
  cost: string;
  rows: [string, string, string][];
}) {
  return (
    <section className="rounded-[13px] border border-line bg-surface-2 p-4">
      <div className="flex items-center gap-2.5">
        <span className="rounded border border-purple/30 px-1.5 py-px font-mono text-[9px] uppercase tracking-[0.06em] text-purple">
          {tag}
        </span>
        <h3 className="text-[14px] font-semibold">{title}</h3>
      </div>
      <p className="mt-2 text-[12px] leading-relaxed text-muted">{body}</p>
      <div className="mt-2 font-mono text-[10px] text-faint">COST — {cost}</div>
      <Inert why="Drawn to be reviewed. Nothing here writes — there is no endpoint behind it.">
        <div className="mt-3 overflow-hidden rounded-[10px] border border-line">
          {rows.map(([name, grants, kind]) => (
            <div
              key={name}
              className="flex items-center gap-3 border-b border-line px-3 py-2 last:border-b-0"
            >
              <span className="w-[124px] shrink-0 font-mono text-[11.5px] text-fg-2">{name}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-muted">
                {grants}
              </span>
              <span className="shrink-0 rounded border border-line px-1.5 font-mono text-[8.5px] uppercase tracking-[0.05em] text-faint">
                {kind}
              </span>
            </div>
          ))}
        </div>
      </Inert>
    </section>
  );
}
