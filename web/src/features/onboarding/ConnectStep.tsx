/**
 * Quick-start ③ Connect your agent (GRPH-1362, GRPH-P67 §4 step ③).
 *
 * Two routes, one rule: **the ✓ is the server's, and only a first call earns it.** The gban
 * route installs through the CLI and leaves ③ PENDING (the key it mints is not the dialog's, so
 * nothing here can see its first call); the paste route mints a key in place, shows the harness
 * stanza and polls setup every 3 s for that key's first call. After two minutes hints appear.
 * There is no timeout that grants ✓, and Next never ticks it.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import gbanAgents from "@/lib/gban-agents.json";
import { api } from "@/lib/api";
import { McpInstall } from "@/features/settings/McpInstall";
import { useApproveCliPair } from "@/lib/queries";
import type { ApiKeyCreated } from "@/lib/types";

import type { StepMark } from "./quickStartProgress";

/** Picker order is the design's; Claude Code is the default. */
export const AGENTS: { id: string; label: string; restart: string }[] = [
  { id: "claude", label: "Claude Code", restart: "Quit Claude Code and start it again (or run /mcp to reconnect)." },
  { id: "grok", label: "Grok", restart: "Restart Grok so it re-reads its MCP config." },
  { id: "cursor", label: "Cursor", restart: "Fully quit and reopen Cursor — a GUI app does not re-read its MCP config on reload." },
  { id: "codex", label: "Codex", restart: "Start a new Codex session." },
  { id: "opencode", label: "opencode", restart: "Restart opencode." },
  { id: "openclaw", label: "OpenClaw", restart: "Restart the OpenClaw gateway." },
  { id: "hermes", label: "Hermes", restart: "Restart Hermes." },
];

/** The gban route is offered only for what the CLI package says it can configure (G9). */
export const GBAN_AGENTS: readonly string[] = gbanAgents.agents;

/**
 * The gban route's commands, exported by the CLI package with `{server}` / `{project}` holes, in
 * order: install, login, login --pair, setup (from the checkout), setup --project (no repo).
 * cli/tests parses each with gban's own argparse, so these cannot drift from the CLI's flags.
 */
export function gbanCommands(server: string, projectId: string): string[] {
  return gbanAgents.commands.map((c) => c.replaceAll("{server}", server).replaceAll("{project}", projectId));
}

/** The checkout directory name for a repo URL ("" when there is no usable repo). */
export function repoDir(repoUrl: string): string {
  const last = repoUrl.trim().replace(/\/+$/, "").replace(/\.git$/, "").split(/[/:]/).pop() ?? "";
  return last;
}

export type Route = "gban" | "key";
export const ROUTE_LABEL: Record<Route, string> = { gban: "gban CLI", key: "pasted key" };
export const ROUTE_TITLE: Record<Route, [string, string]> = {
  gban: ["Recommended — the gban CLI", "Sets up everything"],
  key: ["Just paste a key", "2 minutes, no delegation"],
};

export const HINT_AFTER_MS = 120_000;

export function CopyBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-start gap-2 rounded-[8px] border border-line bg-bg p-2.5">
      <code className="min-w-0 flex-1 whitespace-pre-wrap break-words font-mono text-[12px] text-fg">{text}</code>
      <Button
        variant="outline"
        size="sm"
        type="button"
        aria-label={label}
        onClick={() => {
          void navigator.clipboard?.writeText(text).catch(() => undefined);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
      </Button>
    </div>
  );
}

/** Approves the code `gban login --pair` prints, as the person signed in here (GRPH-1366). */
export function PairApproval() {
  const approve = useApproveCliPair();
  const [code, setCode] = React.useState("");
  return (
    <form
      className="rounded-[8px] border border-line bg-bg p-2.5"
      onSubmit={(e) => {
        e.preventDefault();
        if (code.trim()) approve.mutate(code.trim());
      }}
    >
      <label className="block text-[12px] text-muted" htmlFor="qs-pair-code">
        Code shown by <code className="font-mono">gban login --pair</code>
      </label>
      <div className="mt-1 flex gap-2">
        <input
          id="qs-pair-code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          autoComplete="off"
          placeholder="ABCD-EFGH"
          className="min-w-0 flex-1 rounded-md border border-control bg-surface-2 px-2 py-1 font-mono text-[13px]"
        />
        <Button size="sm" type="submit" loading={approve.isPending} disabled={approve.isPending || !code.trim()}>
          Approve
        </Button>
      </div>
      {approve.isSuccess && (
        <p role="status" className="mt-1.5 text-[12px] text-muted">
          Approved — your terminal is signed in as you.
        </p>
      )}
      {approve.isError && (
        <p role="alert" className="mt-1.5 text-[12px] text-st-blocked">
          That code isn't valid, or it has expired. Run gban login --pair again.
        </p>
      )}
    </form>
  );
}

export function ConnectStep({
  projectId,
  address,
  dedicated,
  repoUrl = "",
  mark,
  agent,
  onAgent,
  route,
  onRoute,
  children,
  hintAfterMs = HINT_AFTER_MS,
}: {
  projectId: string;
  /** Step ⓪'s address; "" means this computer (the page's own origin). */
  address: string;
  dedicated: boolean;
  /** The project's repo; with one, setup runs from the checkout and resolves the project (S5). */
  repoUrl?: string;
  mark: StepMark;
  agent: string;
  onAgent: (a: string) => void;
  route: Route;
  onRoute: (r: Route) => void;
  /** Advanced + Footer, which the dialog owns. */
  children?: React.ReactNode;
  hintAfterMs?: number;
}) {
  const gbanOk = GBAN_AGENTS.includes(agent);
  const effective: Route = gbanOk ? route : "key";
  const server = address || (typeof window !== "undefined" ? window.location.origin : "");
  const cmds = gbanCommands(server, projectId);
  const dir = repoDir(repoUrl);
  const info = AGENTS.find((a) => a.id === agent) ?? AGENTS[0];

  const [minted, setMinted] = React.useState<ApiKeyCreated | null>(null);
  const [minting, setMinting] = React.useState(false);
  const [error, setError] = React.useState("");
  const [waitedLong, setWaitedLong] = React.useState(false);

  async function mint() {
    setMinting(true);
    setError("");
    try {
      setMinted(
        await api.createApiKey(`quick start · ${info.label}`, projectId, null, undefined, undefined, "quickstart"),
      );
    } catch (e) {
      setError(`Could not create a key: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setMinting(false);
    }
  }

  const waiting = effective === "key" && !!minted && mark.status !== "done";
  React.useEffect(() => {
    setWaitedLong(false);
    if (!waiting) return;
    const t = setTimeout(() => setWaitedLong(true), hintAfterMs);
    return () => clearTimeout(t);
  }, [waiting, hintAfterMs]);

  const line =
    mark.status === "done"
      ? `✓ Connected — ${mark.evidence ?? "your agent made its first call"}`
      : mark.status === "unknown"
        ? `? Unknown — ${mark.reason}`
        : minted && effective === "key"
          ? "Waiting for your agent's first call…"
          : "Pending — no first call seen yet.";

  return (
    <section aria-labelledby="qs-connect-h">
      <h2 id="qs-connect-h" className="text-[16px] font-semibold">
        Connect your agent
      </h2>
      <label className="mt-3 block text-[12px] text-muted" htmlFor="qs-agent">
        Which agent?
      </label>
      <select
        id="qs-agent"
        value={agent}
        onChange={(e) => {
          onAgent(e.target.value);
          setMinted(null);
        }}
        className="mt-1 w-full rounded-md border border-control bg-surface-2 px-2 py-1.5 text-[13px]"
      >
        {AGENTS.map((a) => (
          <option key={a.id} value={a.id}>
            {a.label}
          </option>
        ))}
      </select>

      <div role="radiogroup" aria-label="Connection route" className="mt-3 flex gap-2">
        {(["gban", "key"] as Route[]).map((r) => {
          const disabled = r === "gban" && !gbanOk;
          return (
            <button
              key={r}
              type="button"
              role="radio"
              aria-checked={effective === r}
              disabled={disabled}
              title={disabled ? `${info.label} isn't set up by gban yet — paste a key instead.` : undefined}
              onClick={() => onRoute(r)}
              className={`rounded-md border px-2.5 py-1 text-[12px] ${
                effective === r ? "border-accent/50 bg-surface-3 text-fg" : "border-control text-muted"
              } disabled:opacity-50`}
            >
              {ROUTE_TITLE[r][0]} <span className="text-faint">· {ROUTE_TITLE[r][1]}</span>
            </button>
          );
        })}
      </div>
      {!gbanOk && (
        <p className="mt-1.5 text-[11.5px] text-faint">
          {info.label} isn't set up by the gban CLI, so this uses a pasted key.
        </p>
      )}

      {effective === "gban" ? (
        <div className="mt-3 space-y-2">
          <CopyBlock text={cmds[0]} label="Copy the install command" />
          <div className="rounded-[8px] border border-line p-2.5" data-testid="your-step">
            <p className="font-mono text-[10px] uppercase tracking-wide text-faint">
              Your step — signing in is yours; it can't be done for you
            </p>
            <div className="mt-1.5 space-y-2">
              <CopyBlock text={cmds[1]} label="Copy the login command" />
              <p className="text-[12px] text-muted">
                No password prompt? Use the pairing flow instead:
              </p>
              <CopyBlock text={cmds[2]} label="Copy the pair login command" />
              <PairApproval />
            </div>
          </div>
          <CopyBlock
            text={dir ? `cd ~/code/${dir} && ${cmds[3]}` : cmds[4]}
            label="Copy the setup command"
          />
          <p className="text-[12px] text-muted">
            …then restart your agent and ask it <em>"what's in my backlog?"</em>
          </p>
        </div>
      ) : (
        <div className="mt-3">
          {!minted ? (
            <Button size="sm" type="button" onClick={mint} loading={minting} disabled={minting}>
              Create a key for {info.label}
            </Button>
          ) : (
            <>
              <McpInstall apiKey={minted.plaintext} keyPrefix={minted.prefix} keyScope="project" clientId={agent} baseUrl={address || undefined} />
              <p className="mt-2 text-[12px] text-muted">{info.restart}</p>
            </>
          )}
          {waitedLong && (
            <div className="mt-2 space-y-1.5 text-[12px] text-muted" data-testid="connect-hints">
              <p>Restarted your agent? It only reads its config at start.</p>
              {dedicated && (
                <CopyBlock
                  text={`curl -s -o /dev/null -w "%{http_code}\\n" -H "X-API-Key: <your key>" ${server}/api/mcp`}
                  label="Copy the curl check"
                />
              )}
            </div>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[12px] text-st-blocked">
          {error}
        </p>
      )}
      <p className="mt-3 text-[12px] text-muted" role="status" data-status={mark.status}>
        {line}
      </p>
      {children}
    </section>
  );
}
