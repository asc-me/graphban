import { Check, Copy, Info, UserPlus, X } from "lucide-react";
import * as React from "react";
import { NavLink, Route, Routes } from "react-router-dom";

import { copyText } from "@/lib/clipboard";
import { errorDetail } from "@/lib/errors";
import { useAuth } from "@/features/auth/AuthContext";
import {
  useBilling,
  useCreateInvite,
  useInvites,
  useOrgMembers,
  useOrgs,
  useRemoveMember,
  useRevokeInvite,
  useSetMemberRole,
} from "@/lib/queries";
import { adminPath } from "@/lib/routes";
import type { Invite, OrgMember } from "@/lib/types";

import { OrgRoles } from "./OrgRoles";

/**
 * Screen B — Users & access.
 *
 * Two tabs: Members (this file, backed) and Roles (speculative, see {@link OrgRoles}).
 *
 * The load-bearing detail is the seat reconciliation. `quotas.seat_count` counts
 * memberships **plus pending invites**, so the seat meter deliberately runs ahead of the
 * table below it. Showing the number without the explanation makes the table look like a
 * miscount, which is the reading this screen exists to prevent.
 */
export function OrgUsers() {
  return (
    <div className="max-w-[1180px] px-6 pt-4">
      <div className="flex items-center gap-1 border-b border-line">
        <Tab to="users" label="Members" />
        <Tab to="users/roles" label="Roles" speculative />
      </div>
      <Routes>
        <Route index element={<MembersTab />} />
        <Route path="roles" element={<OrgRoles />} />
      </Routes>
    </div>
  );
}

function Tab({ to, label, speculative }: { to: string; label: string; speculative?: boolean }) {
  return (
    <NavLink
      end
      to={adminPath(to)}
      className={({ isActive }) =>
        `flex items-center gap-2 rounded-t-[9px] border border-b-0 px-3.5 py-2 text-[13px] ${
          isActive
            ? "border-line bg-surface-2 font-semibold text-fg"
            : "border-transparent font-medium text-muted hover:text-fg"
        }`
      }
    >
      {label}
      {speculative && (
        <span className="rounded border border-purple/30 px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.05em] text-purple">
          not backed
        </span>
      )}
    </NavLink>
  );
}

function MembersTab() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: members = [], isLoading } = useOrgMembers(org?.id);
  const { data: invites = [] } = useInvites(org?.id);
  const { data: billing } = useBilling(org?.id);
  const [inviting, setInviting] = React.useState(false);

  const seatLimit = billing?.limits.max_seats ?? 0;
  const seatsUsed = billing?.usage.seats ?? members.length + invites.length;
  const atCap = seatLimit > 0 && seatsUsed >= seatLimit;
  const pct = seatLimit > 0 ? Math.min(100, Math.round((seatsUsed / seatLimit) * 100)) : 0;
  const seatTone = atCap ? "text-st-blocked" : pct >= 80 ? "text-st-review" : "text-fg";

  return (
    <div className="pb-16 pt-5">
      <div className="mb-4 flex items-start gap-4">
        <p className="min-w-0 max-w-[72ch] flex-1 text-[12.5px] leading-relaxed text-muted">
          Org role is <span className="font-mono text-fg-2">owner</span>,{" "}
          <span className="font-mono text-fg-2">admin</span> or{" "}
          <span className="font-mono text-fg-2">member</span> — those three, nothing else. What
          someone can reach is set per project on their membership, shown in the access column.
        </p>
        <div className="flex shrink-0 items-start gap-3">
          <div className="text-right">
            <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">SEATS</div>
            <div className="mt-1 flex items-baseline justify-end gap-1.5">
              <span className={`font-mono text-[14px] ${seatTone}`}>{seatsUsed}</span>
              <span className="font-mono text-[11px] text-faint-2">/ {seatLimit || "—"}</span>
            </div>
            <div className="mt-1.5 h-[3px] w-[132px] overflow-hidden rounded-sm bg-line">
              <div
                className={`h-full ${atCap ? "bg-st-blocked" : pct >= 80 ? "bg-st-review" : "bg-st-done"}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="mt-1.5 font-mono text-[9px] text-faint">
              {members.length} member{members.length === 1 ? "" : "s"} · {invites.length} invited
            </div>
          </div>
          <button
            onClick={() => setInviting((v) => !v)}
            disabled={atCap}
            className="inline-flex h-[30px] items-center gap-2 rounded-lg border border-accent/35 bg-accent/[0.14] px-3 text-[12.5px] font-semibold text-accent disabled:border-line disabled:bg-transparent disabled:text-faint-2"
          >
            <UserPlus size={13} /> Invite
          </button>
        </div>
      </div>

      <div className="mb-4 flex gap-2.5 rounded-[10px] border border-line bg-surface px-3 py-2.5">
        <Info size={13} className="mt-0.5 shrink-0 text-faint" />
        <span className="text-[11.5px] leading-relaxed text-muted">
          A seat is a membership <em className="not-italic text-fg-2">or</em> a pending invite, so
          the seat count runs ahead of this table by the number of invites outstanding.{" "}
          {invites.length === 0
            ? "Nothing is pending, so the two agree."
            : `${invites.length} of the ${seatsUsed} seats in use ${
                invites.length === 1 ? "is" : "are"
              } reserved by an invite nobody has accepted.`}
        </span>
      </div>

      {atCap && (
        <div className="mb-4 rounded-[13px] border border-st-review/30 bg-st-review/[0.06] px-3.5 py-3">
          <div className="text-[13px] font-semibold text-st-review">
            Every seat on the {billing?.plan} plan is taken
          </div>
          <div className="mt-1 text-[12px] leading-relaxed text-st-review/80">
            Inviting is disabled until a seat frees — revoke a pending invite or remove a member.
            Plans are operator-assigned, so raising the cap is not something this screen can do.
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8 text-center font-mono text-[11px] text-faint-2">
          loading…
        </div>
      ) : members.length <= 1 && invites.length === 0 ? (
        <JustYou onInvite={() => setInviting(true)} />
      ) : (
        <MemberTable members={members} orgId={org?.id ?? ""} />
      )}

      {inviting && org && <InviteForm orgId={org.id} onDone={() => setInviting(false)} />}
      <PendingInvites invites={invites} orgId={org?.id ?? ""} />
    </div>
  );
}

function JustYou({ onInvite }: { onInvite: () => void }) {
  return (
    <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8">
      <div className="text-[15px] font-semibold">It's just you in here</div>
      <p className="mt-2 max-w-[56ch] text-[12.5px] leading-relaxed text-muted">
        You created this org, so you are the owner — the one role that cannot be changed. Nobody
        has been invited yet: that is a new org, not an empty table.
      </p>
      <button
        onClick={onInvite}
        className="mt-4 h-[30px] rounded-lg border border-accent/35 bg-accent/[0.14] px-3 text-[12.5px] font-semibold text-accent"
      >
        Invite your first teammate
      </button>
    </div>
  );
}

const ROLE_TONE: Record<string, string> = {
  owner: "text-accent border-accent/30 bg-accent/[0.07]",
  admin: "text-st-review border-st-review/30 bg-st-review/[0.07]",
  member: "text-muted border-line",
};

function MemberTable({ members, orgId }: { members: OrgMember[]; orgId: string }) {
  return (
    <div className="overflow-x-auto rounded-[13px] border border-line bg-surface-2">
      <div style={{ minWidth: 900 }}>
        <div className="flex items-center gap-3 border-b border-line bg-surface px-3.5 py-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          <span className="w-[186px] shrink-0">MEMBER</span>
          <span className="w-[148px] shrink-0">EMAIL</span>
          <span className="w-[84px] shrink-0">ORG ROLE</span>
          <span className="min-w-0 flex-1">PROJECT ACCESS</span>
          <span className="w-[82px] shrink-0 text-right">LAST WRITE</span>
          <span className="flex w-[178px] shrink-0 justify-end">ACTIONS</span>
        </div>
        {members.map((m) => (
          <MemberRow key={m.user.id} member={m} orgId={orgId} />
        ))}
        <div className="flex gap-2.5 bg-surface px-3.5 py-2.5">
          <span className="text-[11px] leading-relaxed text-muted">
            The owner's actions stay disabled: ownership belongs to the account that created
            the org, and one that can lose its last owner is one nobody can administer. You
            cannot change or remove yourself either.
          </span>
        </div>
      </div>
    </div>
  );
}

function MemberRow({ member, orgId }: { member: OrgMember; orgId: string }) {
  const { user, role, access, last_write_at } = member;
  const isOwner = role === "owner";
  const { user: me } = useAuth();
  const isSelf = me?.id === user.id;
  const setRole = useSetMemberRole(orgId);
  const [confirming, setConfirming] = React.useState(false);
  return (
    <div
      data-testid={`member-${user.handle}`}
      className="flex items-center gap-3 border-b border-line px-3.5 py-2.5 hover:bg-surface-3"
    >
      <span className="flex w-[186px] min-w-0 shrink-0 items-center gap-2.5">
        <span
          className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full border font-mono text-[10px] font-semibold"
          style={{ background: `${user.avatar}1f`, borderColor: `${user.avatar}4d`, color: user.avatar }}
        >
          {user.initials || user.name.slice(0, 2).toUpperCase()}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] text-fg-2">{user.name}</span>
          <span className="mt-px block font-mono text-[10px] text-faint">@{user.handle}</span>
        </span>
      </span>
      <span className="w-[148px] min-w-0 shrink-0 truncate font-mono text-[11px] text-muted">
        {user.email}
      </span>
      <span className="w-[84px] shrink-0">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.05em] ${
            ROLE_TONE[role] ?? ROLE_TONE.member
          }`}
        >
          <span className="h-[5px] w-[5px] rounded-full bg-current" />
          {role}
        </span>
      </span>
      <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        {access.length === 0 ? (
          <span className="font-mono text-[10.5px] text-faint-2">no project access</span>
        ) : (
          access.map((a) => (
            <span
              key={a.project_id}
              className="inline-flex items-center gap-1.5 rounded-[5px] border border-line px-1.5 py-px"
            >
              <span className="font-mono text-[10.5px] text-fg-2">{a.tag || a.name}</span>
              <span
                className={`font-mono text-[9px] uppercase ${
                  a.level === "write" ? "text-st-done" : "text-muted"
                }`}
              >
                {a.level}
              </span>
            </span>
          ))
        )}
      </span>
      <span className="w-[82px] shrink-0 text-right">
        <LastWrite at={last_write_at} />
      </span>
      <span className="flex w-[178px] shrink-0 items-center justify-end gap-1">
        <select
          value={role}
          aria-label={`Role for ${user.handle}`}
          disabled={isOwner || isSelf || setRole.isPending}
          title={
            isOwner
              ? "The owner cannot be demoted."
              : isSelf
                ? "You cannot change your own role."
                : undefined
          }
          onChange={(e) => setRole.mutate({ userId: user.id, role: e.target.value })}
          className="h-[23px] rounded-md border border-line-2 bg-surface px-1.5 font-mono text-[10px] disabled:cursor-not-allowed disabled:text-faint-2"
        >
          <option value="owner" disabled>
            owner
          </option>
          <option value="admin">admin</option>
          <option value="member">member</option>
        </select>
        <button
          onClick={() => setConfirming(true)}
          disabled={isOwner || isSelf}
          title={
            isOwner
              ? "The owner cannot be removed."
              : isSelf
                ? "You cannot remove yourself."
                : undefined
          }
          className="h-[23px] rounded-md border border-st-blocked/30 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-st-blocked disabled:cursor-not-allowed disabled:border-line disabled:text-faint-2"
        >
          Remove
        </button>
      </span>
      {confirming && (
        <RemoveConfirm member={member} orgId={orgId} onClose={() => setConfirming(false)} />
      )}
    </div>
  );
}

/**
 * The confirmation names exactly what is lost.
 *
 * Removing someone cascades their project access, and a dialog that said only "are you
 * sure?" would hide the half of the action with consequences — the seat is the visible
 * part, the access is the part that mattered.
 */
function RemoveConfirm({
  member,
  orgId,
  onClose,
}: {
  member: OrgMember;
  orgId: string;
  onClose: () => void;
}) {
  const remove = useRemoveMember(orgId);
  const { user, access } = member;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-6">
      <div className="w-full max-w-[440px] rounded-[13px] border border-line-2 bg-surface-2 p-5">
        <h2 className="text-[15px] font-semibold">
          Remove {user.name} from this organization?
        </h2>
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
          Their seat is freed immediately.{" "}
          {access.length === 0 ? (
            <>They have no project access, so nothing else changes.</>
          ) : (
            <>
              Access to{" "}
              <span className="font-mono text-fg-2">
                {access.map((a) => a.tag || a.name).join(", ")}
              </span>{" "}
              is revoked with it — {access.length === 1 ? "that project" : "those projects"}{" "}
              become unreachable for them.
            </>
          )}
        </p>
        <p className="mt-2 text-[11.5px] leading-relaxed text-faint">
          Work they authored is unaffected: items, PRDs and memory reference an id, not a
          seat.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="h-[30px] rounded-lg border border-line-2 px-3 text-[12.5px] text-muted hover:text-fg"
          >
            Cancel
          </button>
          <button
            disabled={remove.isPending}
            onClick={() => remove.mutate(user.id, { onSuccess: onClose })}
            className="h-[30px] rounded-lg border border-st-blocked/40 bg-st-blocked/[0.12] px-3 text-[12.5px] font-semibold text-st-blocked disabled:opacity-50"
          >
            {remove.isPending ? "Removing…" : "Remove"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Same distinction the operator plane draws: no write on record is not inactivity. */
function LastWrite({ at }: { at: string | null }) {
  if (!at) {
    return (
      <span
        className="font-mono text-[10px] text-faint-2"
        title="No write on record. Reads aren't logged, so this is not evidence of inactivity."
      >
        no writes
      </span>
    );
  }
  const s = Math.max(0, Math.floor((Date.now() - new Date(/Z|[+-]\d\d:?\d\d$/.test(at) ? at : `${at}Z`).getTime()) / 1000));
  const rel =
    s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
  return <span className="font-mono text-[10.5px] text-muted">{rel}</span>;
}

function InviteForm({ orgId, onDone }: { orgId: string; onDone: () => void }) {
  const create = useCreateInvite(orgId);
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("member");
  const [error, setError] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setError("");
    try {
      await create.mutateAsync({ email: email.trim(), role });
      setEmail("");
      onDone();
    } catch (err) {
      setError(errorDetail(err, "Could not send the invitation."));
    }
  }

  return (
    <form onSubmit={submit} className="mt-4 flex flex-wrap items-center gap-2 rounded-[13px] border border-accent/25 bg-surface-2 px-3.5 py-3">
      <input
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="teammate@company.dev"
        aria-label="Invitee email"
        className="h-[30px] min-w-[220px] flex-1 rounded-lg border border-line-2 bg-surface px-2.5 font-mono text-[12px] outline-none focus:border-accent/50"
      />
      <select
        value={role}
        onChange={(e) => setRole(e.target.value)}
        aria-label="Role"
        className="h-[30px] rounded-lg border border-line-2 bg-surface px-2 text-[12px]"
      >
        <option value="member">member</option>
        <option value="admin">admin</option>
      </select>
      <button
        type="submit"
        disabled={create.isPending || !email.trim()}
        className="h-[30px] rounded-lg border border-accent/35 bg-accent/[0.14] px-3 text-[12.5px] font-semibold text-accent disabled:opacity-50"
      >
        {create.isPending ? "Sending…" : "Send invite"}
      </button>
      {error && <span className="w-full text-[11px] text-st-blocked">{error}</span>}
    </form>
  );
}

function PendingInvites({ invites, orgId }: { invites: Invite[]; orgId: string }) {
  return (
    <section className="mt-5 overflow-hidden rounded-[13px] border border-line bg-surface-2">
      <div className="flex items-center gap-3 border-b border-line bg-surface px-3.5 py-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
        <span className="min-w-0 flex-1">PENDING INVITES · {invites.length} — EACH HOLDS A SEAT</span>
      </div>
      {invites.length === 0 ? (
        <div className="px-3.5 py-5 text-center">
          <div className="text-[13px] font-semibold text-fg-2">Nothing pending</div>
          <div className="mt-1.5 text-[11.5px] leading-relaxed text-muted">
            Every seat in use is a real member. Accepted, revoked and expired invites are kept in
            history — nothing is purged.
          </div>
        </div>
      ) : (
        invites.map((i) => <InviteRow key={i.id} invite={i} orgId={orgId} />)
      )}
      <div className="border-t border-line px-3.5 py-2.5 text-[11px] leading-relaxed text-faint">
        Expiry is one deployment-wide setting applied to every invite this deployment issues.
        There is no per-invite choice to make here.
      </div>
    </section>
  );
}

function InviteRow({ invite, orgId }: { invite: Invite; orgId: string }) {
  const revoke = useRevokeInvite(orgId);
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-center gap-3 border-b border-line px-3.5 py-2.5">
      <span className="flex min-w-0 flex-1 items-center gap-2.5">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-st-review" />
        <span className="truncate font-mono text-[12px] text-fg-2">{invite.email}</span>
      </span>
      <span className="w-[74px] shrink-0 font-mono text-[9.5px] uppercase text-muted">
        {invite.role}
      </span>
      <span className="flex w-[152px] shrink-0 justify-end gap-1">
        <button
          onClick={async () => {
            if (await copyText(invite.accept_url)) {
              setCopied(true);
              setTimeout(() => setCopied(false), 1600);
            }
          }}
          className="inline-flex h-[23px] items-center gap-1 rounded-md border border-line-2 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-muted hover:border-accent/40 hover:text-accent"
        >
          {copied ? <Check size={10} /> : <Copy size={10} />}
          {copied ? "COPIED" : "COPY LINK"}
        </button>
        <button
          onClick={() => revoke.mutate(invite.id)}
          disabled={revoke.isPending}
          className="inline-flex h-[23px] items-center rounded-md border border-st-blocked/30 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-st-blocked disabled:opacity-50"
        >
          <X size={10} />
          REVOKE
        </button>
      </span>
    </div>
  );
}
