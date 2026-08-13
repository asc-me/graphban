/**
 * A wave's credentials as one paste (GRPH-364).
 *
 * Cursor stores ONE MCP config and reuses it across every agent, so the per-role credential
 * the fleet posture depends on has nowhere to live: one key, every agent, and `independent()`
 * refuses every review because author and reviewer share both credential and host.
 *
 * The way out is not per-worktree config files — that is four setups per wave and a bad
 * trade. It is that Cursor interpolates environment variables into header values, so ONE
 * config can name three servers whose keys are supplied per wave:
 *
 *   config written once  ->  three env vars rotate per wave
 *
 * Each server carries a genuinely role-narrowed key, so the role gate refuses a worker that
 * reaches for `sign_off`, and a reviewer signing a worker's item is independent because the
 * credentials differ.
 *
 * **This is a real improvement, not a guarantee.** Cursor has no per-agent MCP scoping, so an
 * agent sees all three servers and could deliberately switch hands to sign its own work. What
 * changes is the default: today a Cursor fleet shares one UNRESTRICTED key where nothing is
 * refused; here the wrong call fails unless somebody goes out of their way.
 */

/** The three fleet roles a wave provisions. `all-in-one` is the single-agent posture and is
 *  deliberately absent: a wave is the fleet shape, and mixing them in one config would offer
 *  an unnarrowed credential beside three narrowed ones. */
export const WAVE_ROLES = ["planner", "worker", "reviewer"] as const;

export type WaveRole = (typeof WAVE_ROLES)[number];

/** Header value indirection, per role. Kept in one place because the generated Cursor plugin
 *  names the SAME variables — a rename on one side and the pasted block silently stops
 *  matching the config, which reads as "the key is wrong" rather than "the name is". */
export const ROLE_ENV: Record<WaveRole, string> = {
  planner: "GRAPHBAN_PLANNER_KEY",
  worker: "GRAPHBAN_WORKER_KEY",
  reviewer: "GRAPHBAN_REVIEWER_KEY",
};

export const serverName = (role: WaveRole) => `graphban-${role}`;

/** The per-wave half: what actually rotates. */
export function envBlock(keys: Partial<Record<WaveRole, string>>): string {
  return WAVE_ROLES.filter((r) => keys[r])
    .map((r) => `export ${ROLE_ENV[r]}=${keys[r]}`)
    .join("\n");
}

/**
 * The write-once half. Valid as `.cursor/mcp.json` AND as a plugin's `mcp.json` — the plugin
 * docs do not separately specify a schema for remote servers, so emitting one file that works
 * in both places means a plugin that fails to load costs a copy rather than a rewrite.
 */
export function cursorWaveConfig(url: string): string {
  const servers: Record<string, unknown> = {};
  for (const role of WAVE_ROLES) {
    servers[serverName(role)] = { url, headers: { "X-API-Key": `\${env:${ROLE_ENV[role]}}` } };
  }
  return JSON.stringify({ mcpServers: servers }, null, 2);
}
