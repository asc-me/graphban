import { ArrowUpRight, Cloud, Globe, MessageSquare, ShieldCheck, SquareCheckBig, Users } from "lucide-react";
import * as React from "react";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/cn";
import { ORG_SIGNUP_URL } from "@/lib/routes";

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
 * The not-linked explainer (`Org Link - Hi-fi.dc.html` frame 2a): what an org adds, what stays
 * on this box, and the two ways in. Neither way in is built yet, so both are Coming soon links
 * to the waitlist in a new tab — nothing here calls `POST /api/sync/link` (GRPH-1224).
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
        <div className="flex flex-none items-start gap-3.5 px-[22px] pb-4 pt-5 pr-12">
          <span className="flex size-[34px] flex-none items-center justify-center rounded-[9px] border border-[rgba(167,139,250,.35)] bg-[rgba(167,139,250,.1)] text-[#c9b8ff]">
            <Cloud size={16} aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <span className="font-mono text-meta text-faint">ORGANIZATIONS · GRAPHBAN CLOUD</span>
            <DialogTitle className="mt-1 text-[17px] tracking-[-.01em]">One org across every deployment</DialogTitle>
            <DialogDescription className="mt-1.5 max-w-[62ch] text-body">
              An organization lives in Graphban Cloud and sits above your deployments. It holds people, roles, policy
              rules and the cross-project view, while each deployment keeps running and storing data where it is.
            </DialogDescription>
          </div>
        </div>

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
          <ComingSoonLink
            label="Link to an existing org"
            className={cn(ACTION, "border border-line bg-surface-2 px-3.5 text-fg-2 hover:border-line-3 hover:text-fg")}
          />
          <ComingSoonLink
            label="Create a cloud org"
            className={cn(ACTION, "bg-accent px-4 font-semibold text-bg hover:bg-accent/90")}
            icon={<ArrowUpRight size={12} strokeWidth={2.2} aria-hidden />}
          />
        </div>
      </DialogContent>
    </Dialog>
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
