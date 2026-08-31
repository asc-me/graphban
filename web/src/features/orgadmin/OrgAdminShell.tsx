import { NavLink, Outlet } from "react-router-dom";

import { useBilling, useOrgMembers, useOrgs } from "@/lib/queries";
import { adminPath } from "@/lib/routes";

/**
 * Screen A — the org-administration section (PRD-21 rev2).
 *
 * Admin is a *section*, not a page: four screens under one header, and drilling into one
 * never leaves it. It hangs off the org base rather than a project, because everything
 * here is org-scoped — and off `/org/admin` rather than `/admin`, which belongs to the
 * cross-tenant operator plane. Two surfaces called "admin" that mean different
 * populations should not share a path.
 *
 * Visibility is enforced by the rail, which renders no admin group for a `member`. This
 * shell states the org's identity so an admin acting across several orgs can always see
 * which one they are changing.
 */
const TABS = [
  { to: "users", label: "Users & access", backed: true },
  { to: "teams", label: "Teams", backed: true },
  { to: "deployments", label: "Deployments", backed: true },
  { to: "branding", label: "Branding", backed: false },
  { to: "integrations", label: "Integrations", backed: true },
  { to: "gitops", label: "Gitops", backed: true },
  { to: "billing", label: "Billing", backed: true },
];

export function OrgAdminShell() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: members = [] } = useOrgMembers(org?.id);
  const { data: billing } = useBilling(org?.id);

  const seats = billing?.usage.seats ?? 0;
  const limit = billing?.limits.max_seats ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="max-w-[1180px] px-6 pt-6">
        <div className="font-mono text-[9.5px] uppercase tracking-[0.09em] text-faint-2">ADMIN</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2.5">
          <h1 className="text-[19px] font-semibold tracking-[-0.3px]">{org?.name ?? "—"}</h1>
          {org && (
            <span className="rounded-full border border-accent/30 bg-accent/[0.07] px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.05em] text-accent">
              {org.plan} plan
            </span>
          )}
          <span className="font-mono text-[10px] text-faint">
            {members.length} member{members.length === 1 ? "" : "s"}
            {limit > 0 && ` · ${seats} / ${limit} seats`}
          </span>
        </div>
        <p className="mt-2 max-w-[76ch] text-[12.5px] leading-relaxed text-muted">
          Everything here applies to this organization. Project settings live on the project.
        </p>

        <div className="mt-4 flex items-center gap-0.5 border-b border-line">
          {TABS.map((t) => (
            <NavLink
              key={t.to}
              to={adminPath(t.to)}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3.5 py-2.5 text-[13px] ${
                  isActive
                    ? "font-semibold text-fg shadow-[inset_0_-2px_0_0_var(--color-accent)]"
                    : "font-medium text-muted hover:text-fg"
                }`
              }
            >
              {t.label}
              {!t.backed && (
                <span className="rounded border border-purple/30 px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.05em] text-purple">
                  not backed
                </span>
              )}
            </NavLink>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <Outlet />
      </div>
    </div>
  );
}
