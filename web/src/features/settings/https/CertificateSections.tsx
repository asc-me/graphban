/**
 * HTTPS tier 2/3 (GRPH-1637): bring your own certificate, Let's Encrypt, and the redirect
 * switch. The key is typed into / dropped onto this page and sent once; nothing here reads one
 * back — the API has no route that returns it.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { useHttpsCertificate } from "@/lib/queries";
import type { HttpsCustom, HttpsRefusal } from "@/lib/api";

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString() : "");

/** The API's 422 body is JSON text: {"detail":{"message","reasons":[{code,message}]}}. */
export function refusals(err: Error | null): HttpsRefusal[] {
  if (!err) return [];
  try {
    const d = JSON.parse(err.message)?.detail;
    if (Array.isArray(d?.reasons)) return d.reasons;
    if (typeof d === "string") return [{ code: "error", message: d }];
  } catch {
    /* not JSON */
  }
  return [{ code: "error", message: err.message }];
}

function DropZone({ label, value, onText }: { label: string; value: string; onText: (t: string) => void }) {
  return (
    <div className="block space-y-1 text-[13px]">
      <span className="font-semibold">{label}</span>
      <input type="file" aria-label={`${label} file`} className="block text-[12px]"
        onChange={async (e) => { const f = e.target.files?.[0]; if (f) onText(await f.text()); }} />
      <textarea aria-label={label} rows={3} value={value} onChange={(e) => onText(e.target.value)}
        placeholder="-----BEGIN …-----"
        className="w-full rounded border border-control bg-transparent px-2 py-1 font-mono text-small" />
    </div>
  );
}

export function BringYourOwn({ custom }: { custom: HttpsCustom | null | undefined }) {
  const { upload, stop } = useHttpsCertificate();
  const [cert, setCert] = React.useState("");
  const [key, setKey] = React.useState("");
  const [address, setAddress] = React.useState("");
  const [confirming, setConfirming] = React.useState(false);
  const [stopped, setStopped] = React.useState(false);
  const bad = refusals(upload.error as Error | null);
  const up = custom?.kind === "upload" ? custom : null;
  const warn = up?.warnings ?? [];
  return (
    <section id="bring-your-own" aria-label="Bring your own certificate" className="space-y-2">
      <h3 className="text-lead font-semibold">Bring your own certificate</h3>
      {up ? <UploadState c={up} /> : null}
      {up && !confirming ? (
        <Button onClick={() => setConfirming(true)}>Stop using this certificate</Button>
      ) : null}
      {up && confirming ? (
        <div role="group" aria-label="Confirm stop" className="space-y-1 text-[13px]">
          <p>
            This deletes the uploaded files and the key and returns to the box's own certificate.
            The redirect turns off.
          </p>
          <Button onClick={() => stop.mutate(undefined, { onSuccess: () => { setConfirming(false); setStopped(true); } })}>
            Yes, stop using it
          </Button>{" "}
          <Button onClick={() => setConfirming(false)}>Keep it</Button>
        </div>
      ) : null}
      {stopped && !up ? (
        <p className="text-[13px] text-fg-2" data-testid="stopped">
          Stopped. The uploaded files and key are deleted and the box uses its own certificate again.
        </p>
      ) : null}
      {!up || up.state === "expired" ? (
        <form className="space-y-2" onSubmit={(e) => {
          e.preventDefault();
          upload.mutate({ certificate: cert, key, address: address.trim() }, {
            onSuccess: () => { setCert(""); setKey(""); },
          });
        }}>
          <DropZone label="Certificate chain" value={cert} onText={setCert} />
          <DropZone label="Private key" value={key} onText={setKey} />
          <div className="block space-y-1 text-[13px]">
            <span className="font-semibold">Box address</span>
            <input aria-label="Box address" value={address} onChange={(e) => setAddress(e.target.value)}
              placeholder="graphban.acme.com"
              className="w-full rounded border border-control bg-transparent px-2 py-1 text-[13px]" />
          </div>
          <Button type="submit" disabled={upload.isPending || !cert || !key || !address.trim()}>
            {upload.isPending ? "Checking…" : "Check and use"}
          </Button>
        </form>
      ) : null}
      {bad.length ? (
        <div role="alert" className="space-y-1 text-[13px] text-danger" data-testid="refused">
          <p className="font-semibold">This certificate was not used:</p>
          <ul className="list-disc pl-5">{bad.map((r) => <li key={r.code}>{r.message}</li>)}</ul>
        </div>
      ) : null}
      {warn.includes("wildcard") ? (
        <p role="status" className="text-[13px] text-fg-2" data-testid="warn-wildcard">
          This is a wildcard certificate. It covers every name under its domain, so keep its key private.
        </p>
      ) : null}
      {warn.includes("address_not_here") ? (
        <p role="status" className="text-[13px] text-fg-2" data-testid="warn-address">
          {up?.box_address} does not point at this box yet. It is in use anyway; DNS may still be
          catching up.
        </p>
      ) : null}
    </section>
  );
}

function UploadState({ c }: { c: HttpsCustom }) {
  if (c.state === "expired") {
    return (
      <div role="alert" className="space-y-1 text-[13px]" data-testid="expired">
        <p className="font-semibold">Back on the box's own certificate</p>
        <p>{c.reason.charAt(0).toUpperCase() + c.reason.slice(1)}.</p>
        <p className="text-muted">
          Previous file: {c.subject} <span className="rounded border border-line px-1 text-small">EXPIRED</span>{" "}
          {day(c.not_after)}
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-1 text-[13px]" data-testid="serving">
      <p className="font-semibold">Serving your certificate</p>
      <p className="text-fg-2">{c.subject} for {c.box_address}. Valid until {day(c.not_after)}.</p>
      {c.state === "expiring-soon" ? (
        <p role="status" data-testid="expiring-soon">
          Expiring soon, on {day(c.not_after)}. Upload a new one before then; at expiry the box
          returns to its own certificate.
        </p>
      ) : null}
    </div>
  );
}

export function LetsEncrypt({ custom }: { custom: HttpsCustom | null | undefined }) {
  const { letsEncrypt, stop } = useHttpsCertificate();
  const [domain, setDomain] = React.useState("");
  const [confirming, setConfirming] = React.useState(false);
  const le = custom?.kind === "letsencrypt" ? custom : null;
  const bad = refusals(letsEncrypt.error as Error | null);
  return (
    <section id="graphban-cloud" aria-label="Let's Encrypt" className="space-y-2">
      <h3 className="text-lead font-semibold">Let's Encrypt</h3>
      {le?.state === "pending" ? (
        <p role="status" className="text-[13px]" data-testid="le-pending">
          Asking Let's Encrypt for a certificate for {le.box_address}… The box keeps serving its
          current certificate meanwhile.
        </p>
      ) : null}
      {le?.state === "failed" ? (
        <div role="alert" className="text-[13px]" data-testid="le-failed">
          <p className="font-semibold">Let's Encrypt could not issue a certificate</p>
          <p>{le.reason}</p>
          <p className="text-muted">The box is still serving its current certificate.</p>
        </div>
      ) : null}
      {le?.state === "active" ? (
        <p className="text-[13px]" data-testid="le-active">
          Serving a Let's Encrypt certificate for {le.box_address}. It renews itself.
        </p>
      ) : null}
      {le && !confirming ? <Button onClick={() => setConfirming(true)}>Stop using Let's Encrypt</Button> : null}
      {le && confirming ? (
        <div role="group" aria-label="Confirm stop" className="space-y-1 text-[13px]">
          <p>This returns to the box's own certificate and turns the redirect off.</p>
          <Button onClick={() => stop.mutate(undefined, { onSuccess: () => setConfirming(false) })}>
            Yes, stop using it
          </Button>{" "}
          <Button onClick={() => setConfirming(false)}>Keep it</Button>
        </div>
      ) : null}
      {!le ? (
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); letsEncrypt.mutate(domain.trim()); }}>
          <input aria-label="Public domain" value={domain} onChange={(e) => setDomain(e.target.value)}
            placeholder="graphban.acme.com"
            className="flex-1 rounded border border-control bg-transparent px-2 py-1 text-[13px]" />
          <Button type="submit" disabled={!domain.trim() || letsEncrypt.isPending}>Get a certificate</Button>
        </form>
      ) : null}
      {bad.length ? <p role="alert" className="text-[13px] text-danger">{bad[0].message}</p> : null}
    </section>
  );
}

export function RedirectSwitch({ custom }: { custom: HttpsCustom | null | undefined }) {
  const { redirect } = useHttpsCertificate();
  const available = !!custom && ["active", "expiring-soon"].includes(custom.state);
  if (!available) {
    return (
      <section aria-label="Redirect" className="space-y-1">
        <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Redirect http to https</div>
        <p className="text-[13px] text-muted">
          Not available with this box's own certificate. It arrives with a public certificate.
        </p>
      </section>
    );
  }
  const on = custom!.redirect_on;
  return (
    <section aria-label="Redirect" className="space-y-1">
      <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">Redirect http to https</div>
      <label className="flex items-center gap-2 text-[13px]">
        <input type="checkbox" role="switch" aria-label="Redirect http to https" checked={on}
          disabled={redirect.isPending} onChange={(e) => redirect.mutate(e.target.checked)} />
        {on ? "On" : "Off"}
      </label>
      <p className="text-[13px] text-fg-2" data-testid="redirect-copy">
        {on
          ? `Plain http:// requests go to https://${custom!.box_address}/, the box address, whatever address they arrived on.`
          : "Plain http:// requests are served as they are. Turn this on to send them to HTTPS."}
      </p>
      {redirect.error ? <p role="alert" className="text-[13px] text-danger">{(redirect.error as Error).message}</p> : null}
    </section>
  );
}
