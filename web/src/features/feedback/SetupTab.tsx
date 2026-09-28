import { Check, ExternalLink, RefreshCw } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";
import { settingsPath } from "@/lib/routes";
import type { PlatformConfig } from "@/lib/types";

import {
  dnsRecordKey,
  initialDnsResults,
  measureDnsRecords,
  type DnsRecord,
  type DnsRecordResult,
  type DnsRecordStatus,
} from "./dnsCheck";

type Route = "relay" | "custom-domain" | "none";

const ROUTES: { value: Route; label: string; tag: string; description: string }[] = [
  {
    value: "relay",
    label: "Relay through the cloud",
    tag: "recommended",
    description:
      "Submissions go to the cloud org, which holds them until your host picks them up. No DNS or TLS needed on your side.",
  },
  {
    value: "custom-domain",
    label: "Custom domain",
    tag: "advanced",
    description:
      "Point a subdomain at your host with a CNAME record. TLS is provisioned automatically once DNS resolves.",
  },
  {
    value: "none",
    label: "Direct (same network)",
    tag: "self-host only",
    description:
      "Your website and this deployment share a network. The widget posts directly to the ingest endpoint — no relay, no domain.",
  },
];

export function SetupTab({ platform }: { platform: PlatformConfig | undefined }) {
  const [route, setRoute] = React.useState<Route>("relay");
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const host = typeof window !== "undefined" ? window.location.host : "";

  const dnsRecords: DnsRecord[] = React.useMemo(
    () => [
      {
        type: "CNAME",
        name: "feedback.yourdomain.com",
        value: host || "your-host.example.com",
        purpose: "Points the widget's submit URL at this deployment",
      },
      {
        type: "TXT",
        name: "_graphban-verify.yourdomain.com",
        value: platform?.share_token ?? "⟨not minted⟩",
        purpose: "Proves ownership so TLS can be issued",
      },
    ],
    [host, platform?.share_token],
  );

  const hasIngestToken = !!platform?.ingest_token_prefix;
  const hasTurnstile = !!platform?.turnstile_sitekey;

  const readiness = [
    { label: "Ingest token minted", met: hasIngestToken, hint: "Settings → Integrations → Ingest token" },
    {
      label: "Spam protection configured",
      met: hasTurnstile,
      hint: hasTurnstile ? "Turnstile sitekey is set" : "Settings → Integrations → Turnstile",
    },
    {
      label: "Public intake enabled",
      met: platform?.intake_enabled ?? false,
      hint: "Settings → Project → Public surfaces",
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <p className="text-[13px] text-muted">
        This deployment runs on a private host, so people on your website can't send to it directly.
        Choose how the widget reaches it.
      </p>

      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Route</div>
        <div className="flex flex-col gap-2">
          {ROUTES.map((r) => {
            const active = route === r.value;
            return (
              <button
                key={r.value}
                onClick={() => setRoute(r.value)}
                className={cn(
                  "flex flex-col gap-1 rounded-lg border px-3.5 py-3 text-left transition-colors",
                  active ? "border-line-hover bg-surface-3" : "border-line-2 bg-surface-2 hover:border-line-hover",
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-fg">{r.label}</span>
                  <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[9.5px] uppercase tracking-wide text-faint-2">
                    {r.tag}
                  </span>
                </div>
                <span className="text-[12px] text-muted">{r.description}</span>
              </button>
            );
          })}
        </div>
      </div>

      {route === "custom-domain" && <CustomDomainSection records={dnsRecords} origin={origin} />}
      {route === "relay" && <RelaySection />}
      {route === "none" && <DirectSection origin={origin} />}

      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Readiness</div>
        <div className="flex flex-col gap-1.5">
          {readiness.map((r) => (
            <div key={r.label} className="flex items-center gap-2.5 rounded-md border border-line-2 bg-surface-2 px-3 py-2">
              {r.met ? (
                <Check size={14} className="flex-none text-accent" />
              ) : (
                <span className="flex-none h-3.5 w-3.5 rounded-full border border-line-2" />
              )}
              <span className={cn("text-[12.5px]", r.met ? "text-fg" : "text-muted")}>{r.label}</span>
              <span className="ml-auto text-[11px] text-faint">{r.hint}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-line-2 bg-surface-2 p-4">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Why the cloud org?</div>
        <p className="text-[12.5px] text-muted">
          It's the only public piece. It holds submissions for up to 7 days and never sees your code or memory.
        </p>
        <a
          href={settingsPath("deployment/sync")}
          className="mt-2.5 inline-flex items-center gap-1 text-[12px] text-fg-2 underline decoration-line-hover underline-offset-2 hover:text-fg"
        >
          Sync link settings
          <ExternalLink size={11} />
        </a>
      </div>
    </div>
  );
}

function CustomDomainSection({ records, origin }: { records: DnsRecord[]; origin: string }) {
  const [checking, setChecking] = React.useState(false);
  const [results, setResults] = React.useState<Record<string, DnsRecordResult>>(() =>
    initialDnsResults(records),
  );

  React.useEffect(() => {
    setResults(initialDnsResults(records));
  }, [records]);

  async function checkDns() {
    setChecking(true);
    setResults(measureDnsRecords(records));
    setChecking(false);
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Steps</div>
        <ol className="flex flex-col gap-3">
          <Step n={1} title="Add the CNAME record" result="DNS propagates (usually under a minute)">
            <code className="font-mono text-[11.5px] text-muted-2">
              feedback.yourdomain.com → {origin.replace(/^https?:\/\//, "")}
            </code>
          </Step>
          <Step n={2} title="Add the TXT verification record" result="Proves ownership for TLS">
            <code className="font-mono text-[11.5px] text-muted-2">
              _graphban-verify.yourdomain.com → ⟨your share token⟩
            </code>
          </Step>
          <Step n={3} title="Wait for automatic TLS" result="A certificate is issued within minutes of DNS resolving">
            <p className="text-[12px] text-muted">
              No action needed. Once both records resolve, a Let's Encrypt certificate is provisioned automatically
              and the widget becomes available at <code className="font-mono text-[11.5px]">https://feedback.yourdomain.com</code>.
            </p>
          </Step>
        </ol>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="font-mono text-[10px] uppercase tracking-wide text-faint">DNS records</div>
          <button
            onClick={checkDns}
            disabled={checking}
            className="inline-flex items-center gap-1.5 rounded-md border border-line-2 bg-surface-3 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:text-fg disabled:opacity-50"
          >
            <RefreshCw size={11} className={cn(checking && "animate-spin")} />
            {checking ? "Checking…" : "Check DNS"}
          </button>
        </div>
        <div className="overflow-hidden rounded-lg border border-line-2">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-line-2 bg-surface-2">
                <th className="px-3 py-2 font-mono text-[10px] uppercase tracking-wide text-faint">Type</th>
                <th className="px-3 py-2 font-mono text-[10px] uppercase tracking-wide text-faint">Name</th>
                <th className="px-3 py-2 font-mono text-[10px] uppercase tracking-wide text-faint">Value</th>
                <th className="px-3 py-2 font-mono text-[10px] uppercase tracking-wide text-faint">Status</th>
              </tr>
            </thead>
            <tbody>
              {records.map((r) => {
                const result = results[dnsRecordKey(r)] ?? { status: "unknown", detail: "Not checked yet" };
                return (
                  <tr key={dnsRecordKey(r)} className="border-b border-line-2 last:border-b-0">
                    <td className="px-3 py-2 font-mono text-[11px] text-fg-2">{r.type}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-muted">{r.name}</td>
                    <td className="max-w-[200px] truncate px-3 py-2 font-mono text-[11px] text-muted-2" title={r.value}>
                      {r.value}
                    </td>
                    <td className="px-3 py-2">
                      <DnsStatusBadge status={result.status} detail={result.detail} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-1.5 text-[11px] text-faint">
          TLS is provisioned automatically once both records resolve. No certificate to paste.
        </p>
      </div>
    </div>
  );
}

function RelaySection() {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Steps</div>
      <ol className="flex flex-col gap-3">
        <Step n={1} title="Link a cloud org" result="The relay gets a place to hold submissions">
          <p className="text-[12px] text-muted">
            Go to <a href={settingsPath("deployment/sync")} className="text-fg-2 underline decoration-line-hover underline-offset-2 hover:text-fg">Sync link settings</a> and
            connect this deployment to a cloud org. The org's relay endpoint becomes the widget's submit URL.
          </p>
        </Step>
        <Step n={2} title="Copy the embed snippet" result="The snippet's submit URL points at the relay automatically">
          <p className="text-[12px] text-muted">
            Switch to <span className="text-fg-2">Customize</span> and copy the snippet from the bottom. The generated
            URL includes the relay route — no manual edit needed.
          </p>
        </Step>
        <Step n={3} title="Submissions are picked up by your host" result="The cloud holds them for up to 7 days">
          <p className="text-[12px] text-muted">
            Your deployment polls the relay for new submissions. The cloud never sees your code or memory — it
            only holds the form data until you collect it.
          </p>
        </Step>
      </ol>
    </div>
  );
}

function DirectSection({ origin }: { origin: string }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Steps</div>
      <ol className="flex flex-col gap-3">
        <Step n={1} title="Confirm network access" result="The visitor's browser can reach this host">
          <p className="text-[12px] text-muted">
            The widget posts to <code className="font-mono text-[11.5px]">{origin}/api/public/submit</code>. If
            your website and this deployment share a VPN or internal network, no further setup is needed.
          </p>
        </Step>
        <Step n={2} title="Copy the embed snippet" result="The snippet points at this host directly">
          <p className="text-[12px] text-muted">
            Switch to <span className="text-fg-2">Customize</span> and copy the snippet. The embed URL already
            targets this origin.
          </p>
        </Step>
      </ol>
    </div>
  );
}

function DnsStatusBadge({ status, detail }: { status: DnsRecordStatus; detail: string }) {
  const tone =
    status === "verified"
      ? "border-st-done/30 text-st-done"
      : status === "missing" || status === "mismatch"
        ? "border-st-blocked/30 text-st-blocked"
        : "border-line-2 text-faint";
  return (
    <span
      className={cn("rounded border px-1.5 py-px font-mono text-[10px]", tone)}
      title={detail}
    >
      {status}
    </span>
  );
}

function Step({
  n,
  title,
  result,
  children,
}: {
  n: number;
  title: string;
  result: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-3">
      <span className="flex h-6 w-6 flex-none items-center justify-center rounded-full border border-line-2 bg-surface-3 font-mono text-[11px] text-muted">
        {n}
      </span>
      <div className="flex flex-col gap-1">
        <div className="text-[13px] font-medium text-fg">{title}</div>
        <div className="text-[11.5px] text-faint">{result}</div>
        <div className="mt-0.5">{children}</div>
      </div>
    </li>
  );
}
