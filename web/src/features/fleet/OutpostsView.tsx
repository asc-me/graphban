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
import { useConfig, useFleet } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { groupOutposts } from "./outposts";

const INSTALL_METHODS = [
  { id: "npm", label: "npm", steps: [
    { cmd: "npm install -g @graphban/cli", expect: "gban available on PATH" },
    { cmd: "gban init", expect: "Project linked, .gbagent.toml written" },
  ]},
  { id: "docker", label: "Docker", steps: [
    { cmd: "docker pull graphban/gban:latest", expect: "Image pulled" },
    { cmd: "docker run -it graphban/gban gban init", expect: "Project linked inside container" },
  ]},
  { id: "source", label: "From source", steps: [
    { cmd: "git clone https://github.com/graphban/cli.git && cd cli", expect: "Repo cloned" },
    { cmd: "npm install && npm run build", expect: "Build complete" },
    { cmd: "npm link", expect: "gban available on PATH" },
  ]},
];

const ADAPTERS = [
  { id: "qwen", label: "Qwen Code", steps: [
    { cmd: "gban adapter install qwen", expect: "Adapter registered" },
    { cmd: "gban adapter configure qwen --model qwen-max", expect: "Model set" },
  ]},
  { id: "claude", label: "Claude Code", steps: [
    { cmd: "gban adapter install claude", expect: "Adapter registered" },
    { cmd: "gban adapter configure claude --model opus", expect: "Model set" },
  ]},
  { id: "codex", label: "Codex", steps: [
    { cmd: "gban adapter install codex", expect: "Adapter registered" },
  ]},
];

/**
 * Connected gban/gbfleet hosts and what they last declared (GRPH-866).
 *
 * There is no outpost table: a host is whoever registered an agent with a `host`
 * capability or a `label` of the form `model @ host`. Unspecified is a real group,
 * never silently called localhost.
 */
export function OutpostsView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const { data: config } = useConfig();
  const fleetQ = useFleet(activeId);
  const { data, isLoading } = fleetQ;
  const fleetHref = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet.v2")
    : "/fleet.v2";

  const outposts = groupOutposts(data?.agents ?? []);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="outposts-view">
      <PlaceHeader
        viewName={`Outposts ${scope}`}
        purpose="Machines that have registered a gban/gbfleet agent on this project, and the harness they declared."
        action={
          <Link to={fleetHref} className="text-[12px] text-muted transition-colors hover:text-fg-2">
            Fleet catalog
          </Link>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {fleetQ.isError && !data ? (
          <PlannerError message={FETCH_FAILED} onRetry={() => void fleetQ.refetch()} />
        ) : isLoading || !data ? (
          <CardGridSkeleton cards={3} />
        ) : outposts.length === 0 ? (
          // The setup sections belong HERE most of all. They used to render only alongside
          // existing hosts, which put "Set up this machine" behind having already set one up —
          // the reader who needs it is exactly the reader who cannot see it. The design's empty
          // state says "Install gban on a machine and it appears here the first time an agent on
          // it calls register_agent", so the instructions are the empty state's whole job.
          // The host-definition footnote stays out: it explains how groups were formed, and
          // there are none.
          <div className="mx-auto flex max-w-3xl flex-col gap-3">
            <div
              className="mt-8 text-center text-[13px] text-muted"
              data-testid="outposts-empty"
            >
              <Server size={16} className="mx-auto mb-2 opacity-50" />
              No host has registered on this project. That is not a list of zero outposts —
              nothing has checked in.
            </div>
            <ThreeToolsSection />
            <SetUpThisMachineSection />
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-3">
            {outposts.map((post) => (
              <OutpostCard key={post.host} post={post} />
            ))}
            <HostDefinitionFootnote />
            <ThreeToolsSection />
            <SetUpThisMachineSection />
          </div>
        )}
      </div>
    </div>
  );
}

function OutpostCard({ post }: { post: ReturnType<typeof groupOutposts>[number] }) {
  const offlineCount = post.agents.filter((a) => a.state === "offline").length;
  const showFixHint = offlineCount > 0;

  return (
    <section
      className="rounded-[11px] border border-line-2 p-3"
      data-testid="outpost-card"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="font-mono text-[13px] font-medium">
          {post.specified ? post.host : "unspecified host"}
        </h2>
        <span className="font-mono text-[11px] text-faint">
          {post.online} of {post.agents.length} online
        </span>
      </div>
      {showFixHint && (
        <div className="mb-2 rounded-[7px] bg-surface-2 px-2.5 py-1.5 text-[11px] text-muted" data-testid="fix-hint">
          {offlineCount} agent{offlineCount !== 1 ? "s" : ""} offline. Check the machine is reachable and the agent process is running.
        </div>
      )}
      <div className="space-y-1.5">
        {post.agents.map((agent) => (
          <div
            key={agent.id}
            className={cn(
              "rounded-[9px] border border-line-2 bg-surface-2 px-3 py-2",
              agent.state === "offline" && "opacity-60",
            )}
          >
            <div className="flex flex-wrap items-center gap-2 text-[12px]">
              <span className="font-mono text-[11px]">{agent.key}</span>
              <span className="text-muted">{agent.label || "no label"}</span>
              <span className="ml-auto font-mono text-[10px] uppercase text-faint">
                {agent.state}
              </span>
            </div>
            <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted sm:grid-cols-3">
              <div><dt className="inline text-faint">vendor </dt><dd className="inline">{agent.vendor || "—"}</dd></div>
              <div><dt className="inline text-faint">model </dt><dd className="inline">{agent.model || "—"}</dd></div>
              <div><dt className="inline text-faint">tier </dt><dd className="inline">{agent.tier || "—"}</dd></div>
              <div><dt className="inline text-faint">via </dt><dd className="inline">{agent.via}</dd></div>
              <div><dt className="inline text-faint">os </dt><dd className="inline">{agent.os || "not declared"}</dd></div>
              <div className="truncate">
                <dt className="inline text-faint">branch </dt>
                <dd className="inline">{agent.branch || "—"}</dd>
              </div>
            </dl>
          </div>
        ))}
      </div>
    </section>
  );
}

function HostDefinitionFootnote() {
  return (
    <div className="rounded-[9px] border border-line-2 bg-surface-2 px-3 py-2 text-[11px] text-muted" data-testid="host-footnote">
      <p>
        A host is whoever registered an agent with a <code className="font-mono text-[10px]">host</code> capability
        or a label of the form <code className="font-mono text-[10px]">model @ host</code>.
        "Unspecified" is a real group, never silently localhost.
        Tool versions are what the host last declared.
      </p>
    </div>
  );
}

function ThreeToolsSection() {
  return (
    <section className="rounded-[11px] border border-line-2 p-3" data-testid="three-tools">
      <h3 className="mb-2 font-mono text-[12px] font-medium">Three tools, one machine</h3>
      <div className="space-y-2 text-[11px] text-muted">
        <div className="rounded-[7px] bg-surface-2 px-2.5 py-1.5">
          <span className="font-mono text-[11px] font-medium">gban</span>
          <span className="ml-2">The CLI. Runs on the developer's machine. Registers agents, links projects, and dispatches to adapters.</span>
        </div>
        <div className="rounded-[7px] bg-surface-2 px-2.5 py-1.5">
          <span className="font-mono text-[11px] font-medium">gbfleet</span>
          <span className="ml-2">The fleet supervisor. Runs on one machine, spawns workers on seats, and coordinates the wave.</span>
        </div>
        <div className="rounded-[7px] bg-surface-2 px-2.5 py-1.5">
          <span className="font-mono text-[11px] font-medium">gbagent</span>
          <span className="ml-2">The thin adapter that ships with gbfleet. Runs on each worker machine, executes the agent loop, and reports back.</span>
        </div>
      </div>
    </section>
  );
}

function SetUpThisMachineSection() {
  const [installMethod, setInstallMethod] = useState(INSTALL_METHODS[0].id);
  const [adapter, setAdapter] = useState(ADAPTERS[0].id);

  const install = INSTALL_METHODS.find((m) => m.id === installMethod) ?? INSTALL_METHODS[0];
  const adapterSteps = ADAPTERS.find((a) => a.id === adapter) ?? ADAPTERS[0];
  const allSteps = [...install.steps, ...adapterSteps.steps];

  return (
    <section className="rounded-[11px] border border-line-2 p-3" data-testid="set-up-machine">
      <h3 className="mb-2 font-mono text-[12px] font-medium">Set up this machine</h3>
      <div className="mb-3 flex flex-wrap gap-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] uppercase text-faint">Install</span>
          <select
            value={installMethod}
            onChange={(e) => setInstallMethod(e.target.value)}
            className="rounded-[5px] border border-control bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-fg-2"
            data-testid="install-method-picker"
          >
            {INSTALL_METHODS.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] uppercase text-faint">Adapter</span>
          <select
            value={adapter}
            onChange={(e) => setAdapter(e.target.value)}
            className="rounded-[5px] border border-control bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-fg-2"
            data-testid="adapter-picker"
          >
            {ADAPTERS.map((a) => (
              <option key={a.id} value={a.id}>{a.label}</option>
            ))}
          </select>
        </div>
      </div>
      <ol className="space-y-1.5">
        {allSteps.map((step, i) => (
          <li key={i} className="flex items-start gap-2 text-[11px]">
            <span className="flex h-4 w-4 flex-none items-center justify-center rounded-full bg-surface-2 font-mono text-[9px] text-faint">
              {i + 1}
            </span>
            <div className="flex-1">
              <code className="block rounded-[5px] bg-surface-2 px-2 py-1 font-mono text-[10px] text-fg-2">
                {step.cmd}
              </code>
              <span className="mt-0.5 block text-[10px] text-faint">{step.expect}</span>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
