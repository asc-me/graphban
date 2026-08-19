import { Check, Copy, Info, Plus, X } from "lucide-react";
import * as React from "react";

import { copyText } from "@/lib/clipboard";
import { errorDetail } from "@/lib/errors";
import {
  useAdminCreateInvite,
  useAdminDecideOrgRequest,
  useAdminInvites,
  useAdminOrgRequests,
  useAdminRevokeInvite,
  useIsPlatformAdmin,
} from "@/lib/queries";
import type { AdminInvite, OrgRequest } from "@/lib/types";

import {
  Callout,
  Card,
  CardHead,
  Empty,
  PageHead,
  PLANS,
  PLAN_TONE,
  Pill,
  Table,
  Th,
  planLabel,
  relTime,
  tintFor,
} from "./parts";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/**
 * Screen 22 — licensing: who may found a new org, and who has asked to.
 *
 * Two things the design asked for are reported rather than offered, because the server
 * has no control behind them: signup mode and invite expiry are deployment env config,
 * so drawing a picker would have shipped a control that silently did nothing. Stating
 * the running policy is the honest version of the same information.
 */
export function OperatorLicensing() {
  const { data: admin } = useIsPlatformAdmin();
  const [history, setHistory] = React.useState(false);
  const [minting, setMinting] = React.useState(false);
  const { data: invites = [], isLoading } = useAdminInvites(history);

  return (
    <div className="max-w-[1180px] px-5 pb-16 pt-6">
      <PageHead
        title="Licensing"
        lede={
          <>
            A platform invite lets someone <span className="text-op-fg-2">found a new org</span> at a
            plan you choose. That is different from an org invite, which adds a person to an
            org that already exists — that one is issued by the org's own admins, not from
            here.
          </>
        }
        right={
          <button
            onClick={() => setMinting((v) => !v)}
            className="inline-flex h-[30px] items-center gap-2 rounded-lg border border-st-next/35 bg-st-next/[0.12] px-3 text-[12.5px] font-semibold text-st-next hover:bg-st-next/20"
          >
            <Plus size={13} /> Mint platform invite
          </button>
        }
      />

      <div className="mb-4">
        <SignupPolicy mode={admin?.signup_mode} expiryDays={admin?.invite_expiry_days} />
      </div>

      {minting && (
        <MintPanel
          expiryDays={admin?.invite_expiry_days}
          onClose={() => setMinting(false)}
          onMinted={() => setHistory(false)}
        />
      )}

      <div className="mb-2.5 flex items-center gap-3">
        <h2 className="text-[14px] font-semibold">Platform invites</h2>
        <div className="flex-1" />
        <button
          onClick={() => setHistory((v) => !v)}
          aria-pressed={history}
          className={`h-[23px] rounded-md border px-2 font-mono text-[9px] tracking-[0.05em] ${
            history
              ? "border-st-next/40 bg-st-next/[0.12] text-st-next"
              : "border-op-line text-op-muted hover:border-st-next/40 hover:text-st-next"
          }`}
        >
          {history ? "SHOWING ALL" : "SHOW HISTORY"}
        </button>
      </div>

      {isLoading ? (
        <div className="rounded-[13px] border border-op-line bg-op-card px-5 py-8 text-center font-mono text-[11px] text-op-faint-2">
          loading…
        </div>
      ) : invites.length === 0 ? (
        <Empty
          title={history ? "No platform invites have ever been issued" : "No invites are outstanding"}
          body={
            history
              ? "Nobody has been given the ability to found an org on this deployment. Existing tenants are unaffected — this only gates new ones."
              : "Every invite issued has been redeemed, revoked, or has expired. Turn on SHOW HISTORY to see which."
          }
        />
      ) : (
        <Table minWidth={900}>
          <div className="flex items-center gap-3 border-b border-op-line-2 bg-op-inset px-3.5 py-2.5 text-op-faint-2">
            <Th className="min-w-0 flex-1">RECIPIENT</Th>
            <Th className="w-[76px] shrink-0">GRANTS</Th>
            <Th className="w-[104px] shrink-0">ISSUED BY</Th>
            <Th className="w-[118px] shrink-0 text-right">STATUS</Th>
            <Th className="w-[148px] shrink-0">REDEEMED AS</Th>
            <span className="w-[150px] shrink-0" />
          </div>
          {invites.map((inv) => (
            <InviteRow key={inv.id} invite={inv} />
          ))}
        </Table>
      )}

      {history && invites.length > 0 && (
        <div className="mt-4">
          <Callout icon={<Info size={15} className="text-st-next" />}>
            <span className="text-op-fg-2">Redeemed invites are kept, not purged</span> — the row is
            how you know which org came from which invite, and at what plan it was founded.
            Revoking only affects an invite that has not been redeemed yet; it never touches
            an org that already exists.
          </Callout>
        </div>
      )}

      <OrgRequests />
    </div>
  );
}

// ── signup policy (reported, not controlled) ───────────────────────────────
const MODE_COPY: Record<string, string> = {
  open: "anyone can register an account and found their own org",
  invite_only: "registration requires a platform invite issued from this screen",
  closed: "registration is refused; no new accounts can be created",
};

function SignupPolicy({ mode, expiryDays }: { mode?: string; expiryDays?: number }) {
  if (!mode) return null;
  return (
    <Callout icon={<Info size={15} className="text-st-next" />}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[9.5px] tracking-[0.06em] text-op-faint-2">SIGNUP MODE</span>
        <Pill
          tone={
            mode === "open"
              ? "text-st-done border-st-done/30 bg-st-done/[0.08]"
              : mode === "closed"
                ? "text-st-blocked border-st-blocked/30 bg-st-blocked/[0.08]"
                : "text-st-review border-st-review/30 bg-st-review/[0.08]"
          }
          label={mode.replace("_", " ")}
        />
        <span className="text-op-muted-2">— {MODE_COPY[mode] ?? "an unrecognised mode"}.</span>
      </div>
      <div className="mt-1.5">
        Both this and the {expiryDays ?? 14}-day invite expiry are deployment env config,
        so they are reported here rather than editable. Changing either means changing the
        deployment's environment and restarting it.
      </div>
    </Callout>
  );
}

// ── mint ───────────────────────────────────────────────────────────────────
function MintPanel({
  expiryDays,
  onClose,
  onMinted,
}: {
  expiryDays?: number;
  onClose: () => void;
  onMinted: () => void;
}) {
  const create = useAdminCreateInvite();
  const [email, setEmail] = React.useState("");
  const [plan, setPlan] = React.useState<string>("");
  const [error, setError] = React.useState("");
  const [minted, setMinted] = React.useState<AdminInvite | null>(null);
  const valid = EMAIL_RE.test(email.trim());

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setError("");
    try {
      const inv = await create.mutateAsync({ email: email.trim(), plan: plan || null });
      setMinted(inv);
      setEmail("");
      onMinted();
    } catch (err) {
      setError(errorDetail(err, "Could not issue the invitation."));
    }
  }

  return (
    <form
      onSubmit={submit}
      className="animate-fade mb-5 rounded-[13px] border border-st-next/25 bg-op-card p-4"
    >
      <div className="mb-3.5 flex items-center gap-2.5">
        <h2 className="flex-1 text-[14px] font-semibold">New platform invite</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="flex h-6 w-6 items-center justify-center rounded-md border border-op-line text-op-faint hover:border-op-line-hover hover:text-op-fg"
        >
          <X size={11} />
        </button>
      </div>

      <div className="grid gap-4 md:grid-cols-[1.2fr_1fr]">
        <div className="flex flex-col gap-3">
          <div>
            <div className="mb-1.5 font-mono text-[10px] tracking-[0.07em] text-op-faint">
              RECIPIENT EMAIL
            </div>
            <input
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="founder@company.dev"
              aria-label="Recipient email"
              className="h-[31px] w-full rounded-lg border border-op-line bg-op-bg px-2.5 font-mono text-[12px] text-op-fg outline-none focus:border-st-next/50"
            />
            <div
              className={`mt-1.5 font-mono text-[10px] ${
                !email.trim() ? "text-op-faint-2" : valid ? "text-st-done" : "text-st-blocked"
              }`}
            >
              {!email.trim()
                ? "who may found the new org"
                : valid
                  ? "valid — one redemption, then the invite closes"
                  : "not a valid address"}
            </div>
          </div>

          <div>
            <div className="mb-1.5 font-mono text-[10px] tracking-[0.07em] text-op-faint">
              PLAN IT GRANTS
            </div>
            <div className="flex gap-1">
              <button
                type="button"
                onClick={() => setPlan("")}
                className={`h-[29px] flex-1 rounded-[7px] border font-mono text-[9.5px] tracking-[0.04em] ${
                  plan === "" ? PLAN_TONE.free : "border-op-line bg-op-bg text-op-faint"
                }`}
              >
                DEFAULT
              </button>
              {PLANS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPlan(p)}
                  className={`h-[29px] flex-1 rounded-[7px] border font-mono text-[9.5px] tracking-[0.04em] ${
                    plan === p ? PLAN_TONE[p] : "border-op-line bg-op-bg text-op-faint"
                  }`}
                >
                  {planLabel(p)}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <div>
            <div className="mb-1.5 font-mono text-[10px] tracking-[0.07em] text-op-faint">EXPIRES</div>
            <div className="flex h-[29px] items-center rounded-[7px] border border-op-line bg-op-bg px-2.5 font-mono text-[10px] text-op-muted-2">
              {expiryDays ?? 14} DAYS · DEPLOYMENT POLICY
            </div>
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-op-faint">
              Not a per-invite choice — every invite this deployment issues gets the same
              window, set by env config.
            </p>
          </div>
          <Callout icon={<Info size={14} className="text-st-next" />}>
            Whoever redeems this becomes the <span className="text-op-fg-2">owner</span> of the
            org they create. One redemption per invite.
          </Callout>
        </div>
      </div>

      <div className="mt-3.5 flex flex-wrap items-center gap-2.5 border-t border-op-line-2 pt-3.5">
        <button
          type="submit"
          disabled={!valid || create.isPending}
          className="h-7 rounded-[7px] border border-st-next/35 bg-st-next/[0.14] px-3 text-[12px] font-semibold text-st-next disabled:border-op-line disabled:bg-transparent disabled:text-op-faint-3"
        >
          {create.isPending ? "Minting…" : "Mint invite"}
        </button>
        <span className="text-[11px] text-op-faint">
          The invite is emailed; the same link is copyable from the table below.
        </span>
        {error && <span className="w-full text-[11px] text-st-blocked">{error}</span>}
      </div>

      {minted && (
        <div className="mt-3 rounded-[9px] border border-st-done/25 bg-st-done/[0.06] px-3 py-2.5">
          <div className="text-[12px] text-st-done">Invited {minted.email}.</div>
          <div className="mt-1 break-all font-mono text-[10.5px] text-op-muted-2">
            {minted.accept_url}
          </div>
        </div>
      )}
    </form>
  );
}

// ── invite rows ────────────────────────────────────────────────────────────
type InviteState = "pending" | "expired" | "redeemed" | "revoked";

function stateOf(inv: AdminInvite): InviteState {
  if (inv.status === "accepted") return "redeemed";
  if (inv.status === "revoked") return "revoked";
  return inv.expired ? "expired" : "pending";
}

const STATE_TONE: Record<InviteState, string> = {
  pending: "text-st-review border-st-review/30 bg-st-review/[0.06]",
  redeemed: "text-st-done border-st-done/30 bg-st-done/[0.09]",
  expired: "text-op-faint border-op-line",
  revoked: "text-op-faint border-op-line",
};

function InviteRow({ invite }: { invite: AdminInvite }) {
  const revoke = useAdminRevokeInvite();
  const [copied, setCopied] = React.useState(false);
  const state = stateOf(invite);
  const faded = state === "expired" || state === "revoked";

  return (
    <div
      className={`flex items-center gap-3 border-b border-op-line-3 px-3.5 py-2.5 hover:bg-[#0e1218] ${
        faded ? "opacity-60" : ""
      }`}
    >
      <span className="flex min-w-0 flex-1 items-center gap-2.5">
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            state === "pending" ? "bg-st-review" : state === "redeemed" ? "bg-st-done" : "bg-op-faint"
          }`}
        />
        <span
          className={`truncate font-mono text-[11.5px] ${faded ? "text-op-faint" : "text-op-fg"}`}
        >
          {invite.email}
        </span>
      </span>
      <span
        className={`w-[76px] shrink-0 font-mono text-[9.5px] uppercase tracking-[0.04em] ${
          invite.plan ? "text-op-fg-2" : "text-op-faint-2"
        }`}
      >
        {invite.plan ? planLabel(invite.plan) : "default"}
      </span>
      <span className="w-[104px] shrink-0 truncate font-mono text-[10.5px] text-op-faint">
        {invite.invited_by_handle ? `@${invite.invited_by_handle}` : "—"}
      </span>
      <span className="flex w-[118px] shrink-0 justify-end">
        <Pill tone={STATE_TONE[state]} label={state} dashed={faded} />
      </span>
      <span className="flex w-[148px] min-w-0 shrink-0 items-center gap-2">
        <RedeemedAs invite={invite} state={state} />
      </span>
      <span className="flex w-[150px] shrink-0 justify-end gap-1">
        {state === "pending" && (
          <>
            <button
              onClick={async () => {
                if (await copyText(invite.accept_url)) {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1600);
                }
              }}
              className={`inline-flex h-[23px] items-center gap-1 rounded-md border px-2 font-mono text-[9px] tracking-[0.05em] ${
                copied
                  ? "border-st-next/50 text-st-next"
                  : "border-op-line text-op-muted hover:border-st-next/40 hover:text-st-next"
              }`}
            >
              {copied ? <Check size={10} /> : <Copy size={10} />}
              {copied ? "COPIED" : "COPY LINK"}
            </button>
            <button
              onClick={() => revoke.mutate(invite.id)}
              disabled={revoke.isPending}
              className="h-[23px] rounded-md border border-st-blocked/30 px-2 font-mono text-[9px] tracking-[0.05em] text-st-blocked hover:bg-st-blocked/10 disabled:opacity-50"
            >
              REVOKE
            </button>
          </>
        )}
      </span>
    </div>
  );
}

/**
 * "Redeemed" and "redeemed into something" are two separate facts.
 *
 * A platform invite authorizes a signup; the org is founded afterwards. So an accepted
 * invite whose account has not founded anything yet says exactly that, rather than
 * borrowing the em-dash that means "does not apply" on a pending row.
 */
function RedeemedAs({ invite, state }: { invite: AdminInvite; state: InviteState }) {
  if (state !== "redeemed") {
    return <span className="font-mono text-[10.5px] text-op-faint-3">—</span>;
  }
  if (!invite.redeemed_org_name) {
    return (
      <span className="font-mono text-[10px] text-op-faint-2">accepted, no org founded</span>
    );
  }
  return (
    <>
      <span
        className="h-[5px] w-[5px] shrink-0 rounded-sm"
        style={{ background: tintFor(invite.redeemed_org_id) }}
      />
      <span className="truncate font-mono text-[10.5px] text-op-fg-2">
        {invite.redeemed_org_name}
      </span>
    </>
  );
}

// ── additional-org requests ────────────────────────────────────────────────
/**
 * The other half of licensing: an existing user asking to found a *second* org.
 *
 * A platform invite cannot serve this — it is refused for an email that already has an
 * account — so the two queues are genuinely different doors and sit side by side here.
 */
function OrgRequests() {
  const { data: requests = [] } = useAdminOrgRequests();
  return (
    <Card className="mt-6">
      <CardHead
        title="Additional-org requests"
        right={
          <span className="font-mono text-[9px] tracking-[0.05em] text-op-faint-2">
            {requests.length} PENDING
          </span>
        }
      />
      {requests.length === 0 ? (
        <p className="px-4 py-4 text-[12px] leading-relaxed text-op-muted-2">
          Nobody is waiting on a decision. Every account gets one org without asking, so an
          empty queue is the normal state — it does not mean requests are being missed.
        </p>
      ) : (
        <div>
          {requests.map((r) => (
            <OrgRequestRow key={r.id} req={r} />
          ))}
        </div>
      )}
      <div className="border-t border-op-line-2 px-4 py-2.5 text-[11px] leading-relaxed text-op-faint">
        Approving grants exactly one additional org and is consumed when spent, so it
        cannot be replayed. Standing multi-org access comes from the enterprise plan instead.
      </div>
    </Card>
  );
}

function OrgRequestRow({ req }: { req: OrgRequest }) {
  const decide = useAdminDecideOrgRequest();
  return (
    <div className="flex items-center gap-3 border-t border-op-line-3 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px]">{req.company || req.user_id}</div>
        <div className="truncate text-[11.5px] text-op-muted-2">
          {req.reason || "no reason given"}
        </div>
        <div className="mt-0.5 font-mono text-[9.5px] text-op-faint-2">
          asked {relTime(req.created_at) ?? "—"}
        </div>
      </div>
      <button
        disabled={decide.isPending}
        onClick={() => decide.mutate({ id: req.id, approve: true })}
        className="h-[26px] rounded-md border border-st-done/35 bg-st-done/[0.1] px-2.5 font-mono text-[9.5px] tracking-[0.05em] text-st-done disabled:opacity-50"
      >
        APPROVE
      </button>
      <button
        disabled={decide.isPending}
        onClick={() => decide.mutate({ id: req.id, approve: false })}
        className="h-[26px] rounded-md border border-op-line px-2.5 font-mono text-[9.5px] tracking-[0.05em] text-op-muted disabled:opacity-50"
      >
        DENY
      </button>
    </div>
  );
}
