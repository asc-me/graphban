import { Plus } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { useCreateItem, useCreateLine, useItems, useMaintenanceLine, usePrds } from "@/lib/queries";
import type { ItemCreateBody, ItemKind } from "@/lib/types";

type FixScope = "line" | "blocking";

const SELECT_CLS = "h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm";

/** A server refusal carries its instruction in the text; show it, unwrapped from `{detail}`. */
export function refusalText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    const j = JSON.parse(raw);
    if (typeof j?.detail === "string") return j.detail;
    if (j?.detail) return JSON.stringify(j.detail);
  } catch {
    /* plain text */
  }
  return raw;
}

/**
 * `icon={false}` drops the plus — the empty-state card carries the label alone.
 * `openFromQuery` opens the dialog on mount when the URL carries `?new=1`. Only
 * one instance on a page may set it (the tracker header), or an empty tracker —
 * which also renders the empty-state instance — would stack two dialogs.
 */
export function NewItemDialog({
  icon = true,
  openFromQuery = false,
}: { icon?: boolean; openFromQuery?: boolean } = {}) {
  const { activeId } = useProjectCtx();
  const create = useCreateItem(activeId);
  const createLine = useCreateLine(activeId);
  const line = useMaintenanceLine(activeId);
  const prds = usePrds(activeId);
  const items = useItems(activeId);
  const [open, setOpen] = React.useState(
    () =>
      openFromQuery &&
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("new") === "1",
  );
  const [title, setTitle] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [tags, setTags] = React.useState("");
  const [effort, setEffort] = React.useState("");
  const [kind, setKind] = React.useState<ItemKind>("");
  const [scope, setScope] = React.useState<FixScope | "">("");
  const [prdId, setPrdId] = React.useState("");
  const [blocks, setBlocks] = React.useState("");
  const [security, setSecurity] = React.useState(false);
  const [acceptance, setAcceptance] = React.useState("");
  const [touchpoints, setTouchpoints] = React.useState("");
  const [error, setError] = React.useState("");

  const isFix = kind === "fix";
  const lineRow = line.data ?? null;
  const prdItems = (items.data ?? []).filter((i) => prdId && i.prd_id === prdId);
  const acceptanceClauses = acceptance.split("\n").map((l) => l.trim()).filter(Boolean);
  const touchList = touchpoints.split(",").map((t) => t.trim()).filter(Boolean);
  const fixReady = !isFix || (scope === "line" ? !!lineRow : scope === "blocking" ? !!prdId && !!blocks : false);
  const unready = isFix && (acceptanceClauses.length === 0 || touchList.length === 0);

  function reset() {
    setTitle(""); setDescription(""); setTags(""); setEffort(""); setKind(""); setScope("");
    setPrdId(""); setBlocks(""); setSecurity(false); setAcceptance(""); setTouchpoints(""); setError("");
  }

  async function makeLine() {
    setError("");
    try {
      await createLine.mutateAsync();
      await line.refetch();
    } catch (err) {
      setError(refusalText(err));
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || !fixReady) return;
    setError("");
    let desc = description.trim();
    if (isFix && acceptanceClauses.length) {
      desc = `${desc}${desc ? "\n\n" : ""}## Acceptance\n${acceptanceClauses.map((c) => `- ${c}`).join("\n")}`;
    }
    const body: ItemCreateBody = {
      title: title.trim(),
      description: desc,
      tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      effort: Number(effort) || 0,
    };
    if (kind) body.kind = kind;
    if (isFix) {
      body.severity = security ? "security" : "";
      if (touchList.length) body.touchpoints = touchList;
      if (scope === "line" && lineRow) body.line_id = lineRow.id;
      else if (scope === "blocking") {
        body.prd_id = prdId;
        body.blocks = blocks;
      }
    }
    try {
      await create.mutateAsync(body);
    } catch (err) {
      setError(refusalText(err)); // the refusal text is the instruction; the dialog stays open
      return;
    }
    reset();
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          {icon && <Plus size={14} />}
          New item
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New tracker item</DialogTitle>
          <DialogDescription>Drops into the backlog at the top of the stream.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <Input placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
          <select
            aria-label="Kind"
            className={SELECT_CLS}
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as ItemKind);
              if (e.target.value !== "fix") { setScope(""); setSecurity(false); }
            }}
          >
            <option value="">Kind: not recorded</option>
            <option value="feature">Feature</option>
            <option value="fix">Fix</option>
            <option value="chore">Chore</option>
          </select>
          <Textarea
            placeholder="Description (markdown)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
          />
          {isFix && (
            <fieldset className="space-y-2 rounded-md border border-border p-3">
              <legend className="px-1 text-xs text-muted-foreground">Where does this fix go?</legend>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="fix-scope" checked={scope === "line"} onChange={() => setScope("line")} />
                On the maintenance line
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="fix-scope" checked={scope === "blocking"} onChange={() => setScope("blocking")} />
                Blocking a PRD
              </label>
              {scope === "line" && !lineRow && !line.isLoading && (
                <div className="space-y-1 text-sm">
                  <p>This project has no maintenance line yet.</p>
                  <Button type="button" size="sm" variant="outline" onClick={makeLine} disabled={createLine.isPending}>
                    Create the maintenance line
                  </Button>
                </div>
              )}
              {scope === "line" && lineRow && (
                <p className="text-xs text-muted-foreground">Filed on {lineRow.title}.</p>
              )}
              {scope === "blocking" && (
                <div className="space-y-2">
                  <select
                    aria-label="PRD"
                    className={SELECT_CLS}
                    value={prdId}
                    onChange={(e) => { setPrdId(e.target.value); setBlocks(""); }}
                  >
                    <option value="">Choose the PRD</option>
                    {(prds.data ?? []).map((p) => (
                      <option key={p.id} value={p.id}>{p.title}</option>
                    ))}
                  </select>
                  <select
                    aria-label="Blocked item"
                    className={SELECT_CLS}
                    value={blocks}
                    onChange={(e) => setBlocks(e.target.value)}
                    disabled={!prdId}
                  >
                    <option value="">Choose the item this fix blocks</option>
                    {prdItems.map((i) => (
                      <option key={i.id} value={i.id}>{i.id} — {i.title}</option>
                    ))}
                  </select>
                </div>
              )}
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={security} onChange={(e) => setSecurity(e.target.checked)} />
                Severity: Security
              </label>
              <Textarea
                aria-label="Acceptance"
                placeholder="Acceptance clauses, one per line"
                value={acceptance}
                onChange={(e) => setAcceptance(e.target.value)}
                rows={3}
              />
              <Input
                aria-label="Touchpoints"
                placeholder="touchpoints, comma, sep"
                value={touchpoints}
                onChange={(e) => setTouchpoints(e.target.value)}
              />
              {unready && (
                <p className="text-xs text-muted-foreground">
                  Without acceptance clauses and at least one touchpoint this item will not be claimable; it lands as unready.
                </p>
              )}
            </fieldset>
          )}
          <div className="flex gap-3">
            <Input placeholder="tags, comma, sep" value={tags} onChange={(e) => setTags(e.target.value)} />
            <Input
              placeholder="effort"
              type="number"
              value={effort}
              onChange={(e) => setEffort(e.target.value)}
              className="w-24"
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button type="button" variant="outline" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" size="sm" disabled={!title.trim() || !fixReady || create.isPending}>
              Create item
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
