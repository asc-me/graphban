import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { buttonVariants } from "@/components/ui/button";
import { Button } from "@/components/ui/button";
import { OrgHandleField } from "@/features/profile/OrgHandleField";
import { handleClash, type HandleClash } from "@/features/profile/orgHandle";
import { api } from "@/lib/api";
import { errorDetail } from "@/lib/errors";
import {
  keys,
  useConfig,
  useDeclineInvite,
  useLeaveOrg,
  useMyInvites,
  useOrgMembers,
  useOrgs,
  useSetOrgHandle,
  useSyncStatus,
} from "@/lib/queries";
import type { MyInvite, Org, User } from "@/lib/types";

/** The profile's Organizations section (`#organizations`): cloud orgs, or a local box's link. */
export function OrganizationsSection({ user }: { user: User }) {
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  return (
    <section id="organizations" aria-label="Organizations" className="rounded-[14px] border border-line-2 bg-surface-2 p-5">
      <h2 className="mb-3 font-mono text-[10px] uppercase tracking-wide text-faint">Organizations</h2>
      {hosted ? <CloudOrgs user={user} /> : <LocalOrgs />}
    </section>
  );
}

function CloudOrgs({ user }: { user: User }) {
  const { data: orgs = [], isLoading } = useOrgs();
  const { data: invites = [] } = useMyInvites();
  if (isLoading) return <p className="text-body text-muted">Loading…</p>;
  return (
    <div className="space-y-4">
      {invites.length > 0 && (
        <div>
          <h3 className="mb-2 text-[12px] font-medium text-muted">Invitations</h3>
          <ul className="divide-y divide-line-2" aria-label="Invitations">
            {invites.map((i) => (
              <InviteRow key={i.id} invite={i} />
            ))}
          </ul>
        </div>
      )}
      {orgs.length === 0 ? (
        <p className="text-body text-muted">You aren't in any organization yet.</p>
      ) : (
        <ul className="divide-y divide-line-2" aria-label="Your organizations">
          {orgs.map((o) => (
            <OrgRow key={o.id} org={o} accountHandle={user.handle} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** One pending invitation: Accept (with the handle-clash field when needed) or Decline. */
function InviteRow({ invite }: { invite: MyInvite }) {
  const qc = useQueryClient();
  const decline = useDeclineInvite();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [clash, setClash] = React.useState<HandleClash | null>(null);

  async function accept(handle?: string) {
    setBusy(true);
    setError("");
    try {
      const joined = await api.acceptInvite(invite.token, handle);
      qc.setQueryData<Org[]>(keys.orgs, (prev) => [...(prev ?? []).filter((o) => o.id !== joined.id), joined]);
      qc.setQueryData<MyInvite[]>(keys.myInvites, (prev) => (prev ?? []).filter((i) => i.id !== invite.id));
      await Promise.all([
        qc.invalidateQueries({ queryKey: keys.orgs }),
        qc.invalidateQueries({ queryKey: keys.myInvites }),
      ]);
    } catch (err) {
      setBusy(false);
      // On the retry, OrgHandleField shows the error: the next suggestion, or why it failed.
      if (handle) throw err;
      const c = handleClash(err);
      if (c) setClash(c);
      else setError(errorDetail(err, "could not accept the invitation"));
    }
  }

  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-fg-2">{invite.org_name}</span>
        <span className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">
          {invite.role}
        </span>
        {invite.invited_by && <span className="text-[12px] text-muted">from {invite.invited_by}</span>}
        {!clash && (
          <div className="ml-auto flex gap-2">
            <Button size="sm" disabled={busy || decline.isPending} onClick={() => void accept()}>
              {busy ? "Joining…" : "Accept"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || decline.isPending}
              onClick={() =>
                decline.mutate(invite.id, { onError: (e) => setError(errorDetail(e, "could not decline")) })
              }
            >
              Decline
            </Button>
          </div>
        )}
      </div>
      {error && (
        <p role="status" className="mt-1 text-[12px] text-st-blocked">
          {error}
        </p>
      )}
      {clash && (
        <div className="mt-2">
          <p className="mb-2 text-[12px] text-muted">
            Your handle is already taken in <strong>{clash.orgName || invite.org_name}</strong>. Pick the handle
            you'll use there — it stays your own everywhere else.
          </p>
          <OrgHandleField
            orgName={clash.orgName || invite.org_name}
            initial={clash.suggested}
            submitLabel="Accept with this handle"
            onSubmit={(h) => accept(h)}
            onCancel={() => setClash(null)}
          />
        </div>
      )}
    </li>
  );
}

/** Leave, disabled (with the reason inline) when you are the org's only owner. */
function LeaveButton({ org }: { org: Org }) {
  const { data: members } = useOrgMembers(org.role === "owner" ? org.id : undefined);
  const leave = useLeaveOrg();
  const [confirming, setConfirming] = React.useState(false);
  const [error, setError] = React.useState("");
  const onlyOwner = org.role === "owner" && members !== undefined && members.filter((m) => m.role === "owner").length <= 1;

  if (onlyOwner) {
    return (
      <span className="flex items-center gap-2">
        <Button size="sm" variant="outline" disabled>
          Leave
        </Button>
        <span className="text-[12px] text-muted">You're the only owner — make someone else an owner first.</span>
      </span>
    );
  }
  if (confirming) {
    return (
      <span className="flex items-center gap-2">
        <span className="text-[12px] text-muted">Leave {org.name}?</span>
        <Button
          size="sm"
          variant="danger"
          disabled={leave.isPending}
          onClick={() =>
            leave.mutate(org.id, {
              onError: (e) => {
                setError(errorDetail(e, "could not leave"));
                setConfirming(false);
              },
            })
          }
        >
          {leave.isPending ? "Leaving…" : "Confirm leave"}
        </Button>
        <Button size="sm" variant="outline" disabled={leave.isPending} onClick={() => setConfirming(false)}>
          Cancel
        </Button>
      </span>
    );
  }
  return (
    <span className="flex items-center gap-2">
      {error && (
        <span role="status" className="text-[12px] text-st-blocked">
          {error}
        </span>
      )}
      <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
        Leave
      </Button>
    </span>
  );
}

function OrgRow({ org, accountHandle }: { org: Org; accountHandle: string }) {
  const [editing, setEditing] = React.useState(false);
  const setHandle = useSetOrgHandle();
  const qc = useQueryClient();
  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-body font-medium text-fg-2">{org.name}</span>
        <span className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">
          {org.role}
        </span>
        <div className="ml-auto flex gap-2">
          <Link to="/organization" className={buttonVariants({ size: "sm", variant: "outline" })}>Open</Link>
          <LeaveButton org={org} />
        </div>
      </div>
      {org.handle && (
        <div className="mt-1 flex items-center gap-2 text-[12px] text-muted">
          <span>
            you're <span className="font-mono">@{org.handle}</span> here
          </span>
          {!editing && (
            <button type="button" className="text-accent hover:underline" onClick={() => setEditing(true)}>
              Change handle
            </button>
          )}
        </div>
      )}
      {!org.handle && !editing && (
        <button type="button" className="mt-1 text-[12px] text-accent hover:underline" onClick={() => setEditing(true)}>
          Use a different handle here
        </button>
      )}
      {editing && (
        <div className="mt-2">
          <OrgHandleField
            orgName={org.name}
            initial={org.handle ?? accountHandle}
            submitLabel="Change handle"
            onSubmit={async (h) => {
              await setHandle.mutateAsync({ orgId: org.id, handle: h });
              await qc.invalidateQueries({ queryKey: ["orgs"] });
              setEditing(false);
            }}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </li>
  );
}

function LocalOrgs() {
  const { data: sync } = useSyncStatus();
  if (!sync) return <p className="text-body text-muted">Loading…</p>;
  if (!sync.linked) {
    return (
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-body text-muted">This box isn't linked to an organization.</p>
        <Link to="/settings/deployment/sync" className={buttonVariants({ size: "sm" })}>Link this box</Link>
      </div>
    );
  }
  return (
    <div className="flex items-baseline gap-3 text-body">
      <span className="text-fg-2">{sync.org || "Linked organization"}</span>
      <span className="font-mono text-[12px] text-muted">{sync.cloud_url}</span>
    </div>
  );
}
