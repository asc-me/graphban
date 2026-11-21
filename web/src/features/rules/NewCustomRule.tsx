/**
 * New custom rule (GRPH-1810, P91 D1): the form in a stage editor. The primitive list is read
 * from the contract and holds only the primitives whose `stage` is this stage; the id is
 * `custom.<words-joined-by-hyphens>` from the title, editable, hyphens only. A project admin's
 * save holds at once; the server's refusal is shown verbatim.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useCreateCustomRule, useRulePrimitiveContract } from "@/lib/queries";
import type { RulePrimitive, RuleStage } from "@/lib/types";

/** "Migrations show Postgres" -> "migrations-show-postgres". */
export function slugFromTitle(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/, "");
}

/** Only the primitives the contract puts in this stage. */
export function primitivesForStage(all: RulePrimitive[] | undefined, stage: RuleStage): RulePrimitive[] {
  return (all ?? []).filter((p) => p.stage.toLowerCase() === stage.title.toLowerCase());
}

const FIELD = "h-9 w-full rounded-[9px] border border-control bg-surface-2 px-3 text-[13px] text-fg";

/** The control for the project admin who may write rules; members never see it. */
export function NewCustomRule({ projectId, stage, canWrite }: { projectId: string; stage: RuleStage; canWrite: boolean }) {
  const { data: contract } = useRulePrimitiveContract();
  const create = useCreateCustomRule(projectId);
  const [open, setOpen] = React.useState(false);
  const [primitive, setPrimitive] = React.useState("");
  const [params, setParams] = React.useState<Record<string, string>>({});
  const [title, setTitle] = React.useState("");
  const [why, setWhy] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [slugEdited, setSlugEdited] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (!canWrite) return null;
  const prims = primitivesForStage(contract?.primitives, stage);
  const chosen = prims.find((p) => p.id === primitive) ?? prims[0];
  const shownSlug = slugEdited ? slug : slugFromTitle(title);

  if (!open) {
    return (
      <Button size="sm" variant="outline" className="self-start" onClick={() => setOpen(true)} data-testid="rules-new-custom-open">
        New custom rule
      </Button>
    );
  }

  function value(type: string, raw: string): unknown {
    if (type === "list") return raw.split(",").map((x) => x.trim()).filter(Boolean);
    if (type === "int" || type === "float") return raw.trim() === "" ? raw : Number(raw);
    if (type === "bool") return raw === "true";
    return raw;
  }

  async function save() {
    if (!chosen) return;
    setError(null);
    const out: Record<string, unknown> = {};
    for (const p of chosen.params) {
      const raw = params[p.name];
      if (raw !== undefined && raw !== "") out[p.name] = value(p.type, raw);
    }
    try {
      await create.mutateAsync({
        slug: shownSlug,
        primitive: chosen.id,
        title: title.trim(),
        why: why.trim(),
        reason: why.trim(),
        params: out,
      });
    } catch (err) {
      setError(errorDetail(err, "The server refused the rule."));
      return;
    }
    setOpen(false);
    setTitle("");
    setWhy("");
    setSlug("");
    setSlugEdited(false);
    setParams({});
  }

  return (
    <div className="flex flex-col gap-2 rounded-[12px] border border-[#2a333a] p-3" data-testid="rules-new-custom">
      <span className="font-mono text-[10px] font-semibold tracking-[0.06em] text-accent">NEW CUSTOM RULE · {stage.title.toUpperCase()}</span>
      <label className="flex flex-col gap-1 text-[12px] text-fg-2">
        Primitive
        <select
          aria-label="Primitive"
          className={FIELD}
          value={chosen?.id ?? ""}
          onChange={(e) => { setPrimitive(e.target.value); setParams({}); }}
        >
          {prims.map((p) => (
            <option key={p.id} value={p.id}>{p.id}</option>
          ))}
        </select>
      </label>
      {chosen?.params.map((p) => (
        <label key={p.name} className="flex flex-col gap-1 text-[12px] text-fg-2">
          {p.name}{p.type === "list" ? " (comma-separated)" : ""}
          <Input aria-label={p.name} value={params[p.name] ?? ""} onChange={(e) => setParams((x) => ({ ...x, [p.name]: e.target.value }))} />
        </label>
      ))}
      <label className="flex flex-col gap-1 text-[12px] text-fg-2">
        Title
        <Input aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-fg-2">
        Why
        <Input aria-label="Why" value={why} onChange={(e) => setWhy(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-fg-2">
        Id
        <div className="flex items-center gap-1 font-mono text-[12px]">
          <span>custom.</span>
          <Input
            aria-label="Id"
            value={shownSlug}
            onChange={(e) => { setSlugEdited(true); setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-")); }}
          />
        </div>
        <span className={cn("font-mono text-[10.5px] text-muted-2")} data-testid="rules-new-custom-id">{`custom.${shownSlug}`}</span>
      </label>
      {error && <p role="alert" data-testid="rules-new-custom-error" className="text-[12px] leading-snug text-st-blocked">{error}</p>}
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
        <Button size="sm" disabled={!chosen || !title.trim() || !why.trim() || !shownSlug || create.isPending} onClick={() => void save()} data-testid="rules-new-custom-save">
          Save rule
        </Button>
      </div>
    </div>
  );
}
