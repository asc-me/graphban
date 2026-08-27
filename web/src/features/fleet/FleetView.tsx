import { Check, Copy, Users } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { McpInstall } from "@/features/settings/McpInstall";
import { useProjectCtx } from "@/features/ProjectContext";
import { api } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useFleet } from "@/lib/queries";
import { WAVE_ROLES } from "./wave";
import type { FleetAgent } from "@/lib/types";

/**
 * The Fleet view (PRD-17 D5).
 *
 * Without it every wave costs a trip through Settings and three hand-assembled pastes per
 * terminal — the tax that stops anyone actually running four agents. D1–D3 make a fleet
 * possible; this is what makes it usable.
 */

/**
 * Role colour is the STATUS that role produces, not a fourth colour vocabulary. A worker is
 * the colour of `in_progress` because that is what a worker's items become; a reviewer is the
 * colour of `review`. The roster then rhymes with the tracker rather than asking a reader to
 * learn a second mapping.
 */
const ROLE_TONE: Record<string, string> = {
  planner: "text-[#b794f6] border-[#b794f6]/40",
  worker: "text-[color:var(--color-st-in_progress)] border-[color:var(--color-st-in_progress)]/40",
  reviewer: "text-[color:var(--color-st-review)] border-[color:var(--color-st-review)]/40",
  // Deliberately NOT one of the three colours. An all-in-one agent is not a worker that
  // happens to also review — it is the other posture, where the human is the reviewer and no
  // server-side gate applies. Tinting it as a role would say the opposite.
  "all-in-one": "text-muted border-line-2",
};

const ALL_IN_ONE = "all-in-one";

/**
 * Counts BY ROLE, never a bare total.
 *
 * "4 agents online" is the same number for a balanced fleet and for four workers with nobody
 * to review them — and those need opposite actions, the second being a review queue about to
 * back up. The breakdown is the whole information.
 */
function RoleCounts({ byRole, roles }: { byRole: Record<string, number>; roles: string[] }) {
  const shown = [...roles, ALL_IN_ONE].filter((r) => byRole[r]);
  if (shown.length === 0) return null;
  return (
    <span className="flex items-center gap-1.5">
      {shown.map((r) => (
        <span key={r}
          className={cn("rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
                        ROLE_TONE[r] ?? "text-muted border-line-2")}>
          {byRole[r]} {r}
        </span>
      ))}
    </span>
  );
}

/**
 * The pasted prompt. `project` is the tag of the project the seat was minted into (GRPH-476).
 *
 * A seat used to leave this page carrying a role and nothing else. Twice in one afternoon
 * seats were minted into the wrong project — the request said "Fleet Walk", `activeId` said
 * `agentledger`, and neither mis-mint was detectable from the codes: diagnosing it meant
 * querying `enrolments` on the live database. A credential shown once, that cannot be shown
 * again, and does not say what it grants access to, is one you cannot audit afterwards.
 *
 * Naming it here rather than only in the panel is the difference that matters: the panel is
 * the thing the operator has already misread, and the artefact is what survives the page.
 * And it is written as a CHECK rather than a label — the agent can call `get_context` and
 * find out, which is the only step in this chain that catches a mis-mint without a human
 * noticing first.
 */
function primeSnippet(role: string, seat?: string, project?: string) {
  const scope = project
    ? `\nThis seat is for the ${project} project — call get_context first and stop if it disagrees.`
    : "";
  // The role prompt, rendered short enough to paste. `register_agent` first is the load-bearing
  // line: an agent that claims before registering is invisible to the roster and ungoverned by
  // the role gate.
  if (role === ALL_IN_ONE) {
    // The DEFAULT posture, so what this prompt teaches is what most installs actually do.
    //
    // It used to say claim_next, and that single word made the collision divvy inert on a
    // default install: `AreaReservation` has exactly one writer, inside `claim_cluster`, so
    // the reservation table stayed permanently empty and anything reading live presence
    // rendered nothing. The server had always disagreed — `_directive_next` tells every
    // non-planner, non-reviewer role to "call claim_cluster" — and only the paste-in prompt
    // said otherwise.
    //
    // It also used to say "you are the only agent" and "move items to done yourself". Both
    // are now wrong: an all-in-one agent files into the review pool and pulls from it like
    // every other posture, which is what lets two of them review each other with no new role
    // machinery — the independence gates already refuse an agent its own work.
    return [
      "You are an all-in-one agent on a Graphban project: you plan, build, and review.",
      "Call register_agent first, then heartbeat on the interval it returns, so you appear",
      "on the roster. Take work with claim_cluster — it reserves the files you are about to",
      "touch, so another agent is never handed work that collides with yours.",
      "When a change is finished, move it to review rather than done. Then call claim_review",
      "to take somebody else's finished work and sign_off or bounce it with a reason.",
      "You cannot sign off what you built; if you are the only agent here, the human reviews",
      "your work unless the project owner has turned on self-review.",
    ].join("\n") + scope;
  }
  const duty =
    role === "planner" ? "Read collision_clusters and allocate. Do not claim work yourself."
    : role === "reviewer" ? "Take work with claim_review — never your own — then sign_off or bounce with a reason."
    : "claim_cluster, work it, write actual touchpoints back with update_item, then move it to review.";
  // The seat is substituted, so a human pastes a FILLED prompt rather than editing one —
  // that edit is the step where a code gets mangled or lands in the terminal next door.
  const call = seat
    ? `register_agent(enrolment_code="${seat}", label="<model> @ <host>")`
    : "register_agent";
  // `tools_off_limits` comes back from register_agent and is named here on purpose: the tool
  // manifest was fetched before this agent had a role, so it still lists every tool in the
  // product. Without this line the agent learns its boundary by being refused — and three
  // refusals in a row is how the server decides an agent has stopped listening.
  return `You are a ${role} in a Graphban fleet.\nCall ${call} first, then heartbeat on the interval it returns.\nIt returns tools_off_limits — the tools your role will be refused. Your tool list was fetched before you had a role, so it still shows them; do not call them.\n${duty}${scope}`;
}

/** Adapters the supervisor can actually resolve — `gbfleet.adapters.ADAPTERS`.
 *
 * Listed rather than free-typed so the UI cannot advertise a vendor `gbfleet up` would refuse
 * at startup. `codex` is deliberately absent there and so is absent here.
 */
const ADAPTERS = ["claude", "cursor-agent", "gbagent", "grok"] as const;

/**
 * Handing the seats to a supervisor (GRPH-556, PRD-22).
 *
 * `gbfleet` is built and tested and appeared NOWHERE in this product — while this very panel
 * mints the one artefact it consumes. `gbfleet up --seats-file` wants one enrolment code per
 * line, and the codes above are only ever rendered inside a prose prompt, so assembling that
 * file meant hand-extracting them from text that says "shown once".
 *
 * **A handoff, not a control.** The supervisor cuts worktrees and spawns processes on the
 * operator's machine; a web page cannot start one and should not pretend to. So the whole job
 * here is to hand over the exact thing to run, with the values already filled in.
 *
 * Collapsed, and only present once seats exist. Seat mode — paste a prompt into a terminal —
 * is still the normal way to run a fleet; this is the advanced path.
 */
function SupervisorHandoff({ seats, wave }: { seats: { role: string; code: string }[]; wave: string }) {
  const [open, setOpen] = React.useState(false);
  const [adapter, setAdapter] = React.useState<string>(ADAPTERS[0]);
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  // Exactly the file format: one code per line and nothing else. No roles, no comments —
  // `read_seats` splits on lines, so anything decorative here is a code it would try to redeem.
  const seatsFile = seats.map((s) => s.code).join("\n");
  const command =
    `export GBFLEET_API_KEY=gb_sk_…\n` +
    `gbfleet up --server ${origin} --seats-file seats.txt \\\n` +
    `  --adapter ${adapter} --wave ${wave} -- ${adapter} --mcp-config {seat_file} -p {instruction_file}`;

  return (
    <div className="mt-3 rounded-[11px] border border-line-2 bg-surface-2/40 p-3">
      <button onClick={() => setOpen((v) => !v)}
              className="text-[11.5px] text-muted transition-colors hover:text-fg-2">
        {open ? "▾" : "▸"} Run these seats under a supervisor (advanced)
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          <p className="px-1 text-[11px] text-faint">
            <span className="font-mono">gbfleet</span> spawns one agent per seat, each in its own
            git worktree, and reaps them when the wave ends. It runs on <em>your</em> machine —
            this page can only hand you the pieces.
          </p>
          {/* THE SECOND CREDENTIAL, named because it is the first thing to get wrong. The
              supervisor authenticates with an ordinary API key; the seats are for its CHILDREN.
              Handing it a seat, or handing a child the key, both fail in confusing ways. */}
          <p className="px-1 text-[11px] text-faint">
            The supervisor needs its own <strong>API key</strong> from Settings → API Keys — not
            a seat. Seats are what it gives its children. Its own reach is deliberately narrow:{" "}
            <span className="font-mono">fleet_status</span> and{" "}
            <span className="font-mono">propose_allocation</span>, nothing that claims work.
          </p>
          <div className="flex flex-wrap items-center gap-1.5 px-1">
            <span className="text-[11px] text-faint">Adapter:</span>
            {ADAPTERS.map((a) => (
              <button key={a} onClick={() => setAdapter(a)} aria-pressed={adapter === a}
                      className={cn("rounded-md border px-2 py-0.5 font-mono text-[10.5px] transition-colors",
                                    adapter === a ? "border-accent/50 bg-surface-3 text-fg"
                                                  : "border-line-2 text-muted hover:text-fg-2")}>
                {a}
              </button>
            ))}
          </div>
          <CopyRow label={`seats.txt — ${seats.length} code${seats.length === 1 ? "" : "s"}, one per line`}
                   value={seatsFile} />
          <CopyRow label="Then run" value={command} />
          <p className="px-1 text-[11px] text-faint">
            Which adapter and which model are not the same question, and the model decides
            whether this works at all — see{" "}
            <span className="font-mono">docs/fleet-adapters.md</span>.
          </p>
        </div>
      )}
    </div>
  );
}


function CopyRow({ label, value }: { label: string; value: string }) {
  const [done, setDone] = React.useState(false);
  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2 p-3">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[12px] text-fg-2">{label}</span>
        <Button
          size="sm"
          onClick={async () => { await copyText(value); setDone(true); setTimeout(() => setDone(false), 1200); }}
        >
          {done ? <Check size={13} /> : <Copy size={13} />}
        </Button>
      </div>
      <pre className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11px] text-muted">
        {value}
      </pre>
    </div>
  );
}

/**
 * How long since an agent last reported.
 *
 * Null is "never reported", which is a registered agent that has not run — different from
 * one that ran and stopped, and the two would otherwise both render as silence.
 */
function heartbeatLabel(at: string | null): string {
  if (!at) return "no heartbeat yet";
  const iso = /(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`;
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function AgentRow({ a, onDismiss, dismissed }: {
  a: FleetAgent; onDismiss?: (id: string, undo?: boolean) => void; dismissed?: boolean;
}) {
  const offline = a.state === "offline";
  const quarantined = a.state === "quarantined";
  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5",
        // Offline agents FADE rather than vanish. One that died holding a branch is exactly
        // what a human needs to see, and dropping it from the roster would answer "who is out
        // there" with a tidier lie.
        offline && "opacity-55",
        quarantined && "border-[color:var(--color-st-blocked)]/50",
      )}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] text-faint">{a.key}</span>
          <span className="truncate text-[13px] text-fg-2">{a.label || "unnamed agent"}</span>
        </div>
        <div className="mt-0.5 font-mono text-[10.5px] text-faint">
          {/* WHICH credential this agent authenticated with. A stale key in a client config is
              otherwise invisible here: the agent, its role and its state all look right, and
              the fact that explains a surprising role is the one thing not shown. `single`
              marks a credential a role hint cannot narrow — so an all-in-one key that still
              produces a worker is legible as the OLD key rather than a broken gate. */}
          {a.credential && (
            <span className="mr-2 text-muted-2">
              {a.credential}
              {a.credential_posture === "single" && (
                <span className="ml-1 text-faint">· single</span>
              )}
            </span>
          )}
          {/* O3: a forgotten code makes an agent silently all-in-one — safe, but not a
              fleet. The server can only report the fact; making it impossible to miss is
              this row's job. */}
          {!a.enrolment_id && (
            <span className="mr-2 text-[color:var(--color-st-blocked)]">not enrolled</span>
          )}
          {a.worktree || "no worktree"}
          {a.branch ? ` · ${a.branch}` : ""}
          {/* The fleet released the ITEM by itself. The BRANCH is state only a human can
              resolve, so it is surfaced in the row rather than left in a log. */}
          {a.branch_orphaned && (
            <span className="ml-2 text-[color:var(--color-st-blocked)]">branch orphaned</span>
          )}
        </div>
      </div>
      <div className="flex flex-col items-end gap-1">
        <span className={cn("rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase",
                            ROLE_TONE[a.active_role] ?? "text-muted border-line-2")}>
          {a.active_role}
        </span>
        <span className="font-mono text-[10px] text-faint">{a.state}</span>
        {/* WHEN it was last heard from, not just that it is offline. `offline` alone cannot
            tell a process that died thirty seconds ago from one gone for a week, and those
            call for opposite responses: wait, or go clean up the branch it left behind. */}
        <span className="font-mono text-[9.5px] text-faint-2" title={a.last_seen_at ?? undefined}>
          {heartbeatLabel(a.last_seen_at)}
        </span>
      </div>
      <div className="w-40 text-right text-[11px] text-muted">
        {a.holdings.length === 0
          ? <span className="text-faint">holding nothing</span>
          : a.holdings.map((h) => (
              <div key={h.id} className="truncate">
                {h.id}{" "}
                {/* The phase is an INFERENCE, so the title carries the signal that produced
                    it — a derived label nobody can check is one they have to trust. `stale`
                    is dimmed rather than coloured: it means the agent is gone and these
                    signals are frozen, which must not read like work in flight. */}
                <span className={h.phase === "stale" || h.phase === "unknown"
                                   ? "text-faint italic" : "text-faint-2"}
                      title={h.phase_basis}>
                  {h.phase}{h.bounced ? " · rework" : ""}
                </span>
              </div>
            ))}
      </div>
      {onDismiss && (
        <button onClick={() => onDismiss(a.id, dismissed)}
                className="text-[11px] text-faint hover:text-fg-2">
          {dismissed ? "Restore" : "Dismiss"}
        </button>
      )}
    </div>
  );
}

export function FleetView() {
  // `active` as well as `activeId` (GRPH-476): eight write calls on this page are scoped to
  // the active project and none of them said which one. The id is what the API needs; the
  // NAME is what an operator can check against what they meant.
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const { data, refetch } = useFleet(activeId);
  const [role, setRole] = React.useState("worker");
  const [minted, setMinted] = React.useState<{ plaintext: string; role: string } | null>(null);
  // Held in component state ONLY, and never written anywhere: these are plaintext credentials,
  // shown once, exactly like the single-role mint above.
  const [minting, setMinting] = React.useState(false);
  // Seats are plaintext, shown once, and held in component state ONLY — never persisted.
  const [seatPlan, setSeatPlan] = React.useState<Record<string, number>>({});
  const [issued, setIssued] = React.useState<{ id: string; role: string; code: string }[]>([]);
  const [error, setError] = React.useState("");
  const [confirming, setConfirming] = React.useState<null | {
    keys: number; seats: number; agents: number; leases: number; reservations: number;
  }>(null);
  const [confirmWave, setConfirmWave] = React.useState<string | null>(null);
  // The counts must never lag the selection. A confirm showing wave-1's damage while wave-2
  // is chosen is the precise failure this dialog exists to prevent, so the numbers are hidden
  // while a new preview is in flight rather than left showing the previous wave's.
  const [previewing, setPreviewing] = React.useState(false);
  // One flag per tab. A single shared one meant expanding spent SEATS also expanded revoked
  // CREDENTIALS on the other tab — two unrelated disclosures moving together, which is
  // confusing precisely because the tabs exist to keep those questions apart.
  const [showSpentSeats, setShowSpentSeats] = React.useState(false);
  const [showDeadCreds, setShowDeadCreds] = React.useState(false);
  const [showGone, setShowGone] = React.useState(false);
  const [showDismissed, setShowDismissed] = React.useState(false);
  // The wave label comes BACK from the server now. It used to be hardcoded `wave-1` here, so
  // every wave for weeks landed in one bucket and End wave always ended everything.
  const [wave, setWave] = React.useState<string | null>(null);
  // TWO tabs, one question each: "who can reach this project and who is here now", and "what
  // is running right now". The previous three were organised by OBJECT — agents, seats,
  // credentials — while the work is organised by CADENCE: a credential is written once per
  // machine, seats are issued per wave, the roster is watched continuously. A Credentials tab
  // that only listed things was a tab you visit once and never again.
  const [tab, setTab] = React.useState<"connections" | "wave" | "work">("connections");

  async function mint() {
    setError("");
    try {
      const out = await api.mintFleetKey({ project_id: activeId, role, wave: wave ?? "wave-1" });
      setMinted({ plaintext: out.plaintext, role: out.role });
    } catch (e) {
      setError(errorDetail(e, "could not mint a fleet credential"));
    }
  }

  async function issueSeats() {
    setError("");
    setMinting(true);
    try {
      const roles: string[] = WAVE_ROLES.flatMap((r) => Array(seatPlan[r] ?? 0).fill(r));
      const out = await api.issueSeats({ project_id: activeId, roles });
      setWave(out.wave);
      setIssued(out.seats);
      setSeatPlan({});
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not issue the seats"));
    } finally {
      setMinting(false);
    }
  }

  async function reissue(seatId: string) {
    setError("");
    try {
      const out = await api.reissueSeat(seatId);
      // Appended rather than replacing: the operator may be part-way through pasting the
      // others, and dropping those would cost codes that cannot be shown again.
      setIssued((prev) => [...prev, { id: out.id, role: out.role, code: out.code }]);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not reissue that seat"));
    }
  }

  async function clearUnusedSeats() {
    setError("");
    try {
      // Unused only. A consumed seat records which agent took what, and ending the wave is
      // what stops a live session — clearing leftovers must not do either by accident.
      await api.revokeUnusedSeats(activeId);
      setIssued([]);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not clear the unused seats"));
    }
  }

  async function dismiss(id: string, undo = false) {
    setError("");
    try {
      await api.dismissAgent(id, undo);
      await refetch();
    } catch (e) {
      // The server refuses an agent that still holds work — surfaced verbatim, because the
      // reason IS the instruction: release it first.
      setError(errorDetail(e, "could not dismiss that agent"));
    }
  }

  async function clearExpiredCredentials() {
    setError("");
    try {
      // EXPIRED only. "Unused" is the tempting second signal and is a trap: a key minted five
      // minutes ago for a machine nobody has set up yet has never been used, and sweeping on
      // that would revoke an operator's own setup before they finished it.
      await api.revokeExpiredKeys(activeId);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not clear the expired credentials"));
    }
  }

  async function revokeCredential(id: string) {
    setError("");
    try {
      await api.revokeKey(id);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not revoke that credential"));
    }
  }

  async function previewWave(w: string) {
    setError("");
    setConfirmWave(w);
    setPreviewing(true);
    try {
      setConfirming(await api.endWavePreview(activeId, w));
    } catch (e) {
      setConfirming(null);
      setError(errorDetail(e, "could not read that wave"));
    } finally {
      setPreviewing(false);
    }
  }

  async function askEndWave() {
    await previewWave(liveWave);
  }

  async function doEndWave() {
    try {
      await api.endWave(activeId, confirmWave ?? liveWave);
      setConfirming(null);
      setConfirmWave(null);
      setMinted(null);
      setIssued([]);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not end the wave"));
    }
  }

  const agents = data?.agents ?? [];
  // WHICH wave End wave ends. Before, this was the hardcoded `wave-1`; leaving it blank once
  // waves started incrementing would have meant "every wave in the project", which is a
  // bigger hammer than the button promises. Falls back to the newest wave the seats know
  // about, so the confirm always names a real cohort.
  // Every wave this project has, newest first. Drawn from BOTH tables because a wave owns
  // seats now and owned keys before PRD-19 — a wave whose keys are still around is still a
  // wave somebody may want to end.
  // Server-computed: a wave is offered only while it owns an un-revoked seat or key. Derived
  // client-side this listed every wave that had EVER existed — three dead cohorts without one
  // live seat between them.
  const waves = data?.waves ?? [];

  // A seat is LIVE if it can still do something: unused (redeemable) or consumed by an agent
  // that may still be working. Revoked and expired are history.
  const liveSeats = (data?.seats ?? []).filter((s) => s.state === "unused" || s.state === "consumed");
  const spentSeats = (data?.seats ?? []).filter((s) => s.state === "revoked" || s.state === "expired");
  const liveCreds = (data?.credentials ?? []).filter((c) => !c.revoked);
  const deadCreds = (data?.credentials ?? []).filter((c) => c.revoked);

  // An offline agent that still HOLDS something is unfinished business — a lease nobody can
  // finish, or a branch only a human can resolve. That is what "offline agents fade rather
  // than vanish" was written for, and it is kept in full view.
  //
  // An offline agent holding nothing is history, exactly like a revoked seat. Two thirds of
  // this roster was that: 16 of 24 rows, so the tab answering "who is here now" was mostly
  // answering "who was ever here".
  // Dismissed rows are out of every group — a human said they were done with them, and the
  // row survives only because durable work references the id.
  const kept = agents.filter((a) => !a.dismissed);
  const dismissed = agents.filter((a) => a.dismissed);
  const shownAgents = kept.filter(
    (a) => a.state !== "offline" || a.holdings.length > 0 || a.branch_orphaned);
  const goneAgents = kept.filter(
    (a) => a.state === "offline" && a.holdings.length === 0 && !a.branch_orphaned);
  // Un-enrolled agents hold no seat: the single-agent posture, which is legitimate and simply
  // is not a fleet. Grouped BELOW rather than hidden — these are live processes doing work,
  // not history, so the disclosure is about order rather than concealment.
  const fleetAgents = shownAgents.filter((a) => a.enrolled);
  const soloAgents = shownAgents.filter((a) => !a.enrolled);
  // Already dead, so revoking only tidies the list. Deliberately NOT "never used" — see
  // clearExpiredCredentials.
  const expiredCreds = liveCreds.filter(
    (c) => c.expires_at !== null && new Date(c.expires_at) <= new Date());

  const reviewCount = (data?.review_queue ?? []).length;

  const liveWave = wave ?? waves[0] ?? "wave-1";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center justify-between border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">
            Fleet <span className="font-mono text-[13px] font-normal text-muted">{scope}</span>
          </h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            {data
              ? `${data.online} of ${data.total} online · heartbeat every ${data.heartbeat_interval_seconds}s`
              : "Agents working this project, and what they hold."}
          </p>
          {data && data.online > 0 && (
            <div className="mt-1.5 flex items-center gap-2">
              <RoleCounts byRole={data.by_role ?? {}} roles={data.roles ?? []} />
              {/* The posture is NAMED, because both are first-class and a reader should know
                  which they are looking at. A single-agent deployment is not a fleet that has
                  gone wrong — it is the default, where the human is the reviewer. */}
              <span className="text-[11px] text-faint">
                {data.posture === "fleet"
                  ? "specialised roles — the fleet reviews itself"
                  : "single-agent — you are the reviewer"}
              </span>
            </div>
          )}
        </div>
        {/* Nothing live means nothing to end. */}
        {waves.length > 0 && (
          <Button onClick={askEndWave}>End wave</Button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {error && <p className="mb-3 text-[12px] text-red-400">{error}</p>}

        {confirming && (
          <div className="mb-5 rounded-[11px] border border-[color:var(--color-st-blocked)]/50 bg-surface-2 p-4">
            {/* WHICH wave, chosen explicitly. Ending a wave is irreversible, and before this
                the target was whatever the page happened to think was current — fine with one
                wave, wrong the moment there are two. */}
            {waves.length > 1 && (
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[11px] text-faint">Wave:</span>
                {waves.map((w) => (
                  <button
                    key={w}
                    onClick={() => previewWave(w)}
                    aria-pressed={(confirmWave ?? liveWave) === w}
                    className={cn("rounded-md border px-2 py-0.5 font-mono text-[10.5px] transition-colors",
                                  (confirmWave ?? liveWave) === w
                                    ? "border-accent/50 bg-surface-3 text-fg"
                                    : "border-line-2 text-muted hover:text-fg-2")}
                  >
                    {w}
                  </button>
                ))}
              </div>
            )}
            <p className="text-[13px] text-fg-2">
              {previewing ? (
                // Never show the previous wave's numbers against a new selection.
                <span className="text-muted">Reading {confirmWave}…</span>
              ) : (
                <>
                  End <span className="font-mono">{confirmWave ?? liveWave}</span> in{" "}
                  <span className="font-mono">{scope}</span>: revoke{" "}
                  {confirming.seats} seat{confirming.seats === 1 ? "" : "s"}
                  {/* Keys are named ONLY when the wave owns some. Under enrolment a wave owns
                      seats and no keys, so a permanent "revoke 0 keys" clause was describing
                      a mechanism that no longer applies to new waves — and burying the number
                      that does matter. Old waves predating PRD-19 still own keys, and for
                      those it is real damage that must be named. */}
                  {confirming.keys > 0 && (
                    <> and {confirming.keys} wave-tagged key{confirming.keys === 1 ? "" : "s"}</>
                  )}
                  , release {confirming.leases} lease{confirming.leases === 1 ? "" : "s"} and{" "}
                  {confirming.reservations} reservation{confirming.reservations === 1 ? "" : "s"}?
                </>
              )}
            </p>
            <p className="mt-1 text-[12px] text-muted">
              Your own credential is untouched — only wave-tagged keys and this wave&apos;s
              seats, in <span className="font-mono">{scope}</span> only. This cannot be undone.
            </p>
            <div className="mt-3 flex gap-2">
              <Button onClick={doEndWave} disabled={previewing}>
                End {confirmWave ?? liveWave}
              </Button>
              <Button onClick={() => setConfirming(null)}>Cancel</Button>
            </div>
          </div>
        )}

        {/* Three views of one fleet, because they answer different questions and the walk
            kept crossing between them: WHO is out there, WHAT seats are outstanding, and
            WHICH credential each agent is on. Previously the last of those lived on another
            screen entirely. */}
        <div className="mb-3 flex gap-1">
          {([
            ["connections", `Connections${agents.length ? ` (${agents.length})` : ""}`],
            ["wave", `Wave${(data?.seats ?? []).filter((s) => s.state === "unused").length
              ? ` (${data!.seats.filter((s) => s.state === "unused").length} unused)` : ""}`],
            // The count rides the LABEL so the queue stays legible from any tab. Burying
            // "3 items waiting, all built by the only reviewer" behind a click is the one
            // real cost of tabbing this page, and this is what pays it.
            ["work", `Work${reviewCount ? ` (${reviewCount} in review)` : ""}`],
          ] as const).map(([id, label]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              aria-pressed={tab === id}
              className={cn("rounded-[9px] border px-3 py-1.5 text-[12px] transition-colors",
                            tab === id ? "border-accent/50 bg-surface-3 text-fg"
                                       : "border-line-2 text-muted hover:text-fg-2")}
            >
              {label}
            </button>
          ))}
          <span className="self-center pl-2 font-mono text-[11px] text-faint">{liveWave}</span>
        </div>

        {/* A number in a tab label is easy to miss when you are looking at something else.
            Work waiting for a second pair of eyes is the one thing on this page that stalls a
            fleet, so the other tabs say so in a sentence and take you there. Shown ONLY when
            there is something waiting — a permanent banner is one you stop reading. */}
        {tab !== "work" && reviewCount > 0 && (
          <button
            onClick={() => setTab("work")}
            className="mb-4 flex w-full items-center gap-2 rounded-[11px] border border-[color:var(--color-st-review)]/40 bg-surface-2 px-3 py-2 text-left text-[12px] text-fg-2 hover:border-[color:var(--color-st-review)]/70"
          >
            <span className="font-mono text-[11px] text-[color:var(--color-st-review)]">
              {reviewCount}
            </span>
            item{reviewCount === 1 ? "" : "s"} waiting for review
            <span className="ml-auto text-[11px] text-muted">Work →</span>
          </button>
        )}

        {tab === "connections" && (
          <>
        <Section title="Roster" desc="Offline agents fade rather than vanish — one that died holding a branch is what you need to see.">
          {agents.length === 0 ? (
            <Empty>No agents yet. Mint a credential below and paste it into a terminal.</Empty>
          ) : (
            <div className="space-y-2">
              {fleetAgents.map((a) => <AgentRow key={a.id} a={a} onDismiss={dismiss} />)}
              {soloAgents.length > 0 && (
                <>
                  <div className="pt-1 text-[11px] text-faint">
                    {soloAgents.length} un-enrolled · single-agent posture, no role gate
                  </div>
                  {soloAgents.map((a) => <AgentRow key={a.id} a={a} onDismiss={dismiss} />)}
                </>
              )}
              {goneAgents.length > 0 && (
                <button onClick={() => setShowGone((v) => !v)}
                        className="w-full pt-1 text-left text-[11px] text-faint hover:text-fg-2">
                  {showGone ? "Hide" : "Show"} {goneAgents.length} gone
                </button>
              )}
              {showGone && goneAgents.map((a) => <AgentRow key={a.id} a={a} onDismiss={dismiss} />)}
              {dismissed.length > 0 && (
                <button onClick={() => setShowDismissed((v) => !v)}
                        className="w-full text-left text-[11px] text-faint hover:text-fg-2">
                  {showDismissed ? "Hide" : "Show"} {dismissed.length} dismissed
                </button>
              )}
              {showDismissed && dismissed.map((a) => (
                <AgentRow key={a.id} a={a} onDismiss={dismiss} dismissed />
              ))}
            </div>
          )}
        </Section>

          </>
        )}

        {tab === "wave" && (
          <>
        <Section
          title="Provision a whole wave"
          desc="One seat per agent. A seat grants a role for one session and expires — paste it into the prompt, not the config. A WAVE is one round of work: its name becomes the branch prefix, and ending it revokes every seat and releases every lease it issued."
        >
          {/* Seats, not credentials. The credential goes into the client config ONCE and
              stays; a seat is what makes an agent a worker or a reviewer for this run. That
              split is what lets a client storing one MCP config for every agent — Cursor —
              run a fleet at all. */}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {WAVE_ROLES.map((r) => (
              <button
                key={r}
                onClick={() => setSeatPlan({ ...seatPlan, [r]: (seatPlan[r] ?? 0) + 1 })}
                className="rounded-[9px] border border-line-2 px-3 py-1.5 text-[12px] text-muted transition-colors hover:text-fg-2"
              >
                + {r}
              </button>
            ))}
            <span className="font-mono text-[11px] text-faint">
              {WAVE_ROLES.filter((r) => seatPlan[r]).map((r) => `${seatPlan[r]}x ${r}`).join(" · ") || "no seats yet"}
            </span>
            {Object.values(seatPlan).some(Boolean) && (
              <button onClick={() => setSeatPlan({})} className="text-[11px] text-faint hover:text-fg-2">
                clear
              </button>
            )}
          </div>
          {/* The project is NAMED on the button, not only in the page chrome. `ProjectBar`
              already shows it — and an operator who has already misread which project is
              active will misread it there too. This is at the point of action. */}
          <Button onClick={issueSeats} disabled={!Object.values(seatPlan).some(Boolean) || minting}>
            {minting ? "Issuing…" : `Issue the seats into ${scope}`}
          </Button>
          {issued.length > 0 && (
            <div className="mt-3 space-y-2">
              {issued.map((s) => (
                <CopyRow key={s.id} label={`${s.role} for ${scope} — prompt + seat, shown once`}
                         value={primeSnippet(s.role, s.code, scope)} />
              ))}
              <p className="px-1 text-[11px] text-faint">
                Paste each into that agent&apos;s prompt as{" "}
                <span className="font-mono">register_agent(enrolment_code=&quot;…&quot;)</span>. Two
                workers need two seats: agents sharing a seat share a session and cannot review
                each other.
              </p>
              <SupervisorHandoff seats={issued} wave={wave ?? "wave-1"} />
            </div>
          )}
          {(data?.seats ?? []).length > 0 && (
            <div className="mt-3 space-y-1.5">
              {/* Clearing leftovers is not ending the wave. Unused seats only — a consumed one
                  records which agent took what, and a live session is stopped by End wave. */}
              {data!.seats.some((s) => s.state === "unused" || s.state === "expired") && (
                <div className="flex justify-end">
                  {/* Sweeps unredeemed seats — unused AND expired, since an expired one is
                      already unusable and leaving it listed as "expired" forever is the
                      clutter this button exists to clear. Consumed seats are never touched. */}
                  <button onClick={clearUnusedSeats}
                          className="text-[11px] text-muted hover:text-[color:var(--color-st-blocked)]">
                    Clear the {data!.seats.filter((s) => s.state === "unused" || s.state === "expired").length} unredeemed
                  </button>
                </div>
              )}
              {/* SPENT seats are collapsed, not listed. A revoked or expired seat is history —
                  after ending a wave the list was 19 dead rows deep and the two seats that
                  still mattered were invisible in it. Kept reachable rather than dropped,
                  because the chain of who took what is the audit trail. */}
              {liveSeats.map((s) => (
                <div key={s.id} className="flex items-center gap-3 rounded-[9px] border border-line-2 bg-surface-2 px-3 py-1.5">
                  <span className={cn("rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase",
                                      ROLE_TONE[s.role] ?? "text-muted border-line-2")}>
                    {s.role}
                  </span>
                  <span className="font-mono text-[11px] text-faint">{s.wave}</span>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
                    {s.state === "consumed" ? `taken by ${s.consumed_by}` : s.state}
                    {s.reissued_from && " · reissued"}
                  </span>
                  {/* A spent seat is not deleted — it is the record that something died, and
                      Reissue is the recovery path a single-use code needs. */}
                  {s.state !== "unused" && (
                    <button onClick={() => reissue(s.id)} className="text-[11px] text-muted hover:text-fg">
                      Reissue
                    </button>
                  )}
                </div>
              ))}
              {spentSeats.length > 0 && (
                <button onClick={() => setShowSpentSeats((v) => !v)}
                        className="w-full pt-1 text-left text-[11px] text-faint hover:text-fg-2">
                  {showSpentSeats ? "Hide" : "Show"} {spentSeats.length} spent
                </button>
              )}
              {showSpentSeats && spentSeats.map((s) => (
                <div key={s.id} className="flex items-center gap-3 rounded-[9px] border border-line-2 bg-surface-2 px-3 py-1.5 opacity-50">
                  <span className="rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase text-muted">
                    {s.role}
                  </span>
                  <span className="font-mono text-[11px] text-faint">{s.wave}</span>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
                    {s.state === "consumed" ? `taken by ${s.consumed_by}` : s.state}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>

          </>
        )}

        {tab === "connections" && (
          <>
            {/* Credential provisioning sits WITH the roster: between them they answer
                one question — who can reach this project, and who is here now. It used
                to live beside a per-wave action, which put a once-per-machine job next
                to one you do every run. */}
        <Section
          title="Onboard one agent with its own credential"
          desc="The older route: a credential narrowed to one role. Seats above are the recommended path — this stays because a role-narrowed key still works and some setups are built on one."
        >
          {/* NOT deleted with the rest of PRD-19 E8, deliberately. G6 says nothing that works
              today stops working, and a role-narrowed credential still does — the API is
              unchanged and this repo's own tests use one. What was wrong was having two routes
              with nothing saying which to reach for, so the ambiguity is resolved by NAMING
              the order rather than by removing the option out from under anyone. */}
          <div className="mb-3 flex flex-wrap gap-2">
            {/* `all-in-one` is offered beside the three because the roster REPORTS it — a
                page that counts a posture it cannot create names a category the reader has no
                way to produce. It mints an unnarrowed credential, which is what makes the
                agent unrestricted. */}
            {[...(data?.roles ?? ["planner", "worker", "reviewer"]), ALL_IN_ONE].map((r) => (
              // Selection is signalled by BACKGROUND and border, never by the role tone.
              // Tinting the selected item with `ROLE_TONE` put `all-in-one` in `text-muted
              // border-line-2` — byte-identical to the unselected style — so choosing it
              // looked exactly like not choosing it. Found on the first walk, after three
              // credentials came out all-in-one. The roster badges keep the colours; a
              // picker's job is "which one is selected".
              <button key={r} onClick={() => setRole(r)} aria-pressed={role === r}
                className={cn("rounded-[9px] border px-3 py-1.5 text-[12px] transition-colors",
                              role === r
                                ? "border-accent/50 bg-surface-3 text-fg"
                                : "border-line-2 text-muted hover:text-fg-2")}>
                {r}
              </button>
            ))}
          </div>
          <Button onClick={mint}>
            Mint {role === ALL_IN_ONE ? "an" : "a"} {role} credential
          </Button>
          {minted && (
            <div className="mt-3 space-y-2">
              {/* Shown once — keys are stored hashed and cannot be recovered. */}
              <CopyRow label="1. Key (shown once)" value={minted.plaintext} />
              {/* The REAL generator, shared with Settings → API Keys (AL-78): per-client
                  formats verified against each tool's docs, correct config filenames, and the
                  note that Grok needs an mcp-remote stdio bridge. This view first shipped a
                  two-branch stub of its own that handed the `claude mcp add` command to
                  Codex, Grok and opencode alike — found on the first real walk. */}
              <McpInstall apiKey={minted.plaintext} />
              <CopyRow label="3. Prime" value={primeSnippet(minted.role, undefined, scope)} />
            </div>
          )}
        </Section>
          </>
        )}

        {tab === "connections" && (
          <Section
            title="Credentials"
            desc="Which key each agent authenticates with. A wave-tagged key is a wave artifact — End wave sweeps those and never a hand-minted one."
          >
            {(data?.credentials ?? []).length === 0 ? (
              <Empty>No credentials reach this project yet.</Empty>
            ) : (
              <div className="space-y-1.5">
                {/* Revoked credentials collapse the same way seats do. After ending wave-1 the
                    list was 15 dead keys deep and the one credential still in use — the
                    operator's own — was lost in it. */}
                {expiredCreds.length > 0 && (
                  <div className="flex justify-end">
                    <button onClick={clearExpiredCredentials}
                            className="text-[11px] text-muted hover:text-[color:var(--color-st-blocked)]">
                      Revoke the {expiredCreds.length} expired
                    </button>
                  </div>
                )}
                {liveCreds.length > 0 && deadCreds.length > 0 && (
                  <div className="flex justify-end">
                    <button onClick={() => setShowDeadCreds((v) => !v)}
                            className="text-[11px] text-faint hover:text-fg-2">
                      {showDeadCreds ? "Hide" : "Show"} {deadCreds.length} revoked
                    </button>
                  </div>
                )}
                {(showDeadCreds ? data!.credentials : liveCreds).map((c) => (
                  <div key={c.id}
                       className={cn("flex items-center gap-3 rounded-[9px] border border-line-2 bg-surface-2 px-3 py-2",
                                     c.revoked && "opacity-50")}>
                    <span className="font-mono text-[11px] text-muted-2">{c.prefix}</span>
                    <span className="min-w-0 flex-1 truncate text-[12px] text-fg-2">{c.name}</span>
                    {c.posture === "single" && (
                      <span className="font-mono text-[10px] text-faint">single</span>
                    )}
                    {c.wave
                      ? <span className="font-mono text-[10px] text-[color:var(--color-st-review)]">{c.wave}</span>
                      : <span className="font-mono text-[10px] text-faint">yours · never swept</span>}
                    <span className="font-mono text-[10px] text-faint">
                      {c.agents} agent{c.agents === 1 ? "" : "s"}
                    </span>
                    {c.revoked
                      ? <span className="font-mono text-[10px] text-faint">revoked</span>
                      : <button onClick={() => revokeCredential(c.id)}
                                className="text-[11px] text-muted hover:text-[color:var(--color-st-blocked)]">
                          Revoke
                        </button>}
                  </div>
                ))}
              </div>
            )}
          </Section>
        )}

        {tab === "work" && (
          <>
        <Section title="Review queue" desc="Who built each item — the reason it needs somebody else.">
          {(data?.review_queue ?? []).length === 0 ? (
            <Empty>Nothing waiting for a second pair of eyes.</Empty>
          ) : (
            <div className="space-y-2">
              {data!.review_queue.map((r) => (
                <div key={r.id} className="flex items-center gap-3 rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
                  <span className="font-mono text-[11px] text-faint">{r.key}</span>
                  <span className="min-w-0 flex-1 truncate text-[13px] text-fg-2">{r.title}</span>
                  {/* The ban rendered as a NEGATIVE on the item, not as a list of who is
                      eligible. The refusal belongs to the item, and saying it this way makes
                      the invariant legible at a glance. */}
                  {r.built_by_label && (
                    <span className="text-[11px] text-[color:var(--color-st-blocked)]">
                      {r.built_by_label} built it
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title="Clusters" desc="Non-colliding work, and why anything held back is waiting.">
          {(data?.clusters ?? []).length === 0 ? (
            <Empty>No ready work to partition.</Empty>
          ) : (
            <div className="space-y-2">
              {data!.clusters.map((c, i) => (
                <div key={i} className="rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-faint">{c.items.join(" · ") || "—"}</span>
                    {c.predicted && (
                      <span className="rounded-md border border-line-2 px-1.5 py-0.5 text-[10px] text-muted">
                        predicted areas
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 font-mono text-[10.5px] text-faint">{c.areas.join(", ")}</div>
                  {/* Without the reason a queued cluster looks like the fleet being stuck, and
                      a human overrides the divvy. With it, they trust it. */}
                  {c.held_by && (
                    <div className="mt-1 text-[11px] text-[color:var(--color-st-blocked)]">
                      collides on {c.blocked_on} — queued until {c.held_by} releases
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
          </>
        )}
      </div>
    </div>
  );
}

function Section({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <section className="mb-7">
      <h2 className="text-[14px] font-semibold tracking-tight">{title}</h2>
      <p className="mb-3 mt-0.5 text-[12px] text-muted">{desc}</p>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-[11px] border border-dashed border-line-2 px-3 py-6 text-center text-[12.5px] text-muted">
      <Users size={16} className="mx-auto mb-2 opacity-50" />
      {children}
    </div>
  );
}
