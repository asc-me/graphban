import { CopyLine } from "@/features/onboarding/QuickStartDialog";
import { useOrgs, useProjects } from "@/lib/queries";

/**
 * The invitee's optional "Run Graphban locally too?" card (GRPH-P67 §4 D10). Collapsed, outside
 * Finish's checks, no status of its own. The local box signs in as the invitee, so there is no key
 * here; the card only names the command and lists the projects their seat can reach.
 */
export function InviteeLinkCard() {
  const orgs = useOrgs(true);
  const projects = useProjects();
  const org = (orgs.data ?? []).find((o) => o.role !== "owner")?.name ?? "the org";
  const cmd = `graphban link --cloud-url ${window.location.origin}`;
  return (
    <details className="mt-4 rounded-[10px] border border-line bg-surface-2 px-3 py-2" data-link-setup>
      <summary className="cursor-pointer text-body font-medium">Run Graphban locally too? (optional)</summary>
      <div className="mt-2 flex flex-col gap-2 text-small text-muted">
        <p>
          On your local Graphban, open <strong>Organization → Link to an org</strong> and sign in as yourself. Your
          seat in {org} is what the link uses.
        </p>
        <CopyLine text={cmd} label="Copy the link command" />
        <p>Prints a code to approve here, signed in as you.</p>
        <p>Projects you can sync in {org}:</p>
        {projects.isError ? (
          <p className="text-muted">Couldn&apos;t load your projects.</p>
        ) : projects.data ? (
          <ul className="flex flex-col gap-0.5">
            {projects.data.map((p) => (
              <li key={p.id} className="font-mono text-[12px] text-fg">
                {p.tag} — {p.name}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </details>
  );
}
