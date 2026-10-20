/** Installing the two local tools. One package each: `gban` pulls nothing, `gbfleet`
 *  brings httpx — and `gbagent` rides along with the supervisor. */
export const INSTALL_SUPERVISOR = "uv tool install graphban-fleet";
export const INSTALL_CLI = "uv tool install graphban-cli";
export const INSTALL_CLI_BREW = "brew install asc-me/tap/gban";

/** Adapters the supervisor can actually resolve — `gbfleet.adapters.ADAPTERS`. */
export const FLEET_ADAPTERS = ["claude", "cursor-agent", "gbagent", "grok"] as const;
export type FleetAdapter = (typeof FLEET_ADAPTERS)[number];
