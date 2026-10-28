import { Server } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import {
  CardGridSkeleton,
  FETCH_FAILED,
  PlannerError,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { copyText } from "@/lib/clipboard";
import { useConfig, useFleet } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import { cn } from "@/lib/cn";
import {
  FLEET_ADAPTERS,
  INSTALL_CLI,
  INSTALL_CLI_BREW,
  INSTALL_SUPERVISOR,
  type FleetAdapter,
} from "./install";
import { groupOutposts, hostHint, type Outpost } from "./outposts";

/**
 * Connected gban/gbfleet hosts and what they last declared (GRPH-866, PRD-47 S7).
 *
 * There is no outpost table: a host is whoever registered an agent with a `host`
 * capability or a `label` of the form `model @ host`. Unspecified is a real group,
 * never silently called localhost. Tool versions are what the host last declared —
 * and are omitted when nothing declared one.
 */
export function OutpostsView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const { data: config } = useConfig();
  const fleetQ = useFleet(activeId);
  const { data, isLoading } = fleetQ;
  const [tab, setTab] = useState<"hosts" | "setup">("hosts");
  const fleetHref = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet")
    : "/fleet";
  const catalogHref = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet?tab=harness")
    : "/fleet?tab=harness";

  const outposts = groupOutposts(data?.agents ?? []);
  const hasHosts = outposts.length > 0;
  const online = outposts.reduce((n, p) => n + p.online, 0);
  const total = outposts.reduce((n, p) => n + p.agents.length, 0);
  const runningFleet = outposts.filter((p) => p.agents.some((a) => a.via === "gbfleet")).length;
  const gbagentModels = [...new Set(
    outposts.flatMap((p) => p.agents.filter((a) => /gbagent/i.test(a.label) || a.vendor === "gbagent").map((a) => a.model).filter(Boolean)),
  )];

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="outposts-view">
      <PlaceHeader
        viewName={`Outposts ${scope}`}
        purpose="The machines your agents run on, what each has installed, and how to set up another."
        action={
          <Link to={catalogHref} className="text-[12px] text-muted transition-colors hover:text-fg-2">
            Fleet catalog
          </Link>
        }
      />
      <div
        role="tablist"
        aria-label="Outposts"
        className="sticky top-0 z-[2] flex flex-none items-stretch gap-1 border-b border-line bg-surface px-5"
      >
        <OpTab
          selected={tab === "hosts"}
          onClick={() => setTab("hosts")}
          label="Hosts"
          meta={hasHosts ? `${outposts.length} checked in` : "none yet"}
        />
        <OpTab
          selected={tab === "setup"}
          onClick={() => setTab("setup")}
          label="Set up a host"
          meta="gban · gbfleet · gbagent"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {fleetQ.isError && !data ? (
          <PlannerError message={FETCH_FAILED} onRetry={() => void fleetQ.refetch()} />
        ) : isLoading || !data ? (
          <div className="p-5"><CardGridSkeleton cards={3} /></div>
        ) : tab === "hosts" ? (
          <HostsTab
            outposts={outposts}
            stats={[
              { k: "Hosts", v: String(outposts.length) },
              { k: "Agents online", v: total ? `${online} of ${total}` : "0" },
              { k: "Running gbfleet", v: String(runningFleet) },
              { k: "gbagent models", v: gbagentModels.join(", ") || "—" },
            ]}
            fleetHref={fleetHref}
            onSetup={() => setTab("setup")}
          />
        ) : (
          <SetupTab />
        )}
      </div>
    </div>
  );
}

function OpTab({
  selected, onClick, label, meta,
}: {
  selected: boolean; onClick: () => void; label: string; meta: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected}
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 border-b-2 px-3 py-2.5 text-body font-medium",
        selected ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
      )}
    >
      {label}
      <span className={cn("font-mono text-meta font-normal", selected ? "text-muted" : "text-faint")}>
        {meta}
      </span>
    </button>
  );
}

function HostsTab({
  outposts, stats, fleetHref, onSetup,
}: {
  outposts: Outpost[];
  stats: { k: string; v: string }[];
  fleetHref: string;
  onSetup: () => void;
}) {
  if (outposts.length === 0) {
    return (
      <div
        className="mx-auto mt-12 flex max-w-lg flex-col items-center gap-2.5 px-5 text-center"
        data-testid="outposts-empty"
      >
        <div className="flex h-10 w-10 items-center justify-center rounded-[12px] border border-dashed border-control">
          <Server size={15} className="text-muted" />
        </div>
        <div className="text-lead font-semibold text-fg-2">No host has checked in</div>
        <p className="text-body leading-relaxed text-muted">
          That&apos;s not a list of zero machines. Nothing has registered yet. Install gban on a
          machine and it appears here the first time an agent on it calls register_agent.
        </p>
        <button
          type="button"
          onClick={onSetup}
          className="mt-1 h-8 rounded-[9px] bg-accent px-3.5 text-body font-semibold text-[#0a0c0e] hover:bg-accent/90"
        >
          Set up a host
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-[1080px] flex-col gap-3.5 p-5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-[10px] border border-line bg-surface px-3 py-2">
        {stats.map((x) => (
          <span key={x.k} className="text-small text-muted">
            {x.k}{" "}
            <span className="font-mono text-fg-2">{x.v}</span>
          </span>
        ))}
        <span className="flex-1" />
        <button type="button" onClick={onSetup} className="border-0 bg-transparent p-0 text-small text-accent">
          Add a host →
        </button>
      </div>
      {outposts.map((post) => (
        <OutpostCard key={post.host} post={post} fleetHref={fleetHref} />
      ))}
      <HostDefinitionFootnote />
    </div>
  );
}

function OutpostCard({ post, fleetHref }: { post: Outpost; fleetHref: string }) {
  const hint = hostHint(post);
  return (
    <section
      className="overflow-hidden rounded-[12px] border border-line-2 bg-surface-2"
      data-testid="outpost-card"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-3.5 py-3">
        <span className="flex h-[26px] w-[26px] items-center justify-center rounded-lg border border-line-2 bg-surface">
          <Server size={13} className="text-muted" />
        </span>
        <h2 className={cn("font-mono text-body font-medium", post.specified ? "text-fg-2" : "text-muted")}>
          {post.specified ? post.host : "unspecified host"}
        </h2>
        <span className="font-mono text-meta text-faint">{post.os}</span>
        <span className="flex-1" />
        <span className="font-mono text-small text-muted">
          {post.online} of {post.agents.length} online
        </span>
      </div>
      {hint && (
        <div
          className="flex flex-wrap items-center gap-2 border-b border-line bg-[rgba(224,179,74,0.04)] px-3.5 py-2 text-small text-[color:var(--color-st-review)]"
          data-testid="fix-hint"
        >
          <span className="min-w-0 flex-1">{hint.text}</span>
          {hint.fixTo === "fleet" && (
            <Link to={fleetHref} className="text-accent">{hint.fixLabel}</Link>
          )}
          {hint.fixTo === "setup" && hint.fixLabel && (
            <span className="text-accent">{hint.fixLabel}</span>
          )}
        </div>
      )}
      <div className="flex flex-col">
        {post.agents.map((agent) => (
          <div
            key={agent.id}
            className={cn(
              "grid grid-cols-[48px_minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1.2fr)_64px] items-center gap-2.5 border-b border-line px-3.5 py-2 last:border-b-0",
              agent.state === "offline" && "opacity-60",
            )}
          >
            <span className="font-mono text-small text-fg-2">{agent.key}</span>
            <span className="min-w-0">
              <span className="block truncate text-[12px] text-fg-2">{agent.model || agent.label || "no label"}</span>
              <span className="block font-mono text-meta text-faint">{agent.via}</span>
            </span>
            <span className="truncate font-mono text-meta text-muted">
              {[agent.vendor, agent.tier].filter(Boolean).join(" · ") || "—"}
            </span>
            <span className="truncate font-mono text-meta text-faint">
              {agent.branch || agent.worktree || "no worktree"}
            </span>
            <span className={cn(
              "justify-self-end font-mono text-meta uppercase",
              agent.state === "offline" ? "text-faint" : "text-st-done",
            )}>
              {agent.state}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function HostDefinitionFootnote() {
  return (
    <p className="max-w-[96ch] text-small leading-relaxed text-faint" data-testid="host-footnote">
      A host is whoever registered an agent with a host capability or a label of the form
      model @ host. &quot;Unspecified&quot; is a real group, never silently localhost.
      Tool versions are what the host last declared.
    </p>
  );
}

function SetupTab() {
  return (
    <div className="mx-auto flex max-w-[1080px] flex-col gap-6 p-5">
      <ThreeToolsSection />
      <SetUpThisMachineSection />
    </div>
  );
}

const LAYERS = [
  {
    n: "01", name: "gban", pkg: "graphban-cli", color: "text-[#7ca2ff]",
    what: "The Graphban CLI. Everything the web app does, from a terminal. No dependencies, so it's safe on any laptop.",
    unlocks: [
      "Log in once; every other tool reuses the credentials",
      "Issue seats, read why an agent is stuck, re-task one",
      "Registers this machine as an outpost",
    ],
    where: "Every machine that touches the project",
  },
  {
    n: "02", name: "gbfleet", pkg: "graphban-fleet", color: "text-accent",
    what: "The supervisor. Reads a seats file, spawns one agent per seat in its own git worktree, and reaps them when the wave ends.",
    unlocks: [
      "Runs claude, cursor-agent, grok or gbagent as adapters",
      "Cuts a worktree and branch per seat",
      "gbfleet doctor checks adapters, git and the server",
    ],
    where: "Only machines that run waves",
  },
  {
    n: "03", name: "gbagent", pkg: "ships with gbfleet", color: "text-[#c9b8ff]",
    what: "The local-model agent. Runs an open model on this machine, so a wave can include agents that never leave it.",
    unlocks: [
      "Adapter name gbagent in gbfleet up",
      "Shows in Harness as gbagent:<model>",
      "Honours the project's local-only policy",
    ],
    where: "Machines with a GPU or a local model server",
  },
];

function ThreeToolsSection() {
  return (
    <section data-testid="three-tools">
      <h2 className="text-lead font-semibold text-fg">Three tools, one machine</h2>
      <p className="mb-3 mt-0.5 max-w-[92ch] text-[12px] text-muted">
        Start with gban. Add gbfleet only on machines that run waves. gbagent ships with
        gbfleet, so there&apos;s nothing extra to install.
      </p>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))] gap-2.5">
        {LAYERS.map((l) => (
          <div key={l.name} className="flex flex-col gap-2 rounded-[12px] border border-line-2 bg-surface-2 p-3.5">
            <div className="flex items-center gap-2">
              <span className="font-mono text-meta text-faint">{l.n}</span>
              <span className={cn("font-mono text-lead", l.color)}>{l.name}</span>
              <span className="ml-auto font-mono text-micro text-faint">{l.pkg}</span>
            </div>
            <p className="text-[12px] leading-relaxed text-fg-2">{l.what}</p>
            <ul className="flex flex-col gap-0.5">
              {l.unlocks.map((u) => (
                <li key={u} className="flex gap-1.5 text-small leading-relaxed text-muted">
                  <span className={l.color}>→</span>
                  <span>{u}</span>
                </li>
              ))}
            </ul>
            <span className="mt-auto border-t border-line pt-1.5 text-small text-faint">{l.where}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function agentCmd(adapter: FleetAdapter): string {
  if (adapter === "gbagent") {
    return "gbagent --model qwen3-coder --mcp-config {seat_file} -p {instruction_file}";
  }
  if (adapter === "cursor-agent") {
    return "cursor-agent --mcp-config {seat_file} -p {instruction_file}";
  }
  return `${adapter} --mcp-config {seat_file} -p {instruction_file}`;
}

function SetUpThisMachineSection() {
  const [install, setInstall] = useState<"uv" | "brew">("uv");
  const [adapter, setAdapter] = useState<FleetAdapter>("claude");
  const origin = typeof window !== "undefined" ? window.location.origin : "";

  const steps = [
    {
      tool: "gban",
      h: "Install gban",
      b: "The CLI on its own. Upgrading uses the same command.",
      cmd: install === "brew" ? INSTALL_CLI_BREW : INSTALL_CLI,
      optional: "",
      out: "",
    },
    {
      tool: "gban",
      h: "Log in",
      b: "Stores the session where gbfleet can read it. Name this machine when you register — there is no host-set command; Outposts groups by register_agent(label=\"<model> @ <host>\").",
      cmd: `gban login --server ${origin}`,
      optional: "",
      out: "",
    },
    {
      tool: "gbfleet",
      h: "Add the supervisor",
      optional: "only if this machine runs waves",
      b: "gbfleet isn't in the Homebrew tap. It's resolved on PATH by whatever launches it, so install it with uv. gbagent comes with it.",
      cmd: INSTALL_SUPERVISOR,
      out: "",
    },
    {
      tool: "gbfleet",
      h: "Check the machine",
      b: "Confirms git, the adapters on PATH, and that the server accepts the key. gban doctor runs the same local half.",
      cmd: "gbfleet doctor",
      optional: "",
      out: "",
    },
    {
      tool: "gbfleet",
      h: "Run a wave",
      b: `Save the seats from Fleet → Wave as seats.txt, one code per line. gbfleet spawns one ${adapter} per seat and reaps them all on End wave.`,
      cmd:
        `gbfleet up --server ${origin} --seats-file seats.txt \\\n` +
        `  --adapter ${adapter} --wave wave-1 -- ${agentCmd(adapter)}`,
      optional: "",
      out: "",
    },
  ];

  return (
    <section className="overflow-hidden rounded-[12px] border border-line-2 bg-surface-2" data-testid="set-up-machine">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-b border-line px-4 py-3">
        <h2 className="text-lead font-semibold text-fg">Set up this machine</h2>
        <span className="flex-1" />
        <span className="font-mono text-micro uppercase tracking-wide text-faint">Install with</span>
        <div className="flex overflow-hidden rounded-lg border border-control" data-testid="install-method-picker">
          {(["uv", "brew"] as const).map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => setInstall(id)}
              aria-pressed={install === id}
              className={cn(
                "h-7 border-0 px-2.5 text-small",
                install === id ? "bg-surface-3 text-fg" : "bg-transparent text-faint",
              )}
            >
              {id === "brew" ? "Homebrew" : "uv"}
            </button>
          ))}
        </div>
        <span className="font-mono text-micro uppercase tracking-wide text-faint">Adapter</span>
        <div className="flex overflow-hidden rounded-lg border border-control" data-testid="adapter-picker">
          {FLEET_ADAPTERS.map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => setAdapter(id)}
              aria-pressed={adapter === id}
              className={cn(
                "h-7 border-0 px-2 font-mono text-meta",
                adapter === id ? "bg-surface-3 text-fg" : "bg-transparent text-faint",
              )}
            >
              {id}
            </button>
          ))}
        </div>
      </div>
      <ol className="m-0 list-none p-0">
        {steps.map((st, i) => (
          <SetupStep key={st.h} n={String(i + 1)} {...st} />
        ))}
      </ol>
      <p className="px-4 py-2.5 text-small leading-relaxed text-faint">
        Everything on the web has a gban equivalent, so you rarely need to come back here once a host is set up.
      </p>
    </section>
  );
}

function SetupStep({
  n, h, b, cmd, tool, optional, out,
}: {
  n: string; h: string; b: string; cmd: string; tool: string; optional: string; out: string;
}) {
  const [copied, setCopied] = useState(false);
  const toolTone = tool === "gban" ? "text-[#7ca2ff] border-[#7ca2ff]/40"
    : tool === "gbagent" ? "text-[#c9b8ff] border-[#a78bfa]/40"
    : "text-accent border-accent/40";
  return (
    <li className="grid grid-cols-[32px_minmax(0,1fr)] gap-3 border-b border-line px-4 py-3.5">
      <span className="flex h-6 w-6 items-center justify-center rounded-md border border-accent/40 bg-accent/10 font-mono text-meta text-accent">
        {n}
      </span>
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-body font-semibold text-fg">{h}</span>
          <span className={cn("rounded border px-1.5 py-px font-mono text-micro tracking-wide", toolTone)}>
            {tool}
          </span>
          {optional && <span className="text-small text-faint">{optional}</span>}
        </div>
        <p className="max-w-[92ch] text-[12px] leading-relaxed text-muted">{b}</p>
        <div className="relative rounded-[9px] border border-line bg-surface">
          <pre className="overflow-x-auto px-3 py-2.5 pr-[72px] font-mono text-[12px] leading-relaxed text-fg-2">
            {cmd}
          </pre>
          <button
            type="button"
            onClick={() => { void copyText(cmd).then((ok) => { if (ok) { setCopied(true); setTimeout(() => setCopied(false), 1500); } }); }}
            className="absolute right-2 top-1.5 h-6 rounded-md border border-control bg-surface-2 px-2 text-meta text-muted hover:text-fg"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        {out && (
          <pre className="overflow-x-auto border-l-2 border-line-2 px-3 py-2 font-mono text-small leading-relaxed text-faint">
            {out}
          </pre>
        )}
      </div>
    </li>
  );
}
