/**
 * Settings → Deployment → Updates: the rule pack card (GRPH-1335).
 * Four answers from `/api/rules/pack/available`, and `unknown` (could not check) is never
 * rendered as current. Install needs a Preview first and sends that preview's sha256; a 409
 * asks for a new preview and is never retried.
 */
import { useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { Markdown } from "@/lib/markdown";
import { keys, useConfig, useRulePack, useRulePackAvailable } from "@/lib/queries";
import type { PackCompat, RulePackOffer, RulePackSide } from "@/lib/types";

function Label({ children }: { children: React.ReactNode }) {
  return <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">{children}</div>;
}

function statusOf(err: unknown): number | undefined {
  return (err as { status?: number } | null)?.status;
}

function detailOf(err: unknown): string {
  const raw = err instanceof Error ? err.message : "";
  try {
    const d = JSON.parse(raw)?.detail;
    if (typeof d === "string" && d) return d;
  } catch {
    /* not a FastAPI body */
  }
  return raw || "request failed";
}

function SideDiff({ name, side }: { name: string; side: RulePackSide }) {
  const changed = Object.keys(side.changed);
  if (!side.added.length && !side.removed.length && !changed.length) {
    return <li data-testid={`diff-${name}`}>{name}: no changes</li>;
  }
  return (
    <li data-testid={`diff-${name}`}>
      {name}: {side.added.length} added{side.added.length ? ` (${side.added.join(", ")})` : ""},{" "}
      {side.removed.length} removed{side.removed.length ? ` (${side.removed.join(", ")})` : ""},{" "}
      {changed.length} changed{changed.length ? ` (${changed.join(", ")})` : ""}
    </li>
  );
}

function Diff({ offer }: { offer: RulePackOffer }) {
  const refused = Object.entries(offer.newly_refused);
  return (
    <div className="mt-3 rounded-[10px] border border-line-2 bg-surface p-3 text-[13px] text-fg-2" data-testid="rulepack-diff">
      <Label>Preview {offer.from ?? "none"} → {offer.to}</Label>
      <ul className="space-y-1">
        <SideDiff name="rules" side={offer.rules} />
        <SideDiff name="profiles" side={offer.profiles} />
        <li data-testid="diff-refused">
          newly refused: {refused.length ? refused.map(([k, v]) => `${k} (${v})`).join(", ") : "none"}
        </li>
      </ul>
    </div>
  );
}

function CompatWarning({ compat }: { compat: PackCompat | null | undefined }) {
  if (!compat || compat.state === "compatible") return null;
  return (
    <p role="alert" className="mt-2 text-[13px] text-danger" data-testid="rulepack-compat" data-state={compat.state}>
      {compat.state === "refused"
        ? "The next server release will not load the installed rule pack: "
        : "Could not tell whether the next server release loads the installed rule pack: "}
      {compat.reason}
    </p>
  );
}

export function RulePackCard({ packCompat }: { packCompat?: PackCompat | null }) {
  const qc = useQueryClient();
  const { data: config } = useConfig();
  const pack = useRulePack();
  const avail = useRulePackAvailable();
  const [offer, setOffer] = React.useState<RulePackOffer | null>(null);
  const [busy, setBusy] = React.useState<"" | "preview" | "install">("");
  const [error, setError] = React.useState("");
  const [stale, setStale] = React.useState(false);
  const [done, setDone] = React.useState("");

  const hosted = Boolean(config?.hosted_mode);
  // Hosted, only a platform admin may read the feed or install: a 403 makes the card read-only.
  const readOnly = hosted && statusOf(avail.error) === 403;
  const version = avail.data?.version ?? "";
  const canAct = avail.data?.state === "available" && !!version && !readOnly;

  async function preview() {
    setBusy("preview"); setError(""); setStale(false); setDone("");
    try {
      setOffer(await api.offerRulePack(version));
    } catch (e) {
      setOffer(null);
      setError(detailOf(e));
    } finally {
      setBusy("");
    }
  }

  async function install() {
    if (!offer) return;
    setBusy("install"); setError("");
    try {
      await api.acceptRulePack(version, offer.sha256);
      setDone(`Installed rule pack ${offer.to}.`);
      setOffer(null);
      await Promise.all([
        qc.invalidateQueries({ queryKey: keys.rulePack }),
        qc.invalidateQueries({ queryKey: keys.rulePackAvailable }),
        qc.invalidateQueries({ queryKey: ["rules-flow"] }),
      ]);
    } catch (e) {
      if (statusOf(e) === 409) {
        // What was previewed is not what the feed serves now. Never auto-retry.
        setOffer(null);
        setStale(true);
      } else {
        setError(detailOf(e));
      }
    } finally {
      setBusy("");
    }
  }

  const p = pack.data;
  const a = avail.data;
  return (
    <div className="mb-3 rounded-[13px] border border-line-2 bg-surface-2 p-4" data-testid="rulepack-card">
      <div className="text-lead font-semibold">Rule pack</div>
      <div className="mt-2 text-[13px] text-fg-2" data-testid="rulepack-installed">
        {pack.isPending ? "Loading the installed rule pack…"
          : pack.isError ? "Could not read the installed rule pack."
          : p?.installed ? (
            <>
              Installed <span className="font-mono text-fg">{p.version}</span>
              {p.sha256 ? <span className="font-mono text-faint"> (sha256 {p.sha256})</span> : null}
            </>
          ) : "No rule pack installed"}
      </div>
      {p?.installed && !p.loaded ? (
        <p role="alert" className="mt-2 text-[13px] text-danger" data-testid="rulepack-not-loaded">
          Installed but not in force on this server: {p.warning || p.reason}
        </p>
      ) : null}
      <CompatWarning compat={packCompat} />

      <div className="mt-3" data-testid="rulepack-state" data-state={readOnly ? "read-only" : a?.state ?? (avail.isError ? "unknown" : "loading")}>
        {avail.isPending ? <p className="text-[13px] text-muted">Checking for a newer rule pack…</p> : null}
        {readOnly ? (
          <p className="text-[13px] text-muted">Only a platform admin can check for or install a rule pack on a hosted server.</p>
        ) : null}
        {avail.isError && !readOnly ? (
          <p className="text-[13px] text-danger">Could not check for a newer rule pack: {detailOf(avail.error)}</p>
        ) : null}
        {a?.state === "unknown" ? (
          <p className="text-[13px] text-danger">Could not check for a newer rule pack{a.feed_error ? `: ${a.feed_error}` : "."}</p>
        ) : null}
        {a?.state === "current" ? <p className="text-[13px] font-semibold text-fg">On the latest rule pack</p> : null}
        {a?.state === "available" ? (
          <div className="space-y-2">
            <p className="text-[13px] font-semibold text-fg">
              Rule pack <span className="font-mono">{a.version}</span> is available
            </p>
            {a.notes ? <div className="text-[13px] text-fg-2" data-testid="rulepack-notes"><Markdown source={a.notes} /></div> : null}
          </div>
        ) : null}
      </div>

      {offer ? <Diff offer={offer} /> : null}
      {stale ? (
        <p role="alert" className="mt-2 text-[13px] text-danger" data-testid="rulepack-stale">
          The pack changed since you previewed it. Preview it again before installing.
        </p>
      ) : null}
      {error ? <p role="alert" className="mt-2 text-[13px] text-danger">{error}</p> : null}
      {done ? <p role="status" className="mt-2 text-[13px] font-semibold text-fg">{done}</p> : null}

      {canAct ? (
        <div className="mt-3 flex gap-2">
          <Button type="button" variant="outline" disabled={busy !== ""} onClick={() => { void preview(); }}>
            {busy === "preview" ? "Previewing…" : stale ? "Preview again" : "Preview"}
          </Button>
          <Button type="button" disabled={!offer || busy !== ""} onClick={() => { void install(); }}>
            {busy === "install" ? "Installing…" : "Install"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
