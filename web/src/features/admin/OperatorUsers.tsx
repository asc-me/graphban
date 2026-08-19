import { Search } from "lucide-react";
import * as React from "react";

import { useAdminUsers } from "@/lib/queries";
import type { AdminUser } from "@/lib/types";

import { Avatar, Empty, PageHead, ROLE_TONE, Table, Th, relTime, tintFor } from "./parts";

/**
 * Screen 21 — everyone across every tenant. Read-only, and it says so in the header.
 *
 * The list names the orgs rather than counting them: a support lookup is "which tenants
 * is this person in", and a bare number answers a question nobody asked. The other
 * deliberate detail is the last column — see {@link LastWrite}.
 */
export function OperatorUsers() {
  const { data: users = [], isLoading } = useAdminUsers();
  const [query, setQuery] = React.useState("");

  const q = query.trim().toLowerCase();
  const rows = q
    ? users.filter((u) => `${u.name} ${u.handle} ${u.email}`.toLowerCase().includes(q))
    : users;

  return (
    <div className="max-w-[1180px] px-5 pb-16 pt-6">
      <PageHead
        title="Users"
        chip={
          <span className="rounded-full border border-op-line px-2 py-0.5 font-mono text-[9.5px] tracking-[0.06em] text-op-muted-2">
            READ-ONLY
          </span>
        }
        lede="Everyone across every tenant. Search and inspect only — no disable, no reset, no impersonation. Account actions belong to the org's own admins, and support requests route through them."
        right={
          <div className="flex h-[30px] items-center gap-2 rounded-lg border border-op-line bg-op-inset px-3">
            <Search size={12} className="shrink-0 text-op-faint-2" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="name, handle, or email…"
              aria-label="Search users"
              className="w-[190px] bg-transparent font-mono text-[11.5px] text-op-fg outline-none placeholder:text-op-faint-3"
            />
          </div>
        }
      />

      {isLoading ? (
        <div className="rounded-[13px] border border-op-line bg-op-card px-5 py-8 text-center font-mono text-[11px] text-op-faint-2">
          loading…
        </div>
      ) : rows.length === 0 && q ? (
        <Empty
          title={`No user matches “${query}”`}
          body="Nobody on this deployment has that name, handle, or email. A person with no org memberships would still appear here — so this really is nobody."
        />
      ) : rows.length === 0 ? (
        <Empty
          title="No accounts yet"
          body="Nobody has registered on this deployment. An account appears the moment someone signs up, whether or not they ever join an org."
        />
      ) : (
        <>
          <Table minWidth={860}>
            <div className="flex items-center gap-3 border-b border-op-line-2 bg-op-inset px-3.5 py-2.5 text-op-faint-2">
              <Th className="w-[188px] shrink-0">USER</Th>
              <Th className="w-[180px] shrink-0">EMAIL</Th>
              <Th className="min-w-0 flex-1">ORG MEMBERSHIPS</Th>
              <Th className="w-[110px] shrink-0 text-right">LAST WRITE</Th>
            </div>
            {rows.map((u) => (
              <UserRow key={u.id} user={u} />
            ))}
          </Table>
          <p className="mt-2.5 px-1 text-[11px] leading-relaxed text-op-faint">
            {q
              ? `${rows.length} ${rows.length === 1 ? "user matches" : "users match"} “${query}”.`
              : "A user can belong to several orgs. Membership is per-org — there is no platform-wide role."}
          </p>
        </>
      )}
    </div>
  );
}

function UserRow({ user }: { user: AdminUser }) {
  return (
    <div className="flex items-center gap-3 border-b border-op-line-3 px-3.5 py-2.5 hover:bg-[#0e1218]">
      <span className="flex w-[188px] min-w-0 shrink-0 items-center gap-2.5">
        <Avatar name={user.name} handle={user.handle} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] text-op-fg">{user.name}</span>
          <span className="mt-px block font-mono text-[10px] text-op-faint">@{user.handle}</span>
        </span>
      </span>
      <span className="w-[180px] min-w-0 shrink-0 truncate font-mono text-[10.5px] text-op-muted-2">
        {user.email}
      </span>
      <span className="flex min-w-0 flex-1 flex-wrap gap-1.5">
        {user.orgs.length === 0 ? (
          <span className="font-mono text-[10px] text-op-faint-3">no org memberships</span>
        ) : (
          user.orgs.map((o) => (
            <span
              key={o.id}
              className="inline-flex items-center gap-1.5 rounded-[5px] border border-op-line bg-op-inset px-1.5 py-0.5"
            >
              <span className="h-[5px] w-[5px] shrink-0 rounded-sm" style={{ background: tintFor(o.id) }} />
              <span className="font-mono text-[10px] text-op-fg-2">{o.name}</span>
              <span
                className={`font-mono text-[8.5px] uppercase tracking-[0.04em] ${
                  ROLE_TONE[o.role] ?? "text-op-muted-2"
                }`}
              >
                {o.role}
              </span>
            </span>
          ))
        )}
      </span>
      <span className="w-[110px] shrink-0 text-right">
        <LastWrite at={user.last_write_at} />
      </span>
    </div>
  );
}

/**
 * The last recorded *write*, not "last active".
 *
 * Reads are never evented, so a user who signs in daily and reads has no row at all —
 * identical, in the data, to one who has never come back. Labelling the column "last
 * active" and printing a dash would turn that unknown into a confident claim of
 * inactivity, which is exactly the reading this product refuses to let an absence get.
 */
function LastWrite({ at }: { at: string | null }) {
  const rel = relTime(at);
  if (rel) return <span className="font-mono text-[11px] text-op-muted-2">{rel}</span>;
  return (
    <span
      className="font-mono text-[10px] text-op-faint-3"
      title="No write on record. Reads aren't logged, so this is not evidence of inactivity."
    >
      no writes
    </span>
  );
}
