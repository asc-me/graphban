import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { settingsPath } from "@/lib/routes";

type Step = "plan" | "account" | "review";

const TIERS = [
  { id: "starter", label: "Starter", price: { monthly: 0, annual: 0 }, note: "Up to 3 agents" },
  { id: "pro", label: "Pro", price: { monthly: 29, annual: 24 }, note: "Up to 10 agents" },
  { id: "team", label: "Team", price: { monthly: 79, annual: 66 }, note: "Up to 25 agents" },
  { id: "enterprise", label: "Enterprise", price: { monthly: -1, annual: -1 }, note: "Contact sales" },
] as const;

const WORK_EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const FREE_EMAIL_DOMAINS = new Set(["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"]);

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
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const navigate = useNavigate();
  const [step, setStep] = React.useState<Step>("plan");
  const [billing, setBilling] = React.useState<"monthly" | "annual">("monthly");
  const [tier, setTier] = React.useState("pro");
  const [orgName, setOrgName] = React.useState("");
  const [contactName, setContactName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [emailTouched, setEmailTouched] = React.useState(false);

  React.useEffect(() => {
    if (open) {
      setStep("plan");
      setBilling("monthly");
      setTier("pro");
      setOrgName("");
      setContactName("");
      setEmail("");
      setEmailTouched(false);
    }
  }, [open]);

  const slug = slugify(orgName);
  const emailError = emailTouched && email.length > 0
    ? (!WORK_EMAIL_RE.test(email)
      ? "Enter a valid email address"
      : FREE_EMAIL_DOMAINS.has(email.split("@")[1]?.toLowerCase() ?? "")
        ? "Use a work email"
        : null)
    : null;

  const selectedTier = TIERS.find((t) => t.id === tier)!;
  const isEnterprise = tier === "enterprise";

  function goSyncSettings() {
    onOpenChange(false);
    navigate(settingsPath("deployment/sync"));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Link to a cloud org</DialogTitle>
          <DialogDescription>
            {step === "plan" && "Choose a plan for your cloud org."}
            {step === "account" && "Tell us about your organization."}
            {step === "review" && "Review and confirm what gets linked."}
          </DialogDescription>
        </DialogHeader>

        <StepIndicator step={step} />

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
            contactName={contactName}
            onContactNameChange={setContactName}
            email={email}
            onEmailChange={setEmail}
            onEmailBlur={() => setEmailTouched(true)}
            emailError={emailError}
            slug={slug}
            onBack={() => setStep("plan")}
            onNext={() => setStep("review")}
          />
        )}
        {step === "review" && (
          <ReviewStep
            tier={selectedTier}
            billing={billing}
            orgName={orgName}
            contactName={contactName}
            email={email}
            slug={slug}
            isEnterprise={isEnterprise}
            onBack={() => setStep("account")}
            onOpenDialog={goSyncSettings}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function StepIndicator({ step }: { step: Step }) {
  const steps: { id: Step; label: string }[] = [
    { id: "plan", label: "Plan" },
    { id: "account", label: "Account" },
    { id: "review", label: "Review" },
  ];
  const idx = steps.findIndex((s) => s.id === step);
  return (
    <div className="mb-4 flex items-center gap-2">
      {steps.map((s, i) => (
        <React.Fragment key={s.id}>
          <span
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold",
              i <= idx ? "bg-accent text-bg" : "bg-surface-4 text-faint",
            )}
          >
            {i + 1}
          </span>
          <span className={cn("text-[11px]", i <= idx ? "text-fg-2" : "text-faint")}>{s.label}</span>
          {i < steps.length - 1 && <span className="mx-1 h-px flex-1 bg-line-2" />}
        </React.Fragment>
      ))}
    </div>
  );
}

function PlanStep({
  billing,
  onBillingChange,
  tier,
  onTierChange,
  onNext,
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
          className={cn(
            "rounded-[8px] border px-2.5 py-1 text-[12px] transition-colors",
            billing === "monthly" ? "border-accent/40 bg-accent/[0.08] text-accent" : "border-line-2 text-muted",
          )}
          onClick={() => onBillingChange("monthly")}
        >
          Monthly
        </button>
        <button
          type="button"
          className={cn(
            "rounded-[8px] border px-2.5 py-1 text-[12px] transition-colors",
            billing === "annual" ? "border-accent/40 bg-accent/[0.08] text-accent" : "border-line-2 text-muted",
          )}
          onClick={() => onBillingChange("annual")}
        >
          Annual
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        {TIERS.map((t) => {
          const selected = t.id === tier;
          const price = billing === "monthly" ? t.price.monthly : t.price.annual;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onTierChange(t.id)}
              className={cn(
                "rounded-[10px] border p-3 text-left transition-colors",
                selected ? "border-accent/50 bg-accent/[0.05]" : "border-line-2 hover:border-line-hover",
              )}
            >
              <div className="text-[13px] font-semibold">{t.label}</div>
              <div className="mt-1 text-[12px] text-muted">
                {price === -1 ? "Custom" : price === 0 ? "Free" : `$${price}/mo`}
              </div>
              <div className="mt-0.5 text-[11px] text-faint">{t.note}</div>
            </button>
          );
        })}
      </div>

      <p className="text-[11px] leading-relaxed text-faint">
        Seats are people — agents and API keys are not seats.
      </p>

      <div className="flex justify-end">
        <Button size="sm" onClick={onNext}>Continue</Button>
      </div>
    </div>
  );
}

function AccountStep({
  orgName,
  onOrgNameChange,
  contactName,
  onContactNameChange,
  email,
  onEmailChange,
  onEmailBlur,
  emailError,
  slug,
  onBack,
  onNext,
}: {
  orgName: string;
  onOrgNameChange: (v: string) => void;
  contactName: string;
  onContactNameChange: (v: string) => void;
  email: string;
  onEmailChange: (v: string) => void;
  onEmailBlur: () => void;
  emailError: string | null;
  slug: string;
  onBack: () => void;
  onNext: () => void;
}) {
  const canContinue = orgName.trim() && contactName.trim() && email.trim() && !emailError;
  return (
    <div className="space-y-3">
      <div>
        <label className="mb-1 block text-[12px] font-medium text-fg-2">Your name</label>
        <Input value={contactName} onChange={(e) => onContactNameChange(e.target.value)} placeholder="Jane Doe" />
      </div>
      <div>
        <label className="mb-1 block text-[12px] font-medium text-fg-2">Work email</label>
        <Input
          value={email}
          onChange={(e) => onEmailChange(e.target.value)}
          onBlur={onEmailBlur}
          placeholder="jane@acme.com"
          type="email"
        />
        {emailError && <p className="mt-1 text-[11px] text-st-blocked">{emailError}</p>}
      </div>
      <div>
        <label className="mb-1 block text-[12px] font-medium text-fg-2">Organization name</label>
        <Input value={orgName} onChange={(e) => onOrgNameChange(e.target.value)} placeholder="Acme Corp" />
      </div>
      {slug && (
        <div className="rounded-[9px] border border-line-2 bg-surface-2 px-3 py-2">
          <span className="text-[11px] text-faint">URL: </span>
          <code className="font-mono text-[11.5px] text-fg-2">cloud.graphban.dev/{slug || "…"}</code>
        </div>
      )}
      <div>
        <p className="text-[11px] text-faint">Authentication via GitHub SSO.</p>
      </div>
      <div className="flex justify-between">
        <Button size="sm" variant="ghost" onClick={onBack}>Back</Button>
        <Button size="sm" onClick={onNext} disabled={!canContinue}>Continue</Button>
      </div>
    </div>
  );
}

function ReviewStep({
  tier,
  billing,
  orgName,
  contactName,
  email,
  slug,
  isEnterprise,
  onBack,
  onOpenDialog,
}: {
  tier: (typeof TIERS)[number];
  billing: "monthly" | "annual";
  orgName: string;
  contactName: string;
  email: string;
  slug: string;
  isEnterprise: boolean;
  onBack: () => void;
  onOpenDialog: () => void;
}) {
  const price = billing === "monthly" ? tier.price.monthly : tier.price.annual;
  const priceLabel = price === -1 ? "Contact sales" : price === 0 ? "Free" : `$${price}/mo ${billing}`;

  return (
    <div className="space-y-4">
      <div className="rounded-[10px] border border-line-2 bg-surface-2 p-3 space-y-1.5">
        <Row label="Organization" value={orgName || "—"} />
        <Row label="Contact" value={contactName || "—"} />
        <Row label="Email" value={email || "—"} />
        <Row label="URL" value={slug ? `cloud.graphban.dev/${slug}` : "—"} />
        <Row label="Plan" value={`${tier.label} — ${priceLabel}`} />
      </div>

      <div className="rounded-[10px] border border-line-2 bg-surface-2 p-3">
        <div className="mb-2 text-[12px] font-medium text-fg-2">What gets linked</div>
        <ul className="space-y-1 text-[12px] text-muted">
          <li className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-accent" />
            Code graph summaries (not vectors — re-embedded cloud-side)
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-accent" />
            Items, requests and PRDs
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-st-done" />
            <span>Memory shards stay on this box</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-st-done" />
            <span>Item content stays on this box</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-st-done" />
            <span>Connection is outbound only</span>
          </li>
        </ul>
      </div>

      <div className="flex items-center justify-between">
        <Button size="sm" variant="ghost" onClick={onBack}>Back</Button>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenDialog}
            className="text-[11.5px] text-muted underline-offset-2 hover:underline"
          >
            Already have an account?
          </button>
          <Button
            size="sm"
            disabled
            title="Cloud org creation is not yet available from self-hosted"
          >
            {isEnterprise ? "Contact sales" : "Create org"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-[11px] text-faint">{label}</span>
      <span className="text-[12.5px] text-fg-2">{value}</span>
    </div>
  );
}
