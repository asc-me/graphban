/** Installing the two local tools. One package each: `gban` pulls nothing, `gbfleet`
 *  brings httpx — and `gbagent` rides along with the supervisor. */
export const INSTALL_SUPERVISOR = "uv tool install graphban-fleet";
export const INSTALL_CLI = "uv tool install graphban-cli";
export const INSTALL_CLI_BREW = "brew install asc-me/tap/gban";

/** Adapters the supervisor can actually resolve — `gbfleet.adapters.ADAPTERS`. */
export const FLEET_ADAPTERS = ["claude", "cursor-agent", "gbagent", "grok"] as const;
export type FleetAdapter = (typeof FLEET_ADAPTERS)[number];

/** The two install lines above assume `uv`. This is how to get it (Astral's installer). */
export const INSTALL_UV = "curl -LsSf https://astral.sh/uv/install.sh | sh";
