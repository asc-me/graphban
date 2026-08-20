import { Info, Plus, Trash2, UserPlus, X } from "lucide-react";
import * as React from "react";

import { errorDetail } from "@/lib/errors";
import {
  useAddTeamMember,
  useCreateTeam,
  useDeleteTeam,
  useOrgMembers,
  useOrgs,
  useProjects,
  useRemoveTeamMember,
  useRevokeTeamGrant,
  useSetTeamGrant,
  useTeams,
} from "@/lib/queries";
import type { Team, TeamGrant } from "@/lib/types";

/**
 * Screen 8 — Teams (PRD-21 D5).
 *
 * A grant is the unit of access administration, and the thing this screen has to make
 * legible is that **a grant materializes**: it writes real project memberships rather
 * than being resolved when someone asks for a page. Two consequences drive the UI.
 *
 * First, a grant row says how many people it currently reaches, because "granted" and
 * "somebody gained access" are different facts — a team with no members grants nothing.
 *
 * Second, a member who already had **direct** access is marked as such, with a note that
 * revoking will not take theirs away. That is the difference between a revoke that does
 * what the admin expects and one that quietly does less than they think.
 */
export function OrgTeams() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const orgId = org?.id ?? "";
  const { data: teams = [], isLoading } = useTeams(orgId);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [creating, setCreating] = React.useState(false);

  const selected = teams.find((t) => t.id === selectedId) ?? teams[0] ?? null;

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <div className="mb-4 flex gap-2.5 rounded-[10px] border border-line bg-surface px-3 py-2.5">
        <Info size={13} className="mt-0.5 shrink-0 text-faint" />
        <span className="max-w-[80ch] text-[11.5px] leading-relaxed text-muted">
          A grant gives every member of this team access to that project. It is written as
          real project access the moment you make it — not resolved later — so adding
          someone to a team changes what they can reach immediately.
        </span>
      </div>

      {isLoading ? (
        <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8 text-center font-mono text-[11px] text-faint-2">
          loading…
        </div>
      ) : teams.length === 0 && !creating ? (
        <NoTeams onCreate={() => setCreating(true)} />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[264px_1fr] lg:items-start">
          <TeamList
            teams={teams}
            selectedId={selected?.id ?? null}
            onSelect={setSelectedId}
            onCreate={() => setCreating(true)}
          />
          {selected && <TeamDetail team={selected} orgId={orgId} />}
        </div>
      )}

      {creating && <CreateTeam orgId={orgId} onDone={() => setCreating(false)} />}
    </div>
  );
}

function NoTeams({ onCreate }: { onCreate: () => void }) {
  return (
    <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8">
      <div className="text-[15px] font-semibold">No teams yet</div>
      <p className="mt-2 max-w-[58ch] text-[12.5px] leading-relaxed text-muted">
        Access is currently granted one person at a time. A team is worth making when the
        same set of people need the same projects — the grant becomes the thing you
        administer, instead of a list of individuals.
      </p>
      <button
        onClick={onCreate}
        className="mt-4 h-[30px] rounded-lg border border-accent/35 bg-accent/[0.14] px-3 text-[12.5px] font-semibold text-accent"
      >
        Create a team
      </button>
    </div>
  );
}

function TeamList({
  teams,
  selectedId,
  onSelect,
  onCreate,
}: {
  teams: Team[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-[13px] border border-line bg-surface-2">
      <div className="border-b border-line bg-surface px-3.5 py-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
        TEAMS · {teams.length}
      </div>
      {teams.map((t) => (
        <button
          key={t.id}
          onClick={() => onSelect(t.id)}
          className={`flex w-full items-center gap-2.5 border-b border-line px-3.5 py-2.5 text-left hover:bg-surface-3 ${
            t.id === selectedId ? "bg-surface-3" : ""
          }`}
        >
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[12.5px] text-fg-2">{t.name}</span>
            <span className="mt-0.5 block font-mono text-[9.5px] text-faint">
              {t.members.length} member{t.members.length === 1 ? "" : "s"} ·{" "}
              {t.grants.length} grant{t.grants.length === 1 ? "" : "s"}
            </span>
          </span>
        </button>
      ))}
      <button
        onClick={onCreate}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-[12.5px] font-semibold text-accent hover:bg-surface-3"
      >
        <Plus size={13} /> Create team
      </button>
    </div>
  );
}

function TeamDetail({ team, orgId }: { team: Team; orgId: string }) {
  const del = useDeleteTeam(orgId);
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-semibold">{team.name}</h2>
          {team.description && (
            <p className="mt-1 text-[12px] leading-relaxed text-muted">{team.description}</p>
          )}
        </div>
        <button
          onClick={() => setConfirmDelete(true)}
          className="inline-flex h-[26px] items-center gap-1.5 rounded-md border border-st-blocked/30 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-st-blocked hover:bg-st-blocked/10"
        >
          <Trash2 size={11} /> Disband
        </button>
      </div>

      <Members team={team} orgId={orgId} />
      <Grants team={team} orgId={orgId} />

      {confirmDelete && (
        <Confirm
          title={`Disband ${team.name}?`}
          body={
            team.grants.length === 0 ? (
              <>This team grants nothing, so no access changes.</>
            ) : (
              <>
                Its {team.grants.length} grant{team.grants.length === 1 ? "" : "s"} stop
                providing access. Anyone who also has a project directly, or through another
                team, keeps theirs — only what this team alone provided goes away.
              </>
            )
          }
          confirmLabel={del.isPending ? "Disbanding…" : "Disband"}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => del.mutate(team.id, { onSuccess: () => setConfirmDelete(false) })}
        />
      )}
    </div>
  );
}

function Members({ team, orgId }: { team: Team; orgId: string }) {
  const { data: orgMembers = [] } = useOrgMembers(orgId);
  const add = useAddTeamMember(orgId);
  const remove = useRemoveTeamMember(orgId);
  const inTeam = new Set(team.members.map((m) => m.id));
  const addable = orgMembers.filter((m) => !inTeam.has(m.user.id));

  return (
    <section className="overflow-hidden rounded-[13px] border border-line bg-surface-2">
      <div className="flex items-center gap-3 border-b border-line bg-surface px-3.5 py-2.5">
        <span className="flex-1 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          MEMBERS · {team.members.length}
        </span>
        {addable.length > 0 && (
          <select
            value=""
            aria-label="Add a member"
            onChange={(e) =>
              e.target.value && add.mutate({ teamId: team.id, userId: e.target.value })
            }
            className="h-[24px] rounded-md border border-line-2 bg-surface px-1.5 text-[11px]"
          >
            <option value="">+ add member</option>
            {addable.map((m) => (
              <option key={m.user.id} value={m.user.id}>
                {m.user.name}
              </option>
            ))}
          </select>
        )}
      </div>
      {team.members.length === 0 ? (
        <p className="px-3.5 py-4 text-[12px] leading-relaxed text-muted">
          Nobody is in this team, so its grants reach no one. The grants are still real —
          they materialize the moment somebody joins.
        </p>
      ) : (
        team.members.map((u) => (
          <div key={u.id} className="flex items-center gap-3 border-b border-line px-3.5 py-2">
            <span
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border font-mono text-[9px] font-semibold"
              style={{ background: `${u.avatar}1f`, borderColor: `${u.avatar}4d`, color: u.avatar }}
            >
              {u.initials || u.name.slice(0, 2).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">{u.name}</span>
            <span className="font-mono text-[10.5px] text-faint">@{u.handle}</span>
            <button
              onClick={() => remove.mutate({ teamId: team.id, userId: u.id })}
              aria-label={`Remove ${u.handle}`}
              className="text-faint hover:text-st-blocked"
            >
              <X size={13} />
            </button>
          </div>
        ))
      )}
    </section>
  );
}

function Grants({ team, orgId }: { team: Team; orgId: string }) {
  const { data: projects = [] } = useProjects();
  const setGrant = useSetTeamGrant(orgId);
  const granted = new Set(team.grants.map((g) => g.project_id));
  const grantable = projects.filter((p) => !granted.has(p.id));

  return (
    <section className="overflow-hidden rounded-[13px] border border-line bg-surface-2">
      <div className="flex items-center gap-3 border-b border-line bg-surface px-3.5 py-2.5">
        <span className="flex-1 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          GRANTS · {team.grants.length}
        </span>
        {grantable.length > 0 && (
          <select
            value=""
            aria-label="Grant a project"
            onChange={(e) =>
              e.target.value &&
              setGrant.mutate({ teamId: team.id, projectId: e.target.value, access: "read" })
            }
            className="h-[24px] rounded-md border border-line-2 bg-surface px-1.5 text-[11px]"
          >
            <option value="">+ grant project</option>
            {grantable.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </div>
      {team.grants.length === 0 ? (
        <p className="px-3.5 py-4 text-[12px] leading-relaxed text-muted">
          This team grants nothing yet. Its members have whatever access they hold directly
          — being in a team is not itself access.
        </p>
      ) : (
        team.grants.map((g) => (
          <GrantRow key={g.project_id} team={team} grant={g} orgId={orgId} />
        ))
      )}
    </section>
  );
}

function GrantRow({ team, grant, orgId }: { team: Team; grant: TeamGrant; orgId: string }) {
  const setGrant = useSetTeamGrant(orgId);
  const revoke = useRevokeTeamGrant(orgId);
  const [confirming, setConfirming] = React.useState(false);
  const byId = new Map(team.members.map((m) => [m.id, m]));
  const direct = grant.direct_user_ids.map((id) => byId.get(id)).filter(Boolean);

  return (
    <div className="border-b border-line px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="font-mono text-[11.5px] text-fg-2">{grant.tag || grant.name}</span>
        <select
          value={grant.access}
          aria-label={`Access for ${grant.tag || grant.name}`}
          onChange={(e) =>
            setGrant.mutate({
              teamId: team.id,
              projectId: grant.project_id,
              access: e.target.value,
            })
          }
          className="h-[23px] rounded-md border border-line-2 bg-surface px-1.5 font-mono text-[10px]"
        >
          <option value="read">read</option>
          <option value="write">write</option>
        </select>
        {/* "Granted" and "somebody gained access" are different facts — a team with no
            members grants nothing, and this number is what says which happened. */}
        <span className="font-mono text-[10px] text-faint">
          {grant.derived_user_ids.length === 0
            ? "reaches nobody"
            : `grants access to ${grant.derived_user_ids.length} ${
                grant.derived_user_ids.length === 1 ? "person" : "people"
              }`}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => setConfirming(true)}
          className="h-[23px] rounded-md border border-st-blocked/30 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-st-blocked hover:bg-st-blocked/10"
        >
          Revoke
        </button>
      </div>

      {direct.length > 0 && (
        <div className="mt-2 rounded-md border border-line bg-surface px-2 py-1.5">
          <div className="text-[11px] leading-relaxed text-muted">
            {direct.map((u) => (
              <span key={u!.id} className="mr-2 inline-flex items-center gap-1">
                <span className="font-mono text-fg-2">@{u!.handle}</span>
                <span className="rounded border border-line-2 px-1 font-mono text-[8.5px] uppercase tracking-[0.05em] text-faint">
                  direct
                </span>
              </span>
            ))}
            — already granted this project individually, so revoking here will not remove
            their access.
          </div>
        </div>
      )}

      {confirming && (
        <Confirm
          title={`Revoke ${team.name}'s access to ${grant.tag || grant.name}?`}
          body={
            <>
              {grant.derived_user_ids.length === 0
                ? "Nobody currently gets access from this grant, so nothing changes."
                : `${grant.derived_user_ids.length} ${
                    grant.derived_user_ids.length === 1 ? "person loses" : "people lose"
                  } the access this grant provides.`}
              {direct.length > 0 && (
                <>
                  {" "}
                  {direct.map((u) => `@${u!.handle}`).join(", ")} keep
                  {direct.length === 1 ? "s" : ""} theirs — it was granted directly, not by
                  this team.
                </>
              )}
            </>
          }
          confirmLabel={revoke.isPending ? "Revoking…" : "Revoke"}
          onCancel={() => setConfirming(false)}
          onConfirm={() =>
            revoke.mutate(
              { teamId: team.id, projectId: grant.project_id },
              { onSuccess: () => setConfirming(false) },
            )
          }
        />
      )}
    </div>
  );
}

function CreateTeam({ orgId, onDone }: { orgId: string; onDone: () => void }) {
  const create = useCreateTeam(orgId);
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setError("");
    try {
      await create.mutateAsync({ name: name.trim() });
      onDone();
    } catch (err) {
      setError(errorDetail(err, "Could not create the team."));
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-4 flex flex-wrap items-center gap-2 rounded-[13px] border border-accent/25 bg-surface-2 px-3.5 py-3"
    >
      <UserPlus size={14} className="shrink-0 text-accent" />
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Platform"
        aria-label="Team name"
        className="h-[30px] min-w-[200px] flex-1 rounded-lg border border-line-2 bg-surface px-2.5 text-[12.5px] outline-none focus:border-accent/50"
      />
      <button
        type="submit"
        disabled={create.isPending || !name.trim()}
        className="h-[30px] rounded-lg border border-accent/35 bg-accent/[0.14] px-3 text-[12.5px] font-semibold text-accent disabled:opacity-50"
      >
        {create.isPending ? "Creating…" : "Create"}
      </button>
      <button
        type="button"
        onClick={onDone}
        className="h-[30px] rounded-lg border border-line-2 px-3 text-[12.5px] text-muted"
      >
        Cancel
      </button>
      {error && <span className="w-full text-[11px] text-st-blocked">{error}</span>}
    </form>
  );
}

/** A confirmation that states the consequence, never just "are you sure?". */
function Confirm({
  title,
  body,
  confirmLabel,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-6">
      <div className="w-full max-w-[460px] rounded-[13px] border border-line-2 bg-surface-2 p-5">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="h-[30px] rounded-lg border border-line-2 px-3 text-[12.5px] text-muted hover:text-fg"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="h-[30px] rounded-lg border border-st-blocked/40 bg-st-blocked/[0.12] px-3 text-[12.5px] font-semibold text-st-blocked"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
