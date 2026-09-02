import { Info, Pencil } from "lucide-react";
import * as React from "react";

import { useDeployments, useOrgs } from "@/lib/queries";
import type { Deployment } from "@/lib/types";

/**
 * Screen 4 — Linked deployments (PRD-21 D6).
 *
 * **Nothing here reaches into a box.** Every field is already cloud-held: a linked
 * deployment runs only the code-graph tools locally and forwards its claims, leases and
 * heartbeats, so "which agents are running, on what" is a query. Relay and reverse tunnel
 * were rejected rather than deferred — the box pushes, the cloud never reaches in.
 *
 * The address is the one thing the cloud cannot know, so it is **rendered as text and then
 * linked**, never as a bare "Open deployment" button. The console cannot test reachability
 * — that is the viewer's network, and a cross-origin probe would hang or be blocked — so a
 * button that dead-ends in a connection error is worse than no button, while a visible
 * `http://ubuntu-srv:8080` tells someone on the wrong network everything before they click.
 */
const OVERRIDE_KEY = "gb_deployment_url_overrides";

function readOverrides(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(OVERRIDE_KEY) || "{}");
  } catch {
    return {};
  }
}

export function OrgDeployments() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: deployments = [], isLoading } = useDeployments(org?.id);
  const [overrides, setOverrides] = React.useState<Record<string, string>>(readOverrides);

  const setOverride = (id: string, url: string) => {
    const next = { ...overrides };
    if (url.trim()) next[id] = url.trim();
    else delete next[id];
    setOverrides(next);
    try {
      localStorage.setItem(OVERRIDE_KEY, JSON.stringify(next));
    } catch {
      // Private mode: the override lives for this session only, which is still better
      // than forcing everyone onto an address that is wrong from their network.
    }
  };

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <p className="mb-4 max-w-[80ch] text-[12.5px] leading-relaxed text-muted">
        Local deployments push their code graph here using a link key.{" "}
        <span className="text-fg-2">The key is the deployment's identity</span> — one
        key, one box — so the name you give it is the name you will see here. Same object
        API keys and Sync / Link mint.
      </p>

      <div className="mb-4 flex gap-2.5 rounded-[10px] border border-line bg-surface px-3 py-2.5">
        <Info size={13} className="mt-0.5 shrink-0 text-faint" />
        <span className="max-w-[80ch] text-[11.5px] leading-relaxed text-muted">
          Everything below is already held here — a linked box forwards its claims and
          leases, so this is a query, not a window into your machine. Summaries and
          structure only; vectors are re-embedded on arrival.
        </span>
      </div>

      {isLoading ? (
        <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8 text-center font-mono text-[11px] text-faint-2">
          loading…
        </div>
      ) : deployments.length === 0 ? (
        <NoDeployments />
      ) : (
        <div className="flex flex-col gap-3">
          {deployments.map((d) => (
            <DeploymentCard
              key={d.credential_id}
              deployment={d}
              override={overrides[d.credential_id] ?? ""}
              onOverride={(url) => setOverride(d.credential_id, url)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function NoDeployments() {
  return (
    <div className="rounded-[13px] border border-line bg-surface-2 px-5 py-8">
      <div className="text-[15px] font-semibold">Nothing is linked yet</div>
      <p className="mt-2 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
        No link key has been minted for this org's projects, so no local box is
        pushing a code graph here. Mint one from API keys → Link key, or Settings →
        Sync / Link, then run{" "}
        <code className="font-mono text-[11.5px] text-fg-2">graphban link</code> on the
        machine that has the checkout.
      </p>
    </div>
  );
}

const FRESHNESS: Record<Deployment["freshness"], { label: string; tone: string; note: string }> = {
  in_sync: {
    label: "in sync",
    tone: "text-st-done border-st-done/30 bg-st-done/[0.08]",
    note: "",
  },
  stale: {
    label: "stale",
    tone: "text-st-review border-st-review/30 bg-st-review/[0.08]",
    note: "Pushed before, not recently — the box stopped, or its link key no longer works.",
  },
  // Deliberately its own state. A link somebody set up and never finished asks for a
  // different action from a box that stopped, so it must not read as staleness.
  never: {
    label: "never pushed",
    tone: "text-faint border-line",
    note: "This link key exists but has never been used. The link was set up and not finished.",
  },
};

function DeploymentCard({
  deployment: d,
  override,
  onOverride,
}: {
  deployment: Deployment;
  override: string;
  onOverride: (url: string) => void;
}) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(override);
  const fresh = FRESHNESS[d.freshness];
  const address = override || d.base_url;
  const live = d.agents.filter((a) => a.state !== "offline");

  return (
    <section
      className={`rounded-[13px] border bg-surface-2 p-4 ${
        d.revoked ? "border-dashed border-line opacity-60" : "border-line"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-[14px] font-semibold">{d.label}</span>
        <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[10px] text-muted">
          {d.prefix}…
        </span>
        <span className="font-mono text-[10.5px] text-faint">→ {d.project_tag}</span>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.05em] ${fresh.tone}`}
        >
          <span className="h-[5px] w-[5px] rounded-full bg-current" />
          {fresh.label}
        </span>
        {d.revoked && (
          <span className="rounded-full border border-line px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.05em] text-faint">
            retired
          </span>
        )}
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-muted">
          {d.node_count.toLocaleString()} node{d.node_count === 1 ? "" : "s"}
        </span>
      </div>

      {fresh.note && (
        <p className="mt-2 text-[11.5px] leading-relaxed text-muted">{fresh.note}</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2.5">
        <span className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          ADDRESS
        </span>
        {editing ? (
          <>
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={d.base_url || "http://localhost:8080"}
              aria-label={`Address override for ${d.label}`}
              className="h-[26px] min-w-[240px] rounded-md border border-line-2 bg-surface px-2 font-mono text-[11.5px] outline-none"
            />
            <button
              onClick={() => {
                onOverride(draft);
                setEditing(false);
              }}
              className="h-[26px] rounded-md border border-accent/35 bg-accent/[0.12] px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-accent"
            >
              Save
            </button>
          </>
        ) : address ? (
          <>
            {/* Text first, then a link on it. The console cannot probe the address — that
                is the viewer's network — so showing it plainly lets someone on the wrong
                one see the problem before they click. */}
            <a
              href={address}
              target="_blank"
              rel="noreferrer"
              className="font-mono text-[11.5px] text-st-next underline underline-offset-2"
            >
              {address}
            </a>
            {override && (
              <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.05em] text-faint">
                your override
              </span>
            )}
            <button
              onClick={() => {
                setDraft(override);
                setEditing(true);
              }}
              aria-label={`Edit address for ${d.label}`}
              className="text-faint hover:text-fg-2"
            >
              <Pencil size={12} />
            </button>
          </>
        ) : (
          <>
            <span className="text-[11.5px] text-muted">
              not reported — this deployment has not told the cloud where it answers
            </span>
            <button
              onClick={() => setEditing(true)}
              className="h-[24px] rounded-md border border-line-2 px-2 font-mono text-[9px] uppercase tracking-[0.05em] text-muted hover:text-fg"
            >
              Set one
            </button>
          </>
        )}
      </div>

      <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
        Self-reported by the box, and a hint rather than a guarantee — the same machine
        answers at different addresses from different networks. An override is stored for
        you alone.
      </p>

      <div className="mt-3 border-t border-line pt-3">
        <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          AGENTS ON {d.project_tag}
        </div>
        {d.agents.length === 0 ? (
          <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">
            Nobody is working this project. That is idle — the cloud would know if anyone
            were, because a linked box forwards its claims here.
          </p>
        ) : (
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {d.agents.map((a) => (
              <span
                key={a.key}
                className={`inline-flex items-center gap-1.5 rounded-[5px] border px-1.5 py-px ${
                  a.state === "offline" ? "border-line opacity-60" : "border-line-2"
                }`}
              >
                <span className="font-mono text-[10.5px] text-fg-2">{a.label || a.key}</span>
                <span className="font-mono text-[9px] uppercase text-faint">{a.role}</span>
              </span>
            ))}
            <span className="font-mono text-[10px] text-faint">
              {live.length} live of {d.agents.length}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
