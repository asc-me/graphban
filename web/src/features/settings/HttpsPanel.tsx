/**
 * This box → HTTPS (P83 tier 1: the box's own certificate authority).
 *
 * Three rules this page holds:
 * - the trust chip says Trusted only when a probe from THIS browser succeeded; a failed or
 *   unfinished probe is "Can't tell", never trusted;
 * - an API error is an error, never an empty names list or an untrusted page;
 * - no private key is read or shown anywhere: the API has no route that returns one.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useHttpsNames, useHttpsStatus } from "@/lib/queries";
import type { HttpsStatus } from "@/lib/api";

export type Trust = "unchecked" | "checking" | "trusted" | "untrusted" | "unknown";
type Os = "macOS" | "Windows" | "iOS" | "Android" | "Linux";

const OS_TABS: Os[] = ["macOS", "Windows", "iOS", "Android", "Linux"];

const STEPS: Record<Os, string[]> = {
  macOS: [
    "Download the certificate file below.",
    "Open it. Keychain Access opens; add it to the login keychain.",
    "Double-click “Graphban box CA”, open Trust, and set “When using this certificate” to Always Trust.",
    "Close the window and enter your password to confirm.",
  ],
  Windows: [
    "Download the certificate file below.",
    "Open it and choose Install Certificate.",
    "Pick Current User, then place it in “Trusted Root Certification Authorities”.",
    "Finish the wizard and confirm the prompt.",
  ],
  iOS: [
    "Download the certificate file below and allow the profile to download.",
    "In Settings, open Profile Downloaded and install it.",
    "In Settings → General → About → Certificate Trust Settings, turn on full trust for it.",
  ],
  Android: [
    "Download the certificate file below.",
    "In Settings, search for “CA certificate” and choose Install anyway if asked.",
    "Pick the downloaded file and confirm.",
  ],
  Linux: [
    "Download the certificate file below.",
    "Copy it to /usr/local/share/ca-certificates/ and run sudo update-ca-certificates.",
    "Restart your browser; Firefox keeps its own store under Settings → Certificates.",
  ],
};

export function detectOs(ua = typeof navigator === "undefined" ? "" : navigator.userAgent): Os {
  if (/iPhone|iPad|iPod/i.test(ua)) return "iOS";
  if (/Android/i.test(ua)) return "Android";
  if (/Windows/i.test(ua)) return "Windows";
  if (/Mac/i.test(ua)) return "macOS";
  return "Linux";
}

export function httpsAddress(loc: Pick<Location, "hostname"> = window.location): string {
  return `https://${loc.hostname}`;
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

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

const CHIP: Record<Trust, string> = {
  unchecked: "Not trusted by this browser yet",
  checking: "Checking…",
  trusted: "Trusted by this browser",
  untrusted: "Not trusted by this browser yet",
  unknown: "Can't tell",
};

export function HttpsPanel() {
  const { data, error, isLoading, refetch } = useHttpsStatus();
  if (isLoading) return <p className="text-[13px] text-muted">Loading HTTPS settings…</p>;
  if (error || !data) {
    return (
      <div role="alert" className="max-w-2xl space-y-2">
        <p className="text-lead font-semibold">Couldn't load the HTTPS settings</p>
        <p className="text-[13px] text-fg-2">{error instanceof Error ? error.message : "Unknown error"}</p>
        <Button onClick={() => refetch()}>Try again</Button>
      </div>
    );
  }
  return <HttpsBody data={data} />;
}

function HttpsBody({ data }: { data: HttpsStatus }) {
  const address = httpsAddress();
  const [trust, setTrust] = React.useState<Trust>(
    window.location.protocol === "https:" ? "trusted" : "unchecked");
  const [probed, setProbed] = React.useState(false);
  const check = async () => {
    setTrust("checking");
    const r = await probeTrust(address);
    setProbed(true);
    setTrust(r);
  };
  const refused = probed && trust === "unknown";
  const renew = data.leaf_not_after ? new Date(data.leaf_not_after).toLocaleDateString() : null;
  return (
    <div className="max-w-2xl space-y-6">
      <section aria-label="HTTPS status" className="space-y-2">
        <h2 className="text-lead font-semibold">This box has its own certificate</h2>
        <p className="text-[13px] text-fg-2">
          Your box signs its own HTTPS certificate. Devices trust it once you install its certificate authority.
        </p>
        <div className="flex items-center gap-2 text-[13px]">
          <span>HTTPS address</span>
          <code className="font-mono text-fg">{address}</code>
          <Button onClick={() => navigator.clipboard?.writeText(address)}>Copy</Button>
        </div>
        <p className="text-[13px] text-fg-2">{data.names.length} names covered</p>
        {renew ? <p className="text-[13px] text-fg-2">Renews on {renew}</p> : null}
        <p className="text-[13px] text-fg-2">
          Plain http:// requests are not redirected to HTTPS with this box's own certificate.
        </p>
        <div className="flex items-center gap-2">
          <span data-testid="trust-chip" data-trust={trust}
            className="rounded border border-line px-2 py-0.5 text-[12px]">{CHIP[trust]}</span>
          <Button onClick={check}>Check again</Button>
        </div>
        {trust === "unknown" ? (
          <p className="text-[13px] text-fg-2">
            The check from this browser failed, so there's no answer. Check again, or trust this box on this device below.
          </p>
        ) : null}
      </section>

      <TrustDevice address={address} refused={refused} onCheck={check} caPath={data.ca_certificate_path} />
      <Names data={data} />

      <section aria-label="Redirect" className="space-y-1">
        <Label>Redirect http to https</Label>
        <p className="text-[13px] text-muted">
          Not available with this box's own certificate. It arrives with a public certificate.
        </p>
      </section>

      <details>
        <summary className="cursor-pointer text-[13px] font-semibold">Advanced</summary>
        <div className="mt-2 space-y-2">
          <a className="text-[13px] underline" href={data.ca_certificate_path} download>
            Download current certificate
          </a>
          <p className="text-[13px] text-fg-2">
            Why the box has its own CA: a public authority will not issue a certificate for a private address, so
            the box signs its own and you tell each device to trust it. The CA's private key never leaves the box
            and is never shown here.
          </p>
        </div>
      </details>
    </div>
  );
}

function TrustDevice({ address, refused, onCheck, caPath }: {
  address: string; refused: boolean; onCheck: () => void; caPath: string;
}) {
  const [os, setOs] = React.useState<Os>(detectOs());
  const http = `http://${window.location.host}`;
  return (
    <section aria-label="Trust this box on this device" className="space-y-2">
      <h3 className="text-lead font-semibold">Trust this box on this device</h3>
      <a className="text-[13px] underline" href={caPath} download>Download the CA file</a>
      <div role="tablist" className="flex gap-1">
        {OS_TABS.map((t) => (
          <button key={t} role="tab" aria-selected={t === os} onClick={() => setOs(t)}
            className={`rounded px-2 py-0.5 text-[12px] ${t === os ? "bg-surface-2 font-semibold" : ""}`}>{t}</button>
        ))}
      </div>
      <ol className="list-decimal space-y-1 pl-5 text-[13px] text-fg-2">
        {STEPS[os].map((s) => <li key={s}>{s}</li>)}
      </ol>
      <Button onClick={onCheck}>Check again</Button>
      <span className="ml-2 text-[12px] text-muted">Opens {address} from this browser.</span>
      {refused ? (
        <div className="space-y-1 text-[13px]" data-testid="trust-refused">
          <p>This browser still doesn't trust the box. Two ways out:</p>
          <ul className="list-disc pl-5">
            <li><a href="#bring-your-own" className="underline">Bring your own certificate</a></li>
            <li><a href="#graphban-cloud" className="underline">Graphban Cloud</a></li>
          </ul>
          <p className="text-muted">You can keep using {http} on this device.</p>
        </div>
      ) : null}
    </section>
  );
}

function Names({ data }: { data: HttpsStatus }) {
  const { add, remove } = useHttpsNames();
  const [name, setName] = React.useState("");
  const full = data.names.length >= data.max_names;
  const err = (add.error ?? remove.error) as Error | null;
  const changed = add.isSuccess || remove.isSuccess;
  return (
    <section aria-label="Names" className="space-y-2">
      <div className="flex items-baseline justify-between">
        <h3 className="text-lead font-semibold">Names</h3>
        <span className="text-[12px] text-muted">{data.names.length} of {data.max_names}</span>
      </div>
      <ul className="divide-y divide-line">
        {data.names.map((n) => (
          <li key={n.name} className="flex items-center justify-between py-1.5 text-[13px]">
            <span><code className="font-mono">{n.name}</code>{" "}
              <span className="text-muted">
                {n.source === "operator" ? "added by you" : `found automatically${n.origin ? ` (${n.origin})` : ""}`}
              </span>
            </span>
            {n.source === "operator" ? (
              <Button onClick={() => remove.mutate(n.name)}>Remove</Button>
            ) : null}
          </li>
        ))}
      </ul>
      <form className="flex gap-2" onSubmit={(e) => {
        e.preventDefault();
        if (name.trim() && !full) add.mutate(name.trim(), { onSuccess: () => setName("") });
      }}>
        <input aria-label="Add a name" className="flex-1 rounded border border-line bg-transparent px-2 py-1 text-[13px]"
          value={name} onChange={(e) => setName(e.target.value)} placeholder="box.example.com or 192.168.1.20" />
        <Button type="submit" disabled={full || !name.trim()}>Add a name</Button>
      </form>
      <p className="text-[12px] text-muted">
        Add any host name or address you reach this box by. The box finds the ones you already use automatically.
      </p>
      {err ? <p role="alert" className="text-[13px] text-danger">{err.message}</p> : null}
      {changed && !err ? (
        <p className="text-[12px] text-muted">
          The certificate was reissued with the new list. Devices that trust the box keep trusting it.
        </p>
      ) : null}
    </section>
  );
}
