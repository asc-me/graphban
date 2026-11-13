import { ArrowUpRight, Cloud } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { FETCH_FAILED } from "@/components/planner/PlannerStates";
import { RefusalRuleLink } from "@/features/activity/RefusalRuleLink";
import { clockTime, LinkedPanel, LinkedSummary } from "@/features/organization/LinkedPanel";
import { LinkToOrgWizard, useLinkFlow } from "@/features/organization/LinkToOrgWizard";
import { cn } from "@/lib/cn";
import { errorDetail, errorRuleId, isRefusal } from "@/lib/errors";
import { useSyncOrg, useSyncStatus } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import { settingsPath } from "@/lib/routes";
import type { SyncOrgDeployment, SyncStatus } from "@/lib/types";

const OPEN_EVENT = "graphban:open-org";

/** Open the Organization window from anywhere (the tour's Link to an org →). The shell owns it. */
export function requestOrgWindow() {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

/** Subscribe the shell to {@link requestOrgWindow}. */
export function onOrgWindowRequested(fn: () => void): () => void {
  window.addEventListener(OPEN_EVENT, fn);
  return () => window.removeEventListener(OPEN_EVENT, fn);
}

/** The design's intro line (`Org Link.dc.html`, L1). */
export const ORG_INTRO =
  "Link this deployment to an org on Graphban Cloud. The org's people, roles and rule decisions arrive here. Your code, agent memory and coding-agent credentials stay on this deployment.";

/** What the box sends and receives (`Org Link.dc.html`, the linked window). */
const SENDS = ["Project names", "Member list", "Usage counts"];
const RECEIVES = ["Org people", "Roles", "Org rule decisions · read-only in Rules"];

const ACTION = "inline-flex h-[34px] flex-none items-center gap-2 whitespace-nowrap rounded-[9px] text-body transition-colors";
const PRIMARY = "bg-accent px-4 font-semibold text-bg hover:bg-accent/90";

/**
 * The Organization window (GRPH-P67 §5, design `Org Link.dc.html` L1–L4; GRPH-1564). Not
 * linked, its whole body is the Link to an org wizard, opened at the first unfinished step.
 * Linked, it is the overview: who the box is signed in as, what syncs, SENDS / RECEIVES and
 * the org's deployments. Link and push state come from `GET /api/sync/status` — the same read
 * Settings → Sync / Link uses — so the two can never disagree. `/api/sync/org` only fills the
 * deployments list; its failure greys that list and says nothing about the link.
 */
export function CloudOrgLinkDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100vh-48px)] w-[min(720px,calc(100%-48px))] max-w-none flex-col overflow-hidden p-0">
        {open && <OrgWindowBody onClose={() => onOpenChange(false)} />}
      </DialogContent>
    </Dialog>
  );
}

function OrgWindowBody({ onClose }: { onClose: () => void }) {
  const statusQ = useSyncStatus();
  const status = statusQ.data;
  const flow = useLinkFlow(status);
  const linkedView = !!status && status.linked && !flow.wizard;
  // Design 2e: a finished link opens on the "Linked as" summary — after Done on ④ and on
  // reopening — and Open org overview leads on. Any wizard visit resets it.
  const [overview, setOverview] = React.useState(false);
  const summaryView = linkedView && !overview;
  const orgQ = useSyncOrg(summaryView);
  const deploymentName = orgQ.data?.linked ? (orgQ.data.deployments ?? []).find((d) => d.here)?.name : null;
  const title = linkedView ? `Linked to ${status!.org || "your org"}` : "Link to an org";
  const role = status?.can_link === true ? "admin" : status?.can_link === false ? "member" : null;

  return (
    <>
      <Header
        title={title}
        role={role}
        // Org Link L4: the linked view leads with "This deployment is linked to <org> as
        // <email>." (the LinkedPanel's first line), never the not-linked intro.
        intro={linkedView ? null : ORG_INTRO}
        description={
          linkedView
            ? `This deployment is linked to ${status!.org || "your org"}${status!.linked_email ? ` as ${status!.linked_email}` : ""}.`
            : ORG_INTRO
        }
      />
      <div className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-[22px] pb-5 pt-1">
        {!status ? (
          statusQ.isError ? (
            <div role="alert" className="flex flex-wrap items-center gap-2.5 text-body text-muted">
              <span className="flex-1">
                {FETCH_FAILED} Whether this deployment is linked was not read — this is not an unlinked deployment.
              </span>
              <button
                type="button"
                onClick={() => void statusQ.refetch()}
                className={cn(ACTION, "border border-control bg-surface-2 px-3.5 text-fg-2 hover:text-fg")}
              >
                {statusQ.isFetching ? "Retrying…" : "Retry now"}
              </button>
            </div>
          ) : (
            <p className="text-body text-faint">Loading…</p>
          )
        ) : flow.wizard || !status.linked ? (
          <LinkToOrgWizard
            status={status}
            initialStep={flow.initialStep}
            readOnly={flow.readOnly}
            onLinked={flow.onLinked}
            onDone={() => {
              setOverview(false);
              flow.onDone();
            }}
          />
        ) : summaryView ? (
          <LinkedSummary
            status={status}
            deploymentName={deploymentName}
            readOnly={flow.readOnly}
            busy={flow.busy}
            onOpenOverview={() => setOverview(true)}
            onChooseProjects={() => {
              setOverview(false);
              flow.openAt(3);
            }}
            onSignIn={() => {
              setOverview(false);
              flow.openAt(2);
            }}
          />
        ) : (
          <LinkedOverview status={status} flow={flow} onClose={onClose} />
        )}
      </div>
    </>
  );
}

function Header({
  title,
  role,
  intro,
  description,
}: {
  title: string;
  role: string | null;
  intro: string | null;
  description: string;
}) {
  return (
    <div className="flex flex-none items-start gap-3.5 px-[22px] pb-4 pt-5 pr-12">
      <span className="flex size-[34px] flex-none items-center justify-center rounded-[9px] border border-[rgba(167,139,250,.35)] bg-[rgba(167,139,250,.1)] text-[#c9b8ff]">
        <Cloud size={16} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <span className="flex items-center gap-2 font-mono text-meta text-faint">
          ORGANIZATION · GRAPHBAN CLOUD
          {role && (
            <span data-role-chip className="rounded-[4px] border border-line px-1.5 py-px uppercase text-fg-2">
              ROLE · {role}
            </span>
          )}
        </span>
        <DialogTitle className="mt-1 text-[17px] tracking-[-.01em]">{title}</DialogTitle>
        {intro ? (
          <DialogDescription className="mt-1.5 max-w-[62ch] text-body">{intro}</DialogDescription>
        ) : (
          <DialogDescription className="sr-only">{description}</DialogDescription>
        )}
      </div>
    </div>
  );
}

/** The org's host label: the server sends the `<slug>`, or a full host once one is claimed. */
function orgHost(host: string | null | undefined): string | null {
  const h = host?.trim();
  if (!h) return null;
  return h.includes(".") ? h : `${h}.graphban.dev`;
}

/** A sync this recent reads green; older is plain. */
const RECENT_MS = 10 * 60 * 1000;

function isRecent(iso: string): boolean {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return Date.now() - new Date(zoned ? iso : `${iso}Z`).getTime() < RECENT_MS;
}

/** The newest push this box made, for the UNREACHABLE line. */
function lastPushAt(status: SyncStatus): string | null {
  const times = status.projects.map((p) => p.last_push_at).filter((t): t is string => !!t);
  return times.length ? times.sort().at(-1)! : null;
}

function LinkedOverview({
  status,
  flow,
  onClose,
}: {
  status: SyncStatus;
  flow: ReturnType<typeof useLinkFlow>;
  onClose: () => void;
}) {
  // Only the deployments list comes from /sync/org; the link's own state is status.state.
  const orgQ = useSyncOrg(true);
  const o = orgQ.data?.linked ? orgQ.data : null;
  const orgName = status.org || o?.org?.name || "your org";
  const unreachable = status.state === "unknown";
  const since = lastPushAt(status);
  const host = orgHost(o?.org?.host);
  const cloudHref = host ? `https://${host}` : null;
  const orgUnread = !o || o.reachable === false;
  const [unlinkErr, setUnlinkErr] = React.useState<unknown>(null);

  async function unlink() {
    setUnlinkErr(null);
    try {
      await flow.unlink();
      onClose();
    } catch (e) {
      setUnlinkErr(e);
    }
  }

  return (
    <>
      {unreachable && (
        <div
          role="status"
          data-org-unreachable
          className="flex flex-wrap gap-2.5 rounded-[10px] border-[1.5px] border-dashed border-[rgba(124,162,255,.6)] px-3.5 py-[11px] text-body leading-normal"
        >
          <span className="font-mono text-meta text-[#7ca2ff]">⌁ UNREACHABLE</span>
          <span className="text-fg-2">
            {since
              ? `Org rules last synced ${clockTime(since)}; they may be out of date until the link is back.`
              : "Org rules may be out of date until the link is back."}
          </span>
        </div>
      )}

      <LinkedPanel
        status={status}
        readOnly={flow.readOnly}
        unlinkable={status.source !== "env"}
        busy={flow.busy}
        confirmUnlink
        onChooseProjects={() => flow.openAt(3)}
        onSignIn={() => flow.openAt(2)}
        onUnlink={() => void unlink()}
      />
      {!!unlinkErr && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-[12px] text-st-blocked">
          <span>{errorDetail(unlinkErr, "Could not unlink")}</span>
          {isRefusal(unlinkErr) && <RefusalRuleLink ruleId={errorRuleId(unlinkErr)} />}
        </div>
      )}

      <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <FlowList id="org-sends" label={`↑ SENDS TO ${orgName}`} items={SENDS} />
        <FlowList id="org-receives" label={`↓ RECEIVES FROM ${orgName}`} items={RECEIVES} />
      </div>

      <div id="org-deployments" className="flex flex-col overflow-hidden rounded-xl border border-line-2">
        <span id="org-deployments-label" className="border-b border-line-2 bg-surface px-3.5 py-[9px] font-mono text-meta text-faint">
          {`DEPLOYMENTS IN ${orgName.toUpperCase()}`}
        </span>
        <ul aria-labelledby="org-deployments-label">
          {(o?.deployments ?? []).map((d, i) => (
            <DeploymentRow key={`${d.name}-${i}`} d={d} unread={orgUnread} />
          ))}
          {(o?.deployments ?? []).length === 0 && (
            <li className="px-3.5 py-2.5 text-body text-faint">
              {orgUnread ? "— Couldn't read the org's deployments." : "No deployments reported yet."}
            </li>
          )}
        </ul>
      </div>

      <div className="flex flex-wrap items-center gap-2.5 border-t border-line-2 pt-3.5">
        <Link to={settingsPath("deployment/sync")} onClick={onClose} className="whitespace-nowrap text-[12px] text-muted transition-colors hover:text-fg">
          Sync link settings
        </Link>
        <span className="flex-1" />
        {cloudHref && (
          <a href={cloudHref} target="_blank" rel="noopener noreferrer" className={cn(ACTION, PRIMARY)}>
            Open in Graphban Cloud
            <ArrowUpRight size={12} strokeWidth={2.2} aria-hidden />
          </a>
        )}
      </div>
    </>
  );
}

function FlowList({ id, label, items }: { id: string; label: string; items: string[] }) {
  return (
    <div className="rounded-xl border border-line-2 bg-surface px-3.5 py-2.5">
      <span id={id} className="font-mono text-meta text-faint">{label}</span>
      <ul aria-labelledby={id} className="mt-1.5 space-y-0.5">
        {items.map((s) => (
          <li key={s} className="text-body text-fg-2">{s}</li>
        ))}
      </ul>
    </div>
  );
}

/** Unread (the org read failed or the cloud is down) greys every figure — never amber, and
 *  never the last numbers shown as current. */
function DeploymentRow({ d, unread }: { d: SyncOrgDeployment; unread: boolean }) {
  let sync: string;
  let tone: string;
  if (unread) {
    sync = d.here && d.last_sync_at ? `last sync ${clockTime(d.last_sync_at)}` : "—";
    tone = "text-faint";
  } else if (d.last_sync_at) {
    sync = `synced ${relTime(d.last_sync_at)}`;
    tone = isRecent(d.last_sync_at) ? "text-st-done" : "text-muted";
  } else {
    sync = "never synced";
    tone = "text-faint";
  }
  const projects = unread || d.projects === null ? "—" : `${d.projects} ${d.projects === 1 ? "project" : "projects"}`;
  return (
    <li data-deployment className="flex flex-wrap items-center gap-2.5 border-t border-line-2 px-3.5 py-2.5 text-body first:border-t-0">
      <span className="font-mono text-fg">{d.name}</span>
      {d.here && (
        <span className="rounded-[4px] border border-[rgba(198,242,78,.35)] px-[5px] py-px font-mono text-[9px] tracking-[.5px] text-accent">
          THIS ONE
        </span>
      )}
      <span className="flex-1" />
      <span data-projects className="text-muted">{projects}</span>
      <span data-sync className={cn("font-mono text-small", tone)}>{sync}</span>
    </li>
  );
}
