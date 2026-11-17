import { useProjectCtx } from "@/features/ProjectContext";
import { errorDetail } from "@/lib/errors";
import { useSetPublishToOrg } from "@/lib/queries";

// Exactly what each switch sends (GRPH-1674). Item bodies are never sent, whatever is set.
const PUBLISH_FLAGS = [
  { key: "specs", label: "Specs", sends: "spec title, body and status" },
  { key: "item_digests", label: "Item digests", sends: "item title, kind, status and touchpoints — never the item body" },
] as const;

/** Per-project "publish to org" switches (GRPH-1674). On by default; an unlinked box sends nothing.
 * Turning one off purges what was published; turning it back on republishes on the next sync. */
export function PublishToOrgPanel() {
  const { active } = useProjectCtx();
  const set = useSetPublishToOrg(active?.id ?? "");
  if (!active) return null;
  const flags = active.publish_to_org ?? { specs: true, item_digests: true };
  return (
    <div className="mb-4" data-testid="publish-to-org">
      <div className="text-body font-medium text-fg-2">Publish to org</div>
      <p className="mb-2 text-[12px] text-faint">
        What a linked box sends your org for this project. An unlinked box sends nothing. Turning a
        switch off removes what was published; turning it back on republishes on the next sync.
        Only a project owner or admin can change these.
      </p>
      <div className="space-y-2">
        {PUBLISH_FLAGS.map((f) => (
          <label key={f.key} className="flex cursor-pointer items-start gap-2.5 text-body text-fg-2">
            <input
              type="checkbox"
              className="mt-0.5 accent-accent"
              aria-label={`Publish ${f.label.toLowerCase()} to org`}
              checked={flags[f.key]}
              disabled={set.isPending}
              onChange={(e) => set.mutate({ [f.key]: e.target.checked })}
            />
            <span>
              {f.label} <span className="text-faint">— sends {f.sends}.</span>
            </span>
          </label>
        ))}
      </div>
      {set.isError && <p className="mt-1 text-body text-red-500">{errorDetail(set.error, "Could not save")}</p>}
    </div>
  );
}
