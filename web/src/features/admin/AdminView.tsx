import { ArrowLeft, ShieldCheck } from "lucide-react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import {
  useAdminInvites,
  useAdminOrgRequests,
  useAdminOrgs,
  useAdminUsers,
  useIsPlatformAdmin,
} from "@/lib/queries";

/**
 * The operator plane (PRD-21, screens 19–22) — the platform-admin console.
 *
 * Two things about it are product decisions rather than styling:
 *
 * 1. **It looks different on purpose.** The tenant app is warm/lime; this plane is
 *    blue-black with a blue accent and a mono OPERATOR badge, because the one surface
 *    in the product that reads across tenants must never be mistaken for one that
 *    doesn't. That is why it renders its own shell instead of the tenant `AppFrame` —
 *    which also means an operator with no org or project of their own can still reach
 *    it, where before the onboarding gates stood in the way.
 * 2. **It is almost entirely read-only, and says so.** Assigning a plan, issuing or
 *    revoking a platform invite, and deciding an additional-org request are the whole
 *    set of writes. No suspend, no impersonation, no password reset, no tenant content
 *    — the boundary that keeps the cross-tenant isolation guarantee honest.
 */
export function AdminView() {
  const { data: admin, isLoading, isError } = useIsPlatformAdmin();
  const ok = !!admin?.is_platform_admin;

  if (isLoading) {
    return <div className="p-8 font-mono text-[12px] text-faint">loading…</div>;
  }
  // A tenant gets a 404 from every route on this plane, so the view says nothing about
  // what it would have shown — the console's existence is not disclosed.
  if (isError || !ok) {
    return <div className="p-8 text-[13px] text-muted">This area is not available.</div>;
  }

  return (
    <div className="flex h-full min-h-0 bg-op-bg text-[13px] text-op-fg">
      <OperatorRail email={admin.email} />
      <div className="flex min-w-0 flex-1 flex-col">
        <RouteStamp />
        <div className="min-h-0 flex-1 overflow-auto">
          <Outlet />
        </div>
      </div>
    </div>
  );
}

// Home carries no badge: a count next to "Platform" would be a number with no referent,
// and the tiles on the page are already the count of everything.
const NAV = [
  { to: "/admin", end: true, label: "Platform", badge: null },
  { to: "/admin/orgs", end: false, label: "Orgs", badge: "orgs" },
  { to: "/admin/users", end: false, label: "Users", badge: "users" },
  { to: "/admin/licensing", end: false, label: "Licensing", badge: "licensing" },
] as const;

function OperatorRail({ email }: { email: string }) {
  const { data: orgs = [] } = useAdminOrgs();
  const { data: users = [] } = useAdminUsers();
  const { data: invites = [] } = useAdminInvites();
  const { data: requests = [] } = useAdminOrgRequests();

  // Licensing's badge counts what is *waiting on the operator*: outstanding invites
  // plus undecided org requests. A count of issued-ever would be a number nobody acts on.
  const badges: Record<string, number> = {
    orgs: orgs.length,
    users: users.length,
    licensing: invites.length + requests.length,
  };

  return (
    <nav className="flex w-[208px] shrink-0 flex-col border-r border-op-line bg-op-rail">
      <div className="border-b border-op-line-2 p-3">
        <div className="flex items-center gap-2.5 rounded-[9px] border border-st-next/30 bg-st-next/[0.07] px-2.5 py-1.5">
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-st-next/35 bg-st-next/[0.16]">
            <ShieldCheck size={11} className="text-st-next" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block font-mono text-[10px] tracking-[0.1em] text-st-next">OPERATOR</span>
            <span className="mt-0.5 block font-mono text-[9px] text-op-faint">PLATFORM PLANE</span>
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-2.5">
        <div className="px-2 pb-2 font-mono text-[9.5px] tracking-[0.09em] text-op-faint-2">
          CROSS-TENANT
        </div>
        <div className="flex flex-col gap-px">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-[7px] px-2.5 py-1.5 text-[12.5px] ${
                  isActive
                    ? "bg-[#131922] font-semibold text-op-fg"
                    : "font-medium text-op-muted-2 hover:bg-[#11151c]"
                }`
              }
            >
              {({ isActive }) => (
                <>
                  <span
                    className={`h-[5px] w-[5px] shrink-0 rounded-full ${
                      isActive ? "bg-st-next" : "bg-transparent"
                    }`}
                  />
                  <span className="min-w-0 flex-1">{n.label}</span>
                  {n.badge && (
                    <span className="font-mono text-[9px] text-op-faint-3">{badges[n.badge]}</span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </div>

        <div className="mt-4 rounded-[10px] border border-op-line-2 bg-op-inset px-3 py-2.5">
          <div className="font-mono text-[9px] tracking-[0.06em] text-op-faint-2">
            WHAT THIS PLANE CAN DO
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-op-muted-2">
            Read every tenant. Assign a plan. Issue a platform invite. Decide an
            additional-org request. Nothing else — no impersonation, no data edits, no
            password resets.
          </p>
        </div>
      </div>

      <div className="border-t border-op-line-2 px-4 py-3">
        <Link
          to="/tracker"
          className="flex items-center gap-1.5 font-mono text-[10px] text-op-faint hover:text-st-next"
        >
          <ArrowLeft size={11} /> back to workspace
        </Link>
        <div className="mt-2 truncate font-mono text-[9px] leading-relaxed text-op-faint-3">
          signed in as {email}
        </div>
      </div>
    </nav>
  );
}

/** The path, stated. Cheap, and it keeps "which plane am I on" answerable at a glance. */
function RouteStamp() {
  const { pathname } = useLocation();
  return (
    <div className="flex shrink-0 items-center gap-3 border-b border-op-line-2 bg-op-rail px-5 py-2">
      <span className="font-mono text-[9.5px] tracking-[0.08em] text-op-faint-2">
        CROSS-TENANT · METADATA ONLY
      </span>
      <div className="flex-1" />
      <span className="font-mono text-[9.5px] text-op-faint-3">{pathname}</span>
    </div>
  );
}
