import { Check, Copy } from "lucide-react";
import * as React from "react";

import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";

type Client = {
  id: string;
  label: string;
  file?: string;
  note?: string;
  build: (url: string, key: string) => string;
};

// Formats verified against each tool's official MCP docs.
const CLIENTS: Client[] = [
  {
    id: "claude",
    label: "Claude Code",
    note: "Run in your terminal. Add --scope user to make it global.",
    build: (u, k) => `claude mcp add --transport http graphban ${u} --header "X-API-Key: ${k}"`,
  },
  {
    id: "cursor",
    label: "Cursor",
    file: "~/.cursor/mcp.json",
    build: (u, k) => JSON.stringify({ mcpServers: { graphban: { url: u, headers: { "X-API-Key": k } } } }, null, 2),
  },
  {
    id: "codex",
    label: "Codex",
    file: "~/.codex/config.toml",
    build: (u, k) => `[mcp_servers.graphban]\nurl = "${u}"\nhttp_headers = { "X-API-Key" = "${k}" }`,
  },
  {
    id: "opencode",
    label: "opencode",
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
  {
    id: "hermes",
    label: "Hermes",
    file: "~/.hermes/config.yaml",
    note: "Run /reload-mcp in Hermes after editing the config.",
    build: (u, k) => `mcp_servers:\n  graphban:\n    url: "${u}"\n    headers:\n      X-API-Key: "${k}"\n    enabled: true`,
  },
  {
    id: "openclaw",
    label: "OpenClaw",
    file: "~/.openclaw/openclaw.json",
    note: "Or run: openclaw mcp set graphban '<json>'. Verify with openclaw mcp doctor --probe.",
    build: (u, k) =>
      JSON.stringify(
        { mcp: { servers: { graphban: { url: u, transport: "streamable-http", headers: { "X-API-Key": k } } } } },
        null,
        2,
      ),
  },
  {
    id: "grok",
    label: "Grok CLI",
    file: "~/.grok/config.toml",
    // Was a `mcp-remote` stdio bridge into a JSON file at `.grok/settings.json`, written when
    // the remote-HTTP schema was undocumented. docs.x.ai now specifies it: TOML at
    // `~/.grok/config.toml`, native remote HTTP, `headers` as inline key-value pairs. The
    // bridge worked but shipped an extra `npx` process and a wrong path.
    note: "Or run: grok mcp add --transport http graphban <url> --header \"X-API-Key: <key>\"",
    build: (u, k) => `[mcp_servers.graphban]\nurl = "${u}"\nheaders = { "X-API-Key" = "${k}" }`,
  },
];

/**
 * MCP install commands per coding tool. `apiKey` is the value dropped into the snippet — the
 * real one-time key right after creation, or the `<YOUR_API_KEY>` placeholder for an existing
 * key (whose value can't be re-shown). `keyPrefix` labels which key it's for.
 */
export function McpInstall({ apiKey, keyPrefix }: { apiKey: string; keyPrefix?: string }) {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const url = `${origin}/api/mcp`;
  const [sel, setSel] = React.useState("claude");
  const [copied, setCopied] = React.useState(false);
  const client = CLIENTS.find((c) => c.id === sel)!;
  const snippet = client.build(url, apiKey);
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
            className={cn(
              "rounded-md border px-2 py-1 text-[11px] transition-colors",
              sel === c.id ? "border-accent/50 bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
            )}
          >
            {c.label}
          </button>
        ))}
      </div>
      {client.file && <div className="mb-1 font-mono text-[10.5px] text-muted-2">{client.file}</div>}
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
      {client.note && <p className="mt-1.5 text-[10.5px] text-faint">{client.note}</p>}
      <p className="mt-1 text-[10.5px] text-faint">
        The URL must be reachable from where the agent runs — <span className="font-mono">{url}</span>
      </p>
    </div>
  );
}
