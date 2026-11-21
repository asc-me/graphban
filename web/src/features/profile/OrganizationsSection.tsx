import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { buttonVariants } from "@/components/ui/button";
import { OrgHandleField } from "@/features/profile/OrgHandleField";
import { useConfig, useOrgs, useSetOrgHandle, useSyncStatus } from "@/lib/queries";
import type { Org, User } from "@/lib/types";

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
  if (isLoading) return <p className="text-body text-muted">Loading…</p>;
  if (orgs.length === 0) return <p className="text-body text-muted">You aren't in any organization yet.</p>;
  return (
    <ul className="divide-y divide-line-2">
      {orgs.map((o) => (
        <OrgRow key={o.id} org={o} accountHandle={user.handle} />
      ))}
    </ul>
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
