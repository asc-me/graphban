/** This box's HTTPS address and a trust probe from this browser (P83). */

/**
 * The HTTPS port every shipped install listens on: Compose publishes ${WEB_HTTPS_PORT:-8443}
 * (docker-compose.yml, web/Dockerfile HTTPS_PORT=8443) and the native install runs Caddy with
 * HTTPS_PORT=8443 (docs/native-install.md). This is an assumption: GRPH-1664 (backend:
 * https_url/https_port in GET /api/https) replaces it with the port the box actually reports.
 */
export const DEFAULT_HTTPS_PORT = 8443;

/**
 * The HTTPS address of this box. On an https page that is this page's own origin, port included.
 * On a plain-http page the HTTPS listener is on its own port (P83 D1), which this API does not
 * report yet; `httpsPort` overrides it, otherwise the shipped default (8443) is assumed.
 */
export function httpsAddress(
  loc: Pick<Location, "protocol" | "hostname" | "origin"> = window.location,
  httpsPort: number = DEFAULT_HTTPS_PORT,
): string {
  if (loc.protocol === "https:") return loc.origin;
  return httpsPort === 443 ? `https://${loc.hostname}` : `https://${loc.hostname}:${httpsPort}`;
}

/** Opens the HTTPS address from this browser. Any rejection or timeout is "unknown". */
export async function probeTrust(address: string, timeoutMs = 5000): Promise<"trusted" | "unknown"> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    await fetch(`${address}/health`, { mode: "no-cors", signal: ctl.signal, cache: "no-store" });
    return "trusted";
  } catch {
    return "unknown";
  } finally {
    clearTimeout(timer);
  }
}
