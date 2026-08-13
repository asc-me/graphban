/**
 * A wave's credentials as one paste (GRPH-364).
 *
 * Cursor stores ONE MCP config and reuses it across every agent, so a per-role credential has
 * nowhere to live: one key for everybody, and `independent()` then refuses every review.
 *
 * **This shipped once assuming `${env:VAR}` interpolation and that was wrong.** Probed against
 * Cursor 3.16.2 with the variables provably present in the process environment: `${env:VAR}`,
 * `${VAR}` and `$VAR` were all ignored in both `url` and `headers`, and the server was silently
 * DROPPED rather than sent a literal — no request, no 401, no error. The documented example
 * that suggested otherwise was almost certainly a stdio server's `env` block, which is a
 * different mechanism.
 *
 * What survived the probe is the part that mattered: Cursor happily holds SEVERAL server
 * entries with different literal headers. So one config still carries three role-narrowed
 * credentials — it is regenerated per wave instead of written once, which costs one paste and
 * keeps the roles genuinely ENFORCED rather than advisory.
 */

/** The three fleet roles a wave provisions. `all-in-one` is the single-agent posture and is
 *  deliberately absent: a wave is the fleet shape. */
export const WAVE_ROLES = ["planner", "worker", "reviewer"] as const;

export type WaveRole = (typeof WAVE_ROLES)[number];

export const serverName = (role: WaveRole) => `graphban-${role}`;

/**
 * The whole `mcp.json` for a wave, keys included.
 *
 * Literal rather than referenced, because nothing else works — see above. That is a real
 * trade: the file now holds credentials and is regenerated each wave. It is the same shape a
 * hand-written Cursor config already has, and wave keys suit it better than a hand-minted one
 * does: they expire, and End wave revokes them.
 */
export function cursorWaveConfig(url: string, keys: Partial<Record<WaveRole, string>>): string {
  const servers: Record<string, unknown> = {};
  for (const role of WAVE_ROLES) {
    if (!keys[role]) continue;
    servers[serverName(role)] = { url, headers: { "X-API-Key": keys[role] } };
  }
  return JSON.stringify({ mcpServers: servers }, null, 2);
}

/**
 * What each agent must declare so two on ONE credential can review each other.
 *
 * Only needed for the shared-credential fallback — with a wave, the credentials already
 * differ. On a shared key an agent that declares nothing that differs is refused review,
 * deliberately: absence is not a difference, or omitting a field would launder a self-review.
 */
export function instanceHint(role: WaveRole | "shared", n: number): string {
  return `capabilities={"instance": "${role}-${n}"}`;
}
