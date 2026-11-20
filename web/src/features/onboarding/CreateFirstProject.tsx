import { useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { PROJECT_ACCENTS } from "@/features/onboarding/projectAccents";
import { TagField, useTagField } from "@/features/onboarding/TagField";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";

export function CreateFirstProject() {
  const qc = useQueryClient();
  const { setActiveId } = useProjectCtx();
  const [name, setName] = React.useState("");
  const [accent, setAccent] = React.useState(PROJECT_ACCENTS[0]);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const tagField = useTagField(name);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    if (tagField.blocked) return;
    setBusy(true);
    setError("");
    try {
      const project = await api.createProject({
        name: name.trim(),
        tag: tagField.tag || undefined,
        accent,
      });
      await qc.invalidateQueries({ queryKey: keys.projects });
      setActiveId(project.id);
    } catch {
      setError("Could not create the project. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-w-0 items-center justify-center overflow-x-hidden p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-md min-w-0 rounded-[16px] border border-line bg-surface-3/70 p-7 shadow-[0_24px_60px_rgba(0,0,0,0.4)]"
      >
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-[11px] bg-[rgba(198,242,78,0.12)]">
          <Sparkles size={18} className="text-accent" />
        </div>
        <h1 className="mb-1 text-[17px] font-semibold tracking-tight">Create your first project</h1>
        <p className="mb-6 text-body leading-relaxed text-muted">
          A project is a workspace for your tracker, agent memory, requests, and PRDs. You can add
          more later.
        </p>

        <Field label="Project name" htmlFor="first-project-name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Core Platform"
            autoFocus
            required
          />
        </Field>

        <div className="mb-5">
          <TagField tag={tagField.tag} setTag={tagField.setTag} status={tagField.status} />
        </div>

        <p className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">Accent</p>
        <div className="mb-6 flex flex-wrap gap-2.5">
          {PROJECT_ACCENTS.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setAccent(c)}
              aria-label={`accent ${c}`}
              aria-pressed={accent === c}
              className="h-7 min-h-6 w-7 min-w-6 rounded-[8px] transition-transform focus-visible:outline-2 focus-visible:outline-focus [@media(hover:hover)_and_(pointer:fine)]:hover:scale-110"
              style={{
                background: c,
                // Selection is a box-shadow, not outline. An inline outline beats
                // the global :focus-visible rule, so keyboard focus disappears.
                // The 4px ring sits inside a 5px focus offset. The 2px inner stop
                // is the card surface (bg-surface-3).
                boxShadow:
                  accent === c
                    ? "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)"
                    : undefined,
                outlineOffset: "5px",
              }}
            />
          ))}
        </div>

        {error && (
          <p className="mb-4 text-[12px] text-st-blocked" role="alert">
            {error}
          </p>
        )}

        <Button type="submit" className="min-h-6 w-full" disabled={busy || tagField.blocked}>
          {busy ? "Creating…" : "Create project"}
        </Button>
      </form>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactElement<{ id?: string }>;
}) {
  return (
    <div className="mb-5 min-w-0">
      <label
        htmlFor={htmlFor}
        className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint"
      >
        {label}
      </label>
      {React.cloneElement(children, { id: htmlFor })}
    </div>
  );
}
