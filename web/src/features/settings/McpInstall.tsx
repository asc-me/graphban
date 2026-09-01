import { Check, Copy } from "lucide-react";
import * as React from "react";

import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";

type FormKind = "command" | "config";

/** The key's scope, same axis as Settings' mint toggle and `graphban init --key-scope`. */
export type KeyScope = "project" | "global";

/** What each harness names those two scopes. Only clients with a verified `--scope` use it. */
const HARNESS_SCOPE: Record<KeyScope, string> = { project: "project", global: "user" };

type Form = {
  /** Shown above the snippet — the file it belongs in. Config forms only. */
  file?: string;
  /** A function form varies the note with the key's scope, so it can explain the flag it set. */
  note?: string | ((scope: KeyScope | "") => string | undefined);
  /** `scope` is "" when the caller does not know the key's scope — emit no flag then. */
  build: (url: string, key: string, scope: KeyScope | "") => string;
};

type Client = {
  id: string;
  label: string;
  /** A terminal one-liner. Preferred, and the default when present. */
  command?: Form;
  /** The stanza to paste into a config file. */
  config?: Form;
  /**
   * Why this client has no Command form. Only set where the vendor docs are explicit — an
   * absent tab with no reason reads as something we forgot rather than something they lack.
   */
  noCommand?: string;
};

const OPENCLAW_JSON = (u: string, k: string) =>
  JSON.stringify({ url: u, transport: "streamable-http", headers: { "X-API-Key": k } });

// Formats verified against each tool's official MCP docs. `--scope` values are from the two
// tools' own `mcp add --help`: claude takes local|user|project (default local — a per-machine
// registration, the one thing you do NOT want to guess), grok takes user|project and defaults
// to USER, so a project-scoped key must say so explicitly. OpenClaw's `mcp set` exposes no
// scope flag I can verify, so its command stays scope-free — the key's scope goes unmentioned
// rather than into a flag that may not exist.
// Command availability is NOT uniform,
// and having a `mcp add` is not the same as being able to declare THIS server with it: Cursor
// ships no MCP CLI at all; opencode's `mcp` subcommands cover auth/list/logout; `codex mcp add`
// takes stdio servers exclusively; `hermes mcp add` installs catalog entries. None of the four
// can add a remote server with a header from a terminal, so for them the config file is the
// whole story — and each says so, since a missing Command tab otherwise reads as our omission.
export const CLIENTS: Client[] = [
  {
    id: "claude",
    label: "Claude Code",
    command: {
      note: (s) =>
        s === "global"
          ? "--scope user registers it in every project — the key is unbound and names a project per call."
          : s === "project"
            ? "--scope project puts it in .mcp.json beside this repo — the key is bound to exactly one project."
            : "Add --scope project for one repo, --scope user for every project.",
      build: (u, k, s) =>
        `claude mcp add --transport http${s ? ` --scope ${HARNESS_SCOPE[s]}` : ""} graphban ${u} --header "X-API-Key: ${k}"`,
    },
    config: {
      file: ".mcp.json",
      note: "Project-scoped, and safe to commit only if the key is an env reference.",
      build: (u, k) =>
        JSON.stringify({ mcpServers: { graphban: { type: "http", url: u, headers: { "X-API-Key": k } } } }, null, 2),
    },
  },
  {
    id: "cursor",
    label: "Cursor",
    noCommand: "Cursor has no MCP CLI — the config file and the Customize UI are the documented routes.",
    config: {
      file: "~/.cursor/mcp.json",
      build: (u, k) => JSON.stringify({ mcpServers: { graphban: { url: u, headers: { "X-API-Key": k } } } }, null, 2),
    },
  },
  {
    id: "codex",
    label: "Codex",
    noCommand: "`codex mcp add` takes stdio servers only; a remote server with a header must go in the config.",
    config: {
      file: "~/.codex/config.toml",
      build: (u, k) => `[mcp_servers.graphban]\nurl = "${u}"\nhttp_headers = { "X-API-Key" = "${k}" }`,
    },
  },
  {
    id: "opencode",
    label: "opencode",
    noCommand: "opencode's `mcp` commands cover auth, list and logout — servers are declared in the config.",
    config: {
      file: "opencode.json",
      build: (u, k) =>
        JSON.stringify(
          {
            $schema: "https://opencode.ai/config.json",
            mcp: { graphban: { type: "remote", url: u, enabled: true, headers: { "X-API-Key": k } } },
          },
          null,
          2,
        ),
    },
  },
  {
    id: "hermes",
    label: "Hermes",
    // `url` and `command` are mutually exclusive transport discriminators in hermes_cli's
    // mcp_config, and `headers` is HTTP-only — so this remote shape is the right one. There IS
    // a `hermes mcp add`, but it is documented for installing catalog entries rather than for
    // declaring an arbitrary remote server with a header, so it does not earn a Command form.
    noCommand: "`hermes mcp add` installs catalog entries; a remote server with a header is declared in the config.",
    config: {
      file: "~/.hermes/config.yaml",
      note: "Run /reload-mcp in Hermes after editing the config.",
      build: (u, k) => `mcp_servers:\n  graphban:\n    url: "${u}"\n    headers:\n      X-API-Key: "${k}"\n    enabled: true`,
    },
  },
  {
    id: "openclaw",
    label: "OpenClaw",
    command: {
      note: "Verify with openclaw mcp doctor --probe.",
      build: (u, k) => `openclaw mcp set graphban '${OPENCLAW_JSON(u, k)}'`,
    },
    config: {
      file: "~/.openclaw/openclaw.json",
      build: (u, k) => JSON.stringify({ mcp: { servers: { graphban: JSON.parse(OPENCLAW_JSON(u, k)) } } }, null, 2),
    },
  },
  {
    id: "grok",
    label: "Grok CLI",
    // Was a `mcp-remote` stdio bridge into a JSON file at `.grok/settings.json`, written when
    // the remote-HTTP schema was undocumented. docs.x.ai now specifies it: TOML at
    // `~/.grok/config.toml`, native remote HTTP, `headers` as inline key-value pairs. The
    // bridge worked but shipped an extra `npx` process and a wrong path.
    command: {
      // grok defaults to user — the flag is what stops a project key's global install.
      note: (s) =>
        s === "global"
          ? "--scope user writes ~/.grok/config.toml, available in all your projects (grok's default)."
          : s === "project"
            ? "--scope project writes ./.grok/config.toml, shared with everyone in the repo — grok would otherwise default to user."
            : undefined,
      build: (u, k, s) =>
        `grok mcp add --transport http${s ? ` --scope ${HARNESS_SCOPE[s]}` : ""} graphban ${u} --header "X-API-Key: ${k}"`,
    },
    config: {
      file: "~/.grok/config.toml",
      build: (u, k) => `[mcp_servers.graphban]\nurl = "${u}"\nheaders = { "X-API-Key" = "${k}" }`,
    },
  },
];

/**
 * MCP install commands per coding tool. `apiKey` is the value dropped into the snippet — the
 * real one-time key right after creation, or the `<YOUR_API_KEY>` placeholder for an existing
 * key (whose value can't be re-shown). `keyPrefix` labels which key it's for. `keyScope`
 * drives the harness `--scope` flag: where the tool supports one, project keys register in the
 * repo and global keys in the user config, so the snippet matches the key without the operator
 * translating between the two. Unknown scope → no flag, and the note says what to add.
 */
export function McpInstall({
  apiKey,
  keyPrefix,
  keyScope,
}: {
  apiKey: string;
  keyPrefix?: string;
  keyScope?: KeyScope;
}) {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const url = `${origin}/api/mcp`;
  const [sel, setSel] = React.useState("claude");
  const [form, setForm] = React.useState<FormKind>("command");
  const [copied, setCopied] = React.useState(false);
  const client = CLIENTS.find((c) => c.id === sel)!;
  // Falls back rather than resetting: selecting Cursor shows its config without an effect, and
  // going back to a client that has both restores the form you had chosen.
  const active = (form === "command" ? client.command : client.config) ?? client.command ?? client.config!;
  const both = Boolean(client.command && client.config);
  const scope: KeyScope | "" = keyScope ?? "";
  const snippet = active.build(url, apiKey, scope);
  const note = typeof active.note === "function" ? active.note(scope) : active.note;
  const placeholder = apiKey.startsWith("<");

  return (
    <div className="mt-3 rounded-[11px] border border-line-2 bg-surface-2 p-3">
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
        Connect an agent · MCP{keyPrefix ? ` · ${keyPrefix}…` : ""}
      </div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {CLIENTS.map((c) => (
          <button
            key={c.id}
            onClick={() => {
              setSel(c.id);
              setCopied(false);
            }}
            aria-pressed={sel === c.id}
            className={cn(
              "rounded-md border px-2 py-1 text-[11px] transition-colors",
              sel === c.id ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
            )}
          >
            {c.label}
          </button>
        ))}
      </div>
      {both && (
        <div className="mb-2 flex gap-1">
          {(["command", "config"] as FormKind[]).map((f) => (
            <button
              key={f}
              onClick={() => {
                setForm(f);
                setCopied(false);
              }}
              aria-pressed={active === client[f]}
              className={cn(
                "rounded-md border px-2 py-0.5 text-[10.5px] transition-colors",
                active === client[f] ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
              )}
            >
              {f === "command" ? "Command" : "Config file"}
            </button>
          ))}
        </div>
      )}
      {!client.command && client.noCommand && (
        <p className="mb-2 text-[10.5px] text-faint">{client.noCommand}</p>
      )}
      {active.file && <div className="mb-1 font-mono text-[10.5px] text-muted-2">{active.file}</div>}
      <div className="relative">
        <pre className="max-h-56 overflow-auto rounded-md border border-line-2 bg-surface px-2.5 py-2 pr-10 font-mono text-[10.5px] leading-relaxed text-fg-2">
          {snippet}
        </pre>
        <button
          onClick={() => copyText(snippet).then((ok) => ok && (setCopied(true), setTimeout(() => setCopied(false), 1500)))}
          className="absolute right-1.5 top-1.5 rounded-md border border-line-2 bg-surface-3 p-1.5 text-muted hover:text-fg"
          title="Copy"
        >
          {copied ? <Check size={13} className="text-accent" /> : <Copy size={13} />}
        </button>
      </div>
      {placeholder && (
        <p className="mt-1.5 text-[10.5px] text-faint">
          Replace <span className="font-mono text-muted-2">{apiKey}</span> with the key you saved when you created it.
        </p>
      )}
      {note && <p className="mt-1.5 text-[10.5px] text-faint">{note}</p>}
      <p className="mt-1 text-[10.5px] text-faint">
        The URL must be reachable from where the agent runs — <span className="font-mono">{url}</span>
      </p>
    </div>
  );
}
