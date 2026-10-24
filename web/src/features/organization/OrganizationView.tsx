import { Building2, Check, Copy, Mail, X } from "lucide-react";
import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useCreateInvite,
  useInvites,
  useOrgMembers,
  useOrgs,
  useRevokeInvite,
} from "@/lib/queries";
import { errorDetail } from "@/lib/errors";
import type { Invite, Org, OrgRole } from "@/lib/types";

/**
 * Org management (AL-74b): members and pending invites for the caller's org(s).
 * Owners/admins can invite and revoke; members see a read-only roster. Rendered
 * only in hosted mode (the route + nav entry are gated on the config flag).
 */
export function OrganizationView() {
  const { data: orgs = [], isLoading } = useOrgs();
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const org = orgs.find((o) => o.id === activeId) ?? orgs[0] ?? null;

  if (isLoading) {
    return <div className="p-8 font-mono text-[12px] text-faint">loading…</div>;
  }
  if (!org) {
    return <div className="p-8 text-[13px] text-muted">You don't belong to any organization.</div>;
  }

  return (
    <div className="mx-auto max-w-[720px] px-6 py-8">
      <header className="mb-6 flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-[9px] bg-[rgba(198,242,78,0.12)]">
          <Building2 size={17} className="text-accent" />
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[17px] font-semibold tracking-tight">{org.name}</h1>
          <div className="font-mono text-[10px] uppercase tracking-wide text-faint">
            {org.plan} plan · you are {org.role}
          </div>
        </div>
        {orgs.length > 1 && (
          <select
            value={org.id}
            onChange={(e) => setActiveId(e.target.value)}
            className="h-8 rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
          >
            {orgs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        )}
      </header>

      <MembersSection org={org} />
      <InvitesSection org={org} />
    </div>
  );
}

const canManage = (role: OrgRole) => role === "owner" || role === "admin";

function MembersSection({ org }: { org: Org }) {
  const { data: members = [] } = useOrgMembers(org.id);
  return (
    <section className="mb-8">
      <SectionTitle>Members ({members.length})</SectionTitle>
      <div className="overflow-hidden rounded-[12px] border border-line">
        {members.map((m, i) => (
          <div
            key={m.user.id}
            className={`flex items-center gap-3 px-4 py-3 ${i > 0 ? "border-t border-line" : ""}`}
          >
            <Avatar initials={m.user.initials} color={m.user.avatar} size={28} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium">{m.user.name}</div>
              <div className="truncate font-mono text-small text-faint">{m.user.email}</div>
            </div>
            <span className="rounded-md bg-surface-4 px-2 py-0.5 font-mono text-[10px] uppercase text-muted">
              {m.role}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function InvitesSection({ org }: { org: Org }) {
  const manage = canManage(org.role);
  const { data: invites = [] } = useInvites(manage ? org.id : undefined);

  return (
    <section>
      <SectionTitle>Pending invitations</SectionTitle>
      {manage && <InviteForm org={org} />}
      {invites.length === 0 ? (
        <p className="text-body text-muted">No pending invitations.</p>
      ) : (
        <div className="overflow-hidden rounded-[12px] border border-line">
          {invites.map((inv, i) => (
            <InviteRow key={inv.id} org={org} invite={inv} first={i === 0} manage={manage} />
          ))}
        </div>
      )}
    </section>
  );
}

function InviteForm({ org }: { org: Org }) {
  const createInvite = useCreateInvite(org.id);
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState("member");
  const [error, setError] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setError("");
    try {
      await createInvite.mutateAsync({ email: email.trim(), role });
      setEmail("");
    } catch (err) {
      // Surfaces the server message from the invite endpoint.
      setError(errorDetail(err, "Could not send the invitation."));
    }
  }

  return (
    <form onSubmit={submit} className="mb-4 flex items-center gap-2">
      <div className="relative flex-1">
        <Mail size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
        <Input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="teammate@company.com"
          className="pl-8"
        />
      </div>
      <select
        value={role}
        onChange={(e) => setRole(e.target.value)}
        className="h-9 rounded-[8px] border border-control bg-surface-2 px-2 text-[12px]"
      >
        <option value="member">Member</option>
        <option value="admin">Admin</option>
      </select>
      <Button type="submit" disabled={createInvite.isPending || !email.trim()}>
        {createInvite.isPending ? "Sending…" : "Invite"}
      </Button>
      {error && <span className="text-small text-st-blocked">{error}</span>}
    </form>
  );
}

function InviteRow({
  org,
  invite,
  first,
  manage,
}: {
  org: Org;
  invite: Invite;
  first: boolean;
  manage: boolean;
}) {
  const revoke = useRevokeInvite(org.id);
  const [copied, setCopied] = React.useState(false);

  function copyLink() {
    navigator.clipboard?.writeText(invite.accept_url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  return (
    <div className={`flex items-center gap-3 px-4 py-3 ${first ? "" : "border-t border-line"}`}>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px]">{invite.email}</div>
        <div className="font-mono text-[10px] uppercase tracking-wide text-faint">
          {invite.role}
        </div>
      </div>
      {manage && (
        <>
          <button
            onClick={copyLink}
            title="Copy invite link"
            className="flex items-center gap-1 rounded-md px-2 py-1 text-small text-muted hover:bg-surface-3 hover:text-fg"
          >
            {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
            {copied ? "Copied" : "Link"}
          </button>
          <button
            onClick={() => revoke.mutate(invite.id)}
            disabled={revoke.isPending}
            title="Revoke invitation"
            className="rounded-md p-1 text-faint hover:bg-surface-3 hover:text-st-blocked"
          >
            <X size={14} />
          </button>
        </>
      )}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-2.5 font-mono text-small uppercase tracking-wide text-faint">{children}</h2>
  );
}
