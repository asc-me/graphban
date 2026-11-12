import * as React from "react";

import { Input, Textarea } from "@/components/ui/input";
import { requestOrgWindow } from "@/features/settings/CloudOrgLinkDialog";
import { useConfig, useOnboarding, useOrgs, useRequestOrg, useSyncStatus } from "@/lib/queries";
import { settingsPath, usableHttpUrl } from "@/lib/routes";

/** "Request again" appears this long after the stored request date — and not before. */
export const REQUEST_AGAIN_DAYS = 14;

export type PlugKind = "hosted" | "request" | "linked" | "unknown";

/** The date the card prints, from the stored ISO stamp ("5 Nov"). */
export function requestedOn(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function canRequestAgain(iso: string, now = Date.now()): boolean {
  const t = Date.parse(iso);
  return Number.isFinite(t) && now - t >= REQUEST_AGAIN_DAYS * 86_400_000;
}

const BTN =
  "h-[30px] rounded-lg border px-[13px] text-body outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:opacity-50";
const EQUAL = `${BTN} border-control bg-transparent text-fg-2 hover:border-control-hover`;

/**
 * The tour's closing card, anchored on Organization (GRPH-P67 §5, D2/D4/D10; design
 * `Tour Stop.dc.html`, `stop: org`). Hosted users in an org get Invite a teammate and never
 * Request an org. A self-host box that is not linked leads with **Link to an org →**, which
 * ends the tour and opens the Organization window on the wizard (L5, GRPH-1564); Request an
 * org stays as the secondary action. "Not now" is an equal button and nothing nags later. There is no Declined/Expired state: after the stored date
 * is 14 days old, Request again overwrites it.
 */
export function OrgPlug({
  onNotNow,
  onNavigate,
}: {
  /** Finish the tour (recorded like any other Finish). */
  onNotNow: () => void;
  /** Finish the tour, then go here. */
  onNavigate: (to: string) => void;
}) {
  const hosted = !!useConfig().data?.hosted_mode;
  const orgs = useOrgs(hosted).data ?? [];
  const requestedAt = useOnboarding().data?.onboarding?.org_requested_at;
  const request = useRequestOrg();
  const sync = useSyncStatus();
  const [form, setForm] = React.useState(false);
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [note, setNote] = React.useState("");

  // Self-host: the link state decides. A failed or pending read is unknown, never unlinked.
  const linkedOrg = sync.data?.linked ? sync.data : null;
  const kind: PlugKind =
    hosted && orgs.length ? "hosted" : hosted ? "request" : linkedOrg ? "linked" : sync.data ? "request" : "unknown";
  const sent = !!requestedAt && !form;

  async function send() {
    try {
      await request.mutateAsync({ name: name.trim(), email: email.trim(), note: note.trim() || undefined });
      setForm(false);
    } catch {
      /* the error line below reads request.error */
    }
  }

  // Self-host, not linked: Link to an org → leads; the request is the secondary action.
  const selfHost = kind === "request" && !hosted;
  const linkToOrg = () => {
    onNotNow();
    requestOrgWindow();
  };

  let requestAction: { label: string; run: () => void; disabled?: boolean } | null = null;
  if (kind === "hosted" || kind === "unknown") requestAction = null;
  else if (form)
    requestAction = { label: "Send", run: send, disabled: !name.trim() || !email.trim().includes("@") || request.isPending };
  else if (!requestedAt) requestAction = { label: "Request an org", run: () => setForm(true) };
  else if (canRequestAgain(requestedAt)) requestAction = { label: "Request again", run: () => setForm(true) };

  let action: { label: string; run: () => void; disabled?: boolean } | null = null;
  if (kind === "hosted") action = { label: "Invite a teammate →", run: () => onNavigate(settingsPath("members")) };
  else if (selfHost && !form) action = { label: "Link to an org →", run: linkToOrg };
  else action = requestAction;

  if (kind === "linked" && linkedOrg) {
    const orgName = linkedOrg.org || "an org";
    const consoleUrl = usableHttpUrl(linkedOrg.cloud_url);
    return (
      <div className="flex flex-col gap-2.5" data-tour-plug="linked">
        <p className="text-[13px] leading-normal text-fg-2 [text-wrap:pretty]">
          This deployment is linked to <strong className="font-semibold text-fg">{orgName}</strong>.
        </p>
        <div className="grid grid-cols-2 gap-2">
          {consoleUrl ? (
            <a
              className={`${EQUAL} inline-flex items-center justify-center`}
              href={consoleUrl}
              target="_blank"
              rel="noreferrer"
              onClick={onNotNow}
            >
              Open the org →
            </a>
          ) : (
            // A junk or env-baked cloud_url is not a link we open: send them to fix it (as LeftNav does).
            <button
              type="button"
              className={EQUAL}
              onClick={() => onNavigate(`${settingsPath("deployment/sync")}?reason=malformed`)}
            >
              Open the org →
            </button>
          )}
          <button type="button" className={EQUAL} onClick={onNotNow}>
            Not now
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2.5" data-tour-plug={kind}>
      <p className="text-[13px] leading-normal text-fg-2 [text-wrap:pretty]">
        Working with a team? Link this <strong className="font-semibold text-fg">deployment</strong> to an org. Not now is
        always an equal choice.
      </p>
      <ul className="list-disc space-y-0.5 pl-4 text-[13px] leading-normal text-fg-2" data-tour-pitch>
        <li>Shared house rules</li>
        <li>Memory that carries across projects</li>
        <li>One roster of everyone's agents</li>
        <li>Capacity routing across your team's entitlements</li>
        <li>Your code graph stays local-first, and any project you don't choose to sync stays local only.</li>
      </ul>
      {form && (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-small text-muted">
            Name
            <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </label>
          <label className="flex flex-col gap-1 text-small text-muted">
            Email
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
          </label>
          <label className="flex flex-col gap-1 text-small text-muted">
            Note (optional)
            <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
        </div>
      )}
      {sent && (
        <p className="text-body text-muted" data-tour-requested>
          Requested on {requestedOn(requestedAt)}. We'll email an invite.
        </p>
      )}
      {request.isError && (
        <p role="alert" className="text-small text-st-blocked">
          Could not send the request: {request.error instanceof Error ? request.error.message : "try again"}
        </p>
      )}
      <div className={action ? "grid grid-cols-2 gap-2" : "grid grid-cols-1 gap-2"}>
        {action && (
          <button type="button" className={EQUAL} disabled={action.disabled} onClick={action.run}>
            {action.label}
          </button>
        )}
        <button type="button" className={EQUAL} onClick={form ? () => setForm(false) : onNotNow}>
          {form ? "Cancel" : action ? "Not now" : "Finish tour"}
        </button>
      </div>
      {selfHost && !form && (
        <>
          <p className="text-small text-faint" data-tour-link-hint>
            Opens Organization → Link to an org. You&apos;ll sign in with a code.
          </p>
          {requestAction && (
            <button
              type="button"
              onClick={requestAction.run}
              className="self-start rounded text-body text-faint outline-none hover:text-fg-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              {requestAction.label}
            </button>
          )}
        </>
      )}
    </div>
  );
}
