import { ArrowUpRight, Cloud, Globe, MessageSquare, ShieldCheck, SquareCheckBig, Users } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { RefusalRuleLink } from "@/features/activity/RefusalRuleLink";
import { noteGitopsUnlinked } from "@/features/settings/GitopsPanel";
import { cn } from "@/lib/cn";
import { errorDetail, errorRuleId, isRefusal } from "@/lib/errors";
import { useSyncOrg, useSyncUnlink } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import { ORG_SIGNUP_URL, settingsPath } from "@/lib/routes";
import type { SyncOrgDeployment, SyncOrgOverview } from "@/lib/types";

/** `Org Link.dc.html`, intro view. "Feedback on your own domain" spans the full width. */
const BENEFITS: { title: string; body: string; icon: React.ReactNode; wide?: boolean }[] = [
  {
    title: "People and teams in one place",
    body: "Invite once and use the same members and teams on every linked deployment.",
    icon: <Users size={14} />,
  },
  {
    title: "Roles and access (RBAC)",
    body: "Owner, admin, member and viewer roles, scoped to the org or a single project. Agents and API keys get roles too.",
    icon: <ShieldCheck size={14} />,
  },
  {
    title: "Policy rules for every project",
    body: "Set rules once at org level and every project inherits them in Rules, read-only. Projects can add their own on top.",
    icon: <SquareCheckBig size={14} />,
  },
  {
    title: "Galaxy",
    body: "See every project, agent and plan across deployments in a single map.",
    icon: <Globe size={14} />,
  },
  {
    title: "Feedback on your own domain",
    body: "Relay Feedback Kit through the org so reports land in the right project from anywhere.",
    icon: <MessageSquare size={14} />,
    wide: true,
  },
];

const STAYS = ["Code and repos", "Memory shards", "Item content"];

const ACTION = "inline-flex h-[34px] flex-none items-center gap-2 whitespace-nowrap rounded-[9px] text-body transition-colors";

/**
 * The Organization window. Unlinked it is the explainer (`Org Link - Hi-fi.dc.html` frame 2a,
 * GRPH-1224): what an org adds, what stays on this box, and the two ways in — both Coming soon
 * links to the waitlist, so nothing here calls `POST /api/sync/link`. On a linked box it is the
 * org overview (frames 3a/3b, GRPH-1225), read from `GET /api/sync/org`.
 */
export function CloudOrgLinkDialog({
  open,
  onOpenChange,
  linked = false,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** The box has a usable cloud link — open on the overview, not the explainer. */
  linked?: boolean;
}) {
  const org = useSyncOrg(open && linked);
  const overview = linked && org.data?.linked ? org.data : null;
  const close = () => onOpenChange(false);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100vh-48px)] w-[min(720px,calc(100%-48px))] max-w-none flex-col overflow-hidden p-0">
        {overview ? (
          <OrgOverviewView
            o={overview}
            retrying={org.isFetching}
            onRetry={() => void org.refetch()}
            onClose={close}
          />
        ) : linked && (org.isLoading || org.isError) ? (
          <>
            <Header title="Organization" lede={org.isError ? errorDetail(org.error, "Could not load the organization.") : "Loading the organization…"} />
            <div className="flex flex-none flex-wrap items-center gap-2.5 border-t border-line-2 bg-surface px-[22px] py-3.5">
              <span className="flex-1" />
              {org.isError && (
                <button type="button" onClick={() => void org.refetch()} className={cn(ACTION, SECONDARY)}>
                  {org.isFetching ? "Retrying…" : "Retry now"}
                </button>
              )}
            </div>
          </>
        ) : (
          <Explainer />
        )}
      </DialogContent>
    </Dialog>
  );
}

const SECONDARY = "border border-line bg-surface-2 px-3.5 text-fg-2 hover:border-line-3 hover:text-fg";
const PRIMARY = "bg-accent px-4 font-semibold text-bg hover:bg-accent/90";

function Header({ title, lede }: { title: string; lede: string }) {
  return (
    <div className="flex flex-none items-start gap-3.5 px-[22px] pb-4 pt-5 pr-12">
      <span className="flex size-[34px] flex-none items-center justify-center rounded-[9px] border border-[rgba(167,139,250,.35)] bg-[rgba(167,139,250,.1)] text-[#c9b8ff]">
        <Cloud size={16} aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <span className="font-mono text-meta text-faint">ORGANIZATIONS · GRAPHBAN CLOUD</span>
        <DialogTitle className="mt-1 text-[17px] tracking-[-.01em]">{title}</DialogTitle>
        <DialogDescription className="mt-1.5 max-w-[62ch] text-body">{lede}</DialogDescription>
      </div>
    </div>
  );
}

function Explainer() {
  return (
    <>
      <Header
        title="One org across every deployment"
        lede="An organization lives in Graphban Cloud and sits above your deployments. It holds people, roles, policy rules and the cross-project view, while each deployment keeps running and storing data where it is."
      />

      <div className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-[22px] pb-5 pt-1">
        <ul
          aria-label="What an organization adds"
          className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,290px),1fr))] gap-px overflow-hidden rounded-xl border border-line-2 bg-line-2"
        >
          {BENEFITS.map((b) => (
            <li
              key={b.title}
              data-benefit
              className={cn("flex gap-3 bg-surface-2 p-3.5", b.wide && "col-span-full")}
            >
              <span className="flex size-[26px] flex-none items-center justify-center rounded-[7px] border border-line bg-surface text-accent" aria-hidden>
                {b.icon}
              </span>
              <div className="flex min-w-0 flex-col gap-[3px]">
                <span className="text-[13px] font-semibold text-fg">{b.title}</span>
                <span className="text-[12px] leading-normal text-muted">{b.body}</span>
              </div>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-x-[18px] gap-y-2 rounded-[10px] border border-line-2 bg-surface px-3.5 py-[11px]">
          <span id="org-stays" className="self-center whitespace-nowrap font-mono text-meta text-faint">
            STAYS ON THIS BOX
          </span>
          <ul aria-labelledby="org-stays" className="contents">
            {STAYS.map((s) => (
              <li key={s} data-stays className="flex items-center gap-1.5 text-[12px] text-fg-2">
                <span className="text-st-done" aria-hidden>✓</span>
                {s}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="flex flex-none flex-wrap items-center gap-2.5 border-t border-line-2 bg-surface px-[22px] py-3.5">
        <DialogClose className="text-[12px] text-muted transition-colors hover:text-fg">Not now</DialogClose>
        <span className="flex-1" />
        <ComingSoonLink label="Link to an existing org" className={cn(ACTION, SECONDARY)} />
        <ComingSoonLink
          label="Create a cloud org"
          className={cn(ACTION, PRIMARY)}
          icon={<ArrowUpRight size={12} strokeWidth={2.2} aria-hidden />}
        />
      </div>
    </>
  );
}

/** The org's host label: the server sends the `<slug>`, or a full host once one is claimed. */
function orgHost(host: string | null | undefined): string | null {
  const h = host?.trim();
  if (!h) return null;
  return h.includes(".") ? h : `${h}.graphban.dev`;
}

/** Zone-less timestamps are UTC (SQLite hands them back naive — see `relTime`). */
function clock(iso: string): string {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(zoned ? iso : `${iso}Z`).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** A sync this recent reads green; older is plain. */
const RECENT_MS = 10 * 60 * 1000;

function isRecent(iso: string): boolean {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return Date.now() - new Date(zoned ? iso : `${iso}Z`).getTime() < RECENT_MS;
}

const CHIP = "rounded-[4px] border px-1.5 py-0.5 font-mono text-meta uppercase";

/**
 * Frames 3a/3b. `role` is the LINKING credential's org role, not the viewer's, so the chip says
 * "LINKED AS …". Unreachable never shows a number: a null count renders "—", never 0, and only
 * this box's own last sync keeps a time (amber) — the server sends nothing else as current.
 */
function OrgOverviewView({
  o,
  retrying,
  onRetry,
  onClose,
}: {
  o: SyncOrgOverview;
  retrying: boolean;
  onRetry: () => void;
  onClose: () => void;
}) {
  const down = o.reachable === false;
  const name = o.org?.name || "your org";
  const host = orgHost(o.org?.host);
  const cloudHref = host ? `https://${host}` : null;
  const counts = down ? null : (o.counts ?? null);
  const stats: { key: string; label: string; value: number | null; to?: string; href?: string | null }[] = [
    { key: "members", label: "Members", value: counts?.members ?? null, href: cloudHref },
    { key: "teams", label: "Teams", value: counts?.teams ?? null, href: cloudHref },
    { key: "policy_rules", label: "Org policy rules", value: counts?.policy_rules ?? null, to: "/rules" },
    { key: "deployments", label: "Deployments", value: counts?.deployments ?? null, href: "#org-deployments" },
  ];
  const initials = name.split(/[\s-]+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("") || "—";

  return (
    <>
      <Header
        title={`Linked to ${name}`}
        lede={`This deployment shares people, roles and org policy rules with ${name}. Code, memory and item content stay here.`}
      />

      <div className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-[22px] pb-5 pt-1">
        {down && (
          <div
            role="status"
            data-org-unreachable
            className="flex gap-2.5 rounded-[10px] border-[1.5px] border-dashed border-[rgba(224,179,74,.7)] bg-[rgba(224,179,74,.05)] px-3.5 py-[11px] text-body leading-normal text-fg-2"
          >
            <span className="text-st-review" aria-hidden>!</span>
            <span>
              Can&apos;t reach {host ?? name}
              {o.unreachable_since ? ` since ${clock(o.unreachable_since)}` : ""}. Org policy rules show as
              unmeasured in Rules and nothing org-sourced can be edited until the link is back. Local work carries on.
            </span>
          </div>
        )}

        <div data-org-identity className="flex flex-wrap items-center gap-3 rounded-xl border border-line-2 bg-surface-2 p-3.5">
          <span aria-hidden className="flex size-[38px] flex-none items-center justify-center rounded-[9px] border border-line bg-surface-3 font-mono text-[13px] font-semibold text-accent">
            {initials}
          </span>
          <div className="flex min-w-[180px] flex-1 flex-col gap-0.5">
            <span className="text-lead font-semibold text-fg">{name}</span>
            {host && <span className="font-mono text-[11.5px] text-muted">{host}</span>}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {o.org?.plan && <span data-chip className={cn(CHIP, "border-line text-muted")}>{o.org.plan} plan</span>}
            {o.role && <span data-chip className={cn(CHIP, "border-line text-fg-2")}>Linked as {o.role}</span>}
            <span
              data-chip
              data-org-status={down ? "unreachable" : "linked"}
              className={cn(
                CHIP,
                "inline-flex items-center gap-[5px]",
                down ? "border-[rgba(224,179,74,.45)] text-st-review" : "border-[rgba(95,208,122,.4)] text-st-done",
              )}
            >
              <span aria-hidden className="size-1.5 rounded-full bg-current" />
              {down ? "Unreachable" : "Linked"}
            </span>
          </div>
        </div>

        <ul aria-label="Organization counts" className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] gap-px overflow-hidden rounded-xl border border-line-2 bg-line-2">
          {stats.map((s) => {
            const body = (
              <>
                <span data-count className={cn("font-mono text-title font-medium", s.value === null ? "text-faint" : "text-fg")}>
                  {s.value === null ? "—" : s.value}
                </span>
                <span className="text-[12px] text-muted">{s.label}</span>
              </>
            );
            const cls = "flex flex-col gap-1 bg-surface-2 px-3.5 py-3 text-fg transition-colors hover:bg-surface-3";
            return (
              <li key={s.key} data-stat={s.key} className="flex flex-col bg-surface-2">
                {s.to ? (
                  <Link to={s.to} onClick={onClose} className={cls}>{body}</Link>
                ) : s.href ? (
                  <a
                    href={s.href}
                    className={cls}
                    {...(s.href.startsWith("http") ? { target: "_blank", rel: "noopener noreferrer" } : {})}
                  >
                    {body}
                  </a>
                ) : (
                  <div className={cn(cls, "hover:bg-surface-2")}>{body}</div>
                )}
              </li>
            );
          })}
        </ul>

        <div id="org-deployments" className="flex flex-col overflow-hidden rounded-xl border border-line-2">
          <span id="org-deployments-label" className="border-b border-line-2 bg-surface px-3.5 py-[9px] font-mono text-meta text-faint">
            DEPLOYMENTS IN THIS ORG
          </span>
          <ul aria-labelledby="org-deployments-label">
            {(o.deployments ?? []).map((d, i) => (
              <DeploymentRow key={`${d.name}-${i}`} d={d} down={down} />
            ))}
            {(o.deployments ?? []).length === 0 && (
              <li className="px-3.5 py-2.5 text-body text-muted">{down ? "—" : "No deployments reported yet."}</li>
            )}
          </ul>
        </div>
      </div>

      <OverviewFooter o={o} name={name} down={down} cloudHref={cloudHref} retrying={retrying} onRetry={onRetry} onClose={onClose} />
    </>
  );
}

function DeploymentRow({ d, down }: { d: SyncOrgDeployment; down: boolean }) {
  let sync: string;
  let tone: string;
  if (down) {
    sync = d.here && d.last_sync_at ? `last sync ${clock(d.last_sync_at)}` : "—";
    tone = d.here && d.last_sync_at ? "text-st-review" : "text-faint";
  } else if (d.last_sync_at) {
    sync = `synced ${relTime(d.last_sync_at)}`;
    tone = isRecent(d.last_sync_at) ? "text-st-done" : "text-muted";
  } else {
    sync = "never synced";
    tone = "text-faint";
  }
  const projects = down || d.projects === null ? "—" : `${d.projects} ${d.projects === 1 ? "project" : "projects"}`;
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

function OverviewFooter({
  o,
  name,
  down,
  cloudHref,
  retrying,
  onRetry,
  onClose,
}: {
  o: SyncOrgOverview;
  name: string;
  down: boolean;
  cloudHref: string | null;
  retrying: boolean;
  onRetry: () => void;
  onClose: () => void;
}) {
  const unlink = useSyncUnlink();
  const [confirming, setConfirming] = React.useState(false);
  const doUnlink = () =>
    unlink.mutate(undefined, {
      onSuccess: () => {
        noteGitopsUnlinked();
        onClose();
      },
    });

  return (
    <div className="flex flex-none flex-col gap-2.5 border-t border-line-2 bg-surface px-[22px] py-3.5">
      {confirming ? (
        <div role="alertdialog" aria-label="Unlink this deployment" className="flex flex-wrap items-center gap-2.5">
          <span className="min-w-0 flex-1 text-body text-fg-2">
            Unlink this deployment from {name}? Sync stops and org policy rules no longer apply; local data stays.
          </span>
          <button type="button" onClick={() => setConfirming(false)} className={cn(ACTION, SECONDARY)}>
            Cancel
          </button>
          <button
            type="button"
            onClick={doUnlink}
            disabled={unlink.isPending}
            className={cn(ACTION, "bg-st-blocked px-4 font-semibold text-bg hover:opacity-90 disabled:opacity-60")}
          >
            {unlink.isPending ? "Unlinking…" : "Unlink"}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2.5">
          <Link to={settingsPath("deployment/sync")} onClick={onClose} className="whitespace-nowrap text-[12px] text-muted transition-colors hover:text-fg">
            Sync link settings
          </Link>
          {o.can_unlink && (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              className="whitespace-nowrap text-[12px] text-[#ff8f8f] transition-colors hover:text-[#ffb3b3]"
            >
              Unlink…
            </button>
          )}
          <span className="flex-1" />
          {down && (
            <button type="button" onClick={onRetry} disabled={retrying} className={cn(ACTION, SECONDARY)}>
              {retrying ? "Retrying…" : "Retry now"}
            </button>
          )}
          {cloudHref && (
            <a href={cloudHref} target="_blank" rel="noopener noreferrer" className={cn(ACTION, PRIMARY)}>
              Open in Graphban Cloud
              <ArrowUpRight size={12} strokeWidth={2.2} aria-hidden />
            </a>
          )}
        </div>
      )}
      {unlink.isError && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-[12px] text-st-blocked">
          <span>{errorDetail(unlink.error, "Could not unlink")}</span>
          {isRefusal(unlink.error) && <RefusalRuleLink ruleId={errorRuleId(unlink.error)} />}
        </div>
      )}
    </div>
  );
}

function ComingSoonLink({ label, className, icon }: { label: string; className: string; icon?: React.ReactNode }) {
  return (
    <a
      href={ORG_SIGNUP_URL}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`${label} — coming soon, join the waitlist`}
      className={className}
    >
      {label}
      <span data-coming-soon className="rounded-[4px] border border-current/35 px-1 font-mono text-[9px] leading-[13px] tracking-[.4px] uppercase opacity-80">
        Coming soon
      </span>
      {icon}
    </a>
  );
}
