import { useQueryClient } from "@tanstack/react-query";
import { Building2 } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { keys, useCreateOrg } from "@/lib/queries";

/**
 * Hosted onboarding gate (AL-74b): a freshly-registered user has no organization
 * yet, and in hosted mode every project must live under one. This is the first
 * screen they see — create the org, then the usual "create your first project"
 * flow takes over (the project inherits this org).
 */
export function CreateFirstOrg() {
  const qc = useQueryClient();
  const createOrg = useCreateOrg();
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setError("");
    try {
      await createOrg.mutateAsync(name.trim());
      // Projects list is unchanged, but re-fetch orgs so AppFrame advances past this gate.
      await qc.invalidateQueries({ queryKey: keys.orgs });
    } catch {
      setError("Could not create the organization. Please try again.");
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-md rounded-[16px] border border-line bg-surface-3/70 p-7 shadow-[0_24px_60px_rgba(0,0,0,0.4)]"
      >
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-[11px] bg-[rgba(198,242,78,0.12)]">
          <Building2 size={18} className="text-accent" />
        </div>
        <h1 className="mb-1 text-[17px] font-semibold tracking-tight">Create your organization</h1>
        <p className="mb-6 text-body leading-relaxed text-muted">
          Your organization is the home for your team, projects, and billing. You can invite
          teammates once it's set up.
        </p>

        <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
          Organization name
        </label>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Acme Inc."
          autoFocus
          className="mb-6"
        />

        {error && <p className="mb-4 text-[12px] text-st-blocked">{error}</p>}

        <Button type="submit" className="w-full" disabled={createOrg.isPending || !name.trim()}>
          {createOrg.isPending ? "Creating…" : "Create organization"}
        </Button>
      </form>
    </div>
  );
}
