import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { settingsPath } from "@/lib/routes";

type Step = "plan" | "account" | "review";

const TIERS = [
  { id: "free", label: "Free", price: { monthly: 0, annual: 0 }, seats: "Up to 3" },
  { id: "pro", label: "Pro", price: { monthly: 20, annual: 16 }, seats: "Up to 10" },
  { id: "team", label: "Team", price: { monthly: 50, annual: 40 }, seats: "Up to 25" },
  { id: "enterprise", label: "Enterprise", price: null, seats: "Unlimited" },
] as const;

const WORK_EMAIL_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
  "aol.com", "icloud.com", "me.com", "protonmail.com", "pm.me",
]);

function isWorkEmail(email: string): boolean {
  const m = email.match(/@(.+)$/);
  if (!m) return false;
  return !WORK_EMAIL_DOMAINS.has(m[1].toLowerCase());
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function CloudOrgLinkDialog({
  open,
  onOpenChange,
  startAtLink,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Jump straight to the review step — "already have an account" skips Plan and Account. */
  startAtLink?: boolean;
}) {
  const [step, setStep] = React.useState<Step>(startAtLink ? "review" : "plan");
  const [billing, setBilling] = React.useState<"monthly" | "annual">("monthly");
  const [tier, setTier] = React.useState("pro");
  const [orgName, setOrgName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [emailTouched, setEmailTouched] = React.useState(false);

  React.useEffect(() => {
    if (open) {
      setStep(startAtLink ? "review" : "plan");
      setBilling("monthly");
      setTier("pro");
      setOrgName("");
      setEmail("");
      setEmailTouched(false);
    }
  }, [open, startAtLink]);

  const slug = slugify(orgName);
  const emailValid = email.length === 0 || isWorkEmail(email);
  const selectedTier = TIERS.find((t) => t.id === tier) ?? TIERS[1];

  function handleAlreadyHaveAccount() {
    onOpenChange(false);
    window.location.href = settingsPath("deployment/sync");
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {step === "plan" && "Choose a plan"}
            {step === "account" && "Your organization"}
            {step === "review" && "Review and link"}
          </DialogTitle>
          <DialogDescription>
            {step === "plan" && "Seats are people — agents and API keys are not seats."}
            {step === "account" && "A work email keeps recovery and billing in one place."}
            {step === "review" && "Code, memory shards and item content stay on this box."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex gap-1 border-b border-line pb-2 mb-4">
          {(["plan", "account", "review"] as Step[]).map((s, i) => (
            <button
              key={s}
              type="button"
              onClick={() => setStep(s)}
              className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
                step === s
                  ? "bg-surface-4 text-fg"
                  : "text-faint hover:text-muted"
              }`}
            >
              {i + 1}. {s === "plan" ? "Plan" : s === "account" ? "Account" : "Review"}
            </button>
          ))}
        </div>

        {step === "plan" && (
          <PlanStep
            billing={billing}
            onBillingChange={setBilling}
            tier={tier}
            onTierChange={setTier}
            onNext={() => setStep("account")}
          />
        )}
        {step === "account" && (
          <AccountStep
            orgName={orgName}
            onOrgNameChange={setOrgName}
            email={email}
            onEmailChange={(v) => { setEmail(v); if (!emailTouched) setEmailTouched(true); }}
            emailTouched={emailTouched}
            emailValid={emailValid}
            slug={slug}
            onNext={() => setStep("review")}
            onBack={() => setStep("plan")}
          />
        )}
        {step === "review" && (
          <ReviewStep
            tier={selectedTier}
            billing={billing}
            orgName={orgName}
            email={email}
            slug={slug}
            onBack={() => setStep("account")}
            onAlreadyHaveAccount={handleAlreadyHaveAccount}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function PlanStep({
  billing, onBillingChange, tier, onTierChange, onNext,
}: {
  billing: "monthly" | "annual";
  onBillingChange: (b: "monthly" | "annual") => void;
  tier: string;
  onTierChange: (t: string) => void;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onBillingChange("monthly")}
          className={`rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors ${
            billing === "monthly" ? "bg-surface-4 text-fg" : "text-muted hover:text-fg"
          }`}
        >
          Monthly
        </button>
        <button
          type="button"
          onClick={() => onBillingChange("annual")}
          className={`rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors ${
            billing === "annual" ? "bg-surface-4 text-fg" : "text-muted hover:text-fg"
          }`}
        >
          Annual
          <span className="ml-1.5 text-[10px] text-accent">−20%</span>
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        {TIERS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => onTierChange(t.id)}
            className={`rounded-[10px] border p-3 text-left transition-colors ${
              tier === t.id
                ? "border-accent/50 bg-accent/[0.06]"
                : "border-line-2 bg-surface-2 hover:border-line-hover"
            }`}
          >
            <div className="text-[13px] font-semibold">{t.label}</div>
            <div className="mt-0.5 text-[11px] text-muted">{t.seats} seats</div>
            {t.price ? (
              <div className="mt-1.5 text-[12px] text-fg">
                ${billing === "annual" ? t.price.annual : t.price.monthly}
                <span className="text-faint">/mo</span>
              </div>
            ) : (
              <div className="mt-1.5 text-[12px] text-faint">Contact sales</div>
            )}
          </button>
        ))}
      </div>

      <div className="flex justify-end">
        <Button
          size="sm"
          disabled={tier === "enterprise"}
          onClick={onNext}
          title={tier === "enterprise" ? "Enterprise plans route to sales" : undefined}
        >
          {tier === "enterprise" ? "Contact sales" : "Next"}
        </Button>
      </div>
    </div>
  );
}

function AccountStep({
  orgName, onOrgNameChange, email, onEmailChange, emailTouched, emailValid, slug, onNext, onBack,
}: {
  orgName: string;
  onOrgNameChange: (v: string) => void;
  email: string;
  onEmailChange: (v: string) => void;
  emailTouched: boolean;
  emailValid: boolean;
  slug: string;
  onNext: () => void;
  onBack: () => void;
}) {
  const canProceed = orgName.trim().length > 0 && email.length > 0 && emailValid;

  return (
    <div className="space-y-4">
      <div>
        <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
          Organization name
        </label>
        <Input
          value={orgName}
          onChange={(e) => onOrgNameChange(e.target.value)}
          placeholder="e.g. Acme Corp"
          autoFocus
        />
      </div>

      <div>
        <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
          Work email
        </label>
        <Input
          type="email"
          value={email}
          onChange={(e) => onEmailChange(e.target.value)}
          placeholder="you@company.com"
        />
        {emailTouched && email.length > 0 && !emailValid && (
          <p className="mt-1 text-[11px] text-st-blocked">
            Use a work email — personal addresses cannot receive org invitations.
          </p>
        )}
      </div>

      {slug && (
        <div>
          <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
            Org slug
          </label>
          <div className="rounded-[8px] border border-line-2 bg-surface-2 px-3 py-2 font-mono text-[12px] text-muted">
            {slug}
            <span className="ml-1 text-faint">.graphban.cloud</span>
          </div>
        </div>
      )}

      <div className="rounded-[9px] border border-dashed border-line-2 px-3 py-2 text-[12px] text-muted">
        Sign-in: <span className="font-medium text-fg-2">GitHub SSO</span> — invite
        teammates by adding them to the org after creation.
      </div>

      <div className="flex justify-between">
        <Button type="button" variant="outline" size="sm" onClick={onBack}>
          Back
        </Button>
        <Button size="sm" disabled={!canProceed} onClick={onNext}>
          Next
        </Button>
      </div>
    </div>
  );
}

function ReviewStep({
  tier, billing, orgName, email, slug, onBack, onAlreadyHaveAccount,
}: {
  tier: (typeof TIERS)[number];
  billing: "monthly" | "annual";
  orgName: string;
  email: string;
  slug: string;
  onBack: () => void;
  onAlreadyHaveAccount: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-[10px] border border-line-2 bg-surface-2 p-3 space-y-2">
        <div className="flex justify-between text-[12.5px]">
          <span className="text-muted">Plan</span>
          <span className="text-fg-2">{tier.label} — {billing}</span>
        </div>
        <div className="flex justify-between text-[12.5px]">
          <span className="text-muted">Organization</span>
          <span className="text-fg-2">{orgName || "—"}</span>
        </div>
        <div className="flex justify-between text-[12.5px]">
          <span className="text-muted">Email</span>
          <span className="text-fg-2">{email || "—"}</span>
        </div>
        {slug && (
          <div className="flex justify-between text-[12.5px]">
            <span className="text-muted">URL</span>
            <span className="font-mono text-fg-2">{slug}.graphban.cloud</span>
          </div>
        )}
      </div>

      <div className="rounded-[10px] border border-line-2 bg-surface-2 p-3">
        <div className="mb-2 text-[12px] font-semibold text-fg-2">What gets linked</div>
        <ul className="space-y-1.5 text-[12px] text-muted">
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-accent">→</span>
            Items, claims, memory shards, and PRD state flow to the cloud org.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-accent">→</span>
            Code graph summaries are pushed; vectors stay on this box.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-st-blocked">✕</span>
            Source code, raw embeddings, and item content never leave this box.
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5 text-faint">↔</span>
            The connection is outbound only — the cloud org cannot reach in.
          </li>
        </ul>
      </div>

      <div className="flex items-center justify-between">
        <Button type="button" variant="outline" size="sm" onClick={onBack}>
          Back
        </Button>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onAlreadyHaveAccount}
            className="text-[12px] text-muted underline-offset-2 hover:underline"
          >
            Already have an account?
          </button>
          <Button
            size="sm"
            disabled
            title="Cloud org creation is not yet available from self-hosted"
          >
            Create org
          </Button>
        </div>
      </div>
    </div>
  );
}
