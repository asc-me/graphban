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
 * The gban route's commands, exported by the CLI package with `{server}` / `{project}` holes.
 * cli/tests parses each with gban's own argparse, so these cannot drift from the CLI's flags.
 */
export function gbanCommands(server: string, projectId: string): string[] {
  return gbanAgents.commands.map((c) => c.replaceAll("{server}", server).replaceAll("{project}", projectId));
}
const COMMAND_LABELS = ["Copy the install command", "Copy the login command", "Copy the setup command"];

export type Route = "gban" | "key";
export const ROUTE_LABEL: Record<Route, string> = { gban: "gban CLI", key: "pasted key" };

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

export function ConnectStep({
  projectId,
  address,
  dedicated,
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
              {r === "gban" ? "gban CLI" : "Paste a key"}
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
          {gbanCommands(server, projectId).map((cmd, i) => (
            <CopyBlock key={cmd} text={cmd} label={COMMAND_LABELS[i] ?? "Copy the command"} />
          ))}
          <p className="text-[12px] text-muted">
            If you haven't already, run <code className="font-mono">gban setup</code>, then restart your agent.
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
