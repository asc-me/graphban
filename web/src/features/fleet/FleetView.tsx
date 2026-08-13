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
import { cursorWaveConfig, WAVE_ROLES, type WaveRole } from "./wave";
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

function primeSnippet(role: string) {
  // The role prompt, rendered short enough to paste. `register_agent` first is the load-bearing
  // line: an agent that claims before registering is invisible to the roster and ungoverned by
  // the role gate.
  if (role === ALL_IN_ONE) {
    // The DEFAULT posture, and its prompt says so plainly: no fleet, no server-side review
    // gate, and the human is the reviewer. Priming it like a worker would imply a reviewer
    // that is not there and a gate that does not apply.
    return [
      "You are the only agent on this Graphban project. You do everything: plan, build, and",
      "record evidence. There is no reviewer agent — the human reviews your work.",
      "Call register_agent first, then heartbeat on the interval it returns, so you appear",
      "on the roster. claim_next for work; move items to done yourself when they are done.",
    ].join("\n");
  }
  const duty =
    role === "planner" ? "Read collision_clusters and allocate. Do not claim work yourself."
    : role === "reviewer" ? "Take work with claim_review — never your own — then sign_off or bounce with a reason."
    : "claim_cluster, work it, write actual touchpoints back with update_item, then move it to review.";
  return `You are a ${role} in a Graphban fleet.\nCall register_agent first, then heartbeat on the interval it returns.\n${duty}`;
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

function AgentRow({ a }: { a: FleetAgent }) {
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
      </div>
      <div className="w-40 text-right text-[11px] text-muted">
        {a.holdings.length === 0
          ? <span className="text-faint">holding nothing</span>
          : a.holdings.map((h) => <div key={h.id} className="truncate">{h.id}</div>)}
      </div>
    </div>
  );
}

export function FleetView() {
  const { activeId } = useProjectCtx();
  const { data, refetch } = useFleet(activeId);
  const [role, setRole] = React.useState("worker");
  const [minted, setMinted] = React.useState<{ plaintext: string; role: string } | null>(null);
  // Held in component state ONLY, and never written anywhere: these are plaintext credentials,
  // shown once, exactly like the single-role mint above.
  const [waveKeys, setWaveKeys] = React.useState<Partial<Record<WaveRole, string>> | null>(null);
  const [minting, setMinting] = React.useState(false);
  const [error, setError] = React.useState("");
  const [confirming, setConfirming] = React.useState<null | {
    keys: number; agents: number; leases: number; reservations: number;
  }>(null);
  const wave = "wave-1";

  async function mint() {
    setError("");
    try {
      const out = await api.mintFleetKey({ project_id: activeId, role, wave });
      setMinted({ plaintext: out.plaintext, role: out.role });
    } catch (e) {
      setError(errorDetail(e, "could not mint a fleet credential"));
    }
  }

  async function mintWave() {
    setError("");
    setMinting(true);
    try {
      // Sequential rather than parallel: each mint allocates from the project's key sequence,
      // and a failure half-way should leave the roles already issued visible rather than
      // discarding credentials that now exist server-side and cannot be re-shown.
      const out: Partial<Record<WaveRole, string>> = {};
      for (const r of WAVE_ROLES) {
        const k = await api.mintFleetKey({ project_id: activeId, role: r, wave });
        out[r] = k.plaintext;
        setWaveKeys({ ...out });
      }
    } catch (e) {
      setError(errorDetail(e, "could not mint the wave"));
    } finally {
      setMinting(false);
    }
  }

  async function askEndWave() {
    setError("");
    try {
      // Name the damage BEFORE acting. "Are you sure?" teaches people to click through;
      // "revoke 4 keys, release 3 leases?" is a decision.
      setConfirming(await api.endWavePreview(activeId, wave));
    } catch (e) {
      setError(errorDetail(e, "could not read the wave"));
    }
  }

  async function doEndWave() {
    try {
      await api.endWave(activeId, wave);
      setConfirming(null);
      setMinted(null);
      setWaveKeys(null);
      await refetch();
    } catch (e) {
      setError(errorDetail(e, "could not end the wave"));
    }
  }

  const agents = data?.agents ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center justify-between border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Fleet</h1>
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
        {(data?.total ?? 0) > 0 && (
          <Button onClick={askEndWave}>End wave</Button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {error && <p className="mb-3 text-[12px] text-red-400">{error}</p>}

        {confirming && (
          <div className="mb-5 rounded-[11px] border border-[color:var(--color-st-blocked)]/50 bg-surface-2 p-4">
            <p className="text-[13px] text-fg-2">
              Revoke {confirming.keys} key{confirming.keys === 1 ? "" : "s"},
              release {confirming.leases} lease{confirming.leases === 1 ? "" : "s"} and{" "}
              {confirming.reservations} reservation{confirming.reservations === 1 ? "" : "s"}?
            </p>
            <p className="mt-1 text-[12px] text-muted">
              Hand-minted keys are untouched. This cannot be undone.
            </p>
            <div className="mt-3 flex gap-2">
              <Button onClick={doEndWave}>End the wave</Button>
              <Button onClick={() => setConfirming(null)}>Cancel</Button>
            </div>
          </div>
        )}

        <Section title="Roster" desc="Offline agents fade rather than vanish — one that died holding a branch is what you need to see.">
          {agents.length === 0 ? (
            <Empty>No agents yet. Mint a credential below and paste it into a terminal.</Empty>
          ) : (
            <div className="space-y-2">{agents.map((a) => <AgentRow key={a.id} a={a} />)}</div>
          )}
        </Section>

        <Section title="Onboard an agent" desc="Pick a role and a client; paste the two snippets. That is the whole of it.">
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
              <CopyRow label="3. Prime" value={primeSnippet(minted.role)} />
            </div>
          )}
        </Section>

        <Section
          title="Provision a whole wave"
          desc="For a client that stores ONE MCP config and reuses it — Cursor, notably. Three servers, three role-narrowed keys, one file."
        >
          {/* The problem this exists for: Cursor has no per-agent MCP scoping, so every agent
              shares one credential — and `independent()` then refuses every review, because
              author and reviewer match on both key and host. Per-worktree config files would
              fix it and cost four setups per wave, which nobody will do twice. Interpolating
              env vars into the header moves the per-wave part OUT of the file: the config is
              written once and only the three values rotate. */}
          <Button onClick={mintWave} disabled={minting}>
            {minting ? "Minting…" : "Mint a wave — planner, worker, reviewer"}
          </Button>
          {waveKeys && (
            <div className="mt-3 space-y-2">
              <CopyRow
                label="~/.cursor/mcp.json — replaces the file, keys included (shown once)"
                value={cursorWaveConfig(`${window.location.origin}/api/mcp`, waveKeys)}
              />
              <p className="px-1 text-[11px] text-faint">
                Each server carries a role-narrowed key, so a worker reaching for{" "}
                <span className="font-mono">sign_off</span> is refused, and a reviewer signing a
                worker&apos;s item is independent because the credentials differ. Cursor cannot
                scope a server to one agent, so an agent that deliberately switches servers can
                still sign its own work — the default is safe, the ceiling is not absolute.
              </p>
              <p className="px-1 text-[11px] text-faint">
                The keys are literal because Cursor does not interpolate environment variables
                here — probed, with the variables present: the entry is silently dropped rather
                than sent. So this file is regenerated each wave rather than written once.
              </p>
            </div>
          )}
        </Section>

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
