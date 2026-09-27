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
import { cn } from "@/lib/cn";
import { settingsPath } from "@/lib/routes";
import { useNavigate } from "react-router-dom";

type Step = "plan" | "account" | "review";
type BillingCycle = "monthly" | "annual";
type Tier = "free" | "starter" | "pro" | "enterprise";

const TIERS: { id: Tier; name: string; monthly: number; annual: number; features: string[] }[] = [
  { id: "free", name: "Free", monthly: 0, annual: 0, features: ["1 seat", "Community support"] },
  { id: "starter", name: "Starter", monthly: 12, annual: 10, features: ["3 seats", "Email support", "Code graph sync"] },
  { id: "pro", name: "Pro", monthly: 29, annual: 24, features: ["10 seats", "Priority support", "Code graph sync", "Fleet coordination"] },
  { id: "enterprise", name: "Enterprise", monthly: -1, annual: -1, features: ["Unlimited seats", "Dedicated support", "SSO", "Custom deployment"] },
];

const WORK_EMAIL_DOMAINS = new Set([
  "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
  "icloud.com", "me.com", "protonmail.com", "mail.com",
]);

function isWorkEmail(email: string): boolean {
  const at = email.lastIndexOf("@");
  if (at < 0) return false;
  const domain = email.slice(at + 1).toLowerCase();
  return !WORK_EMAIL_DOMAINS.has(domain);
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function CloudOrgLinkDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [step, setStep] = React.useState<Step>("plan");
  const [billing, setBilling] = React.useState<BillingCycle>("monthly");
  const [tier, setTier] = React.useState<Tier>("pro");
  const [orgName, setOrgName] = React.useState("");
  const [contactName, setContactName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [emailTouched, setEmailTouched] = React.useState(false);
  const navigate = useNavigate();

  const slug = slugify(orgName);
  const emailError = emailTouched && email.length > 0 && !isWorkEmail(email)
    ? "Use a work email address"
    : null;

  function reset() {
    setStep("plan");
    setBilling("monthly");
    setTier("pro");
    setOrgName("");
    setContactName("");
    setEmail("");
    setEmailTouched(false);
  }

  function handleOpenChange(v: boolean) {
    if (!v) reset();
    onOpenChange(v);
  }

  const steps: { id: Step; label: string }[] = [
    { id: "plan", label: "Plan" },
    { id: "account", label: "Account" },
    { id: "review", label: "Review" },
  ];
  const stepIdx = steps.findIndex((s) => s.id === step);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Connect to a cloud org</DialogTitle>
          <DialogDescription>
            Pick a plan, set up your org, then review what gets linked.
          </DialogDescription>
        </DialogHeader>

        <div className="mb-4 flex gap-1">
          {steps.map((s, i) => (
            <button
              key={s.id}
              type="button"
              onClick={() => { if (i <= stepIdx) setStep(s.id); }}
              className={cn(
                "flex-1 rounded-md px-2 py-1.5 text-[11.5px] font-medium transition-colors",
                i === stepIdx
                  ? "bg-accent/15 text-accent"
                  : i < stepIdx
                    ? "bg-surface-2 text-muted hover:bg-surface-3"
                    : "bg-surface-2 text-faint cursor-default",
              )}
            >
              {i + 1}. {s.label}
            </button>
          ))}
        </div>

        {step === "plan" && (
          <PlanStep billing={billing} setBilling={setBilling} tier={tier} setTier={setTier} />
        )}
        {step === "account" && (
          <AccountStep
            orgName={orgName} setOrgName={setOrgName}
            contactName={contactName} setContactName={setContactName}
            email={email} setEmail={setEmail}
            setEmailTouched={setEmailTouched}
            emailError={emailError}
            slug={slug}
          />
        )}
        {step === "review" && (
          <ReviewStep
            billing={billing} tier={tier}
            orgName={orgName} contactName={contactName} email={email} slug={slug}
          />
        )}

        <div className="mt-4 flex items-center justify-between">
          <button
            type="button"
            onClick={() => navigate(settingsPath("deployment/sync"))}
            className="text-[11.5px] text-muted underline-offset-2 hover:underline"
          >
            Already have an account?
          </button>
          <div className="flex gap-2">
            {stepIdx > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setStep(steps[stepIdx - 1].id)}
              >
                Back
              </Button>
            )}
            {stepIdx < 2 ? (
              <Button
                size="sm"
                disabled={
                  (step === "account" && (!orgName.trim() || !contactName.trim() || !email.trim() || !!emailError))
                }
                onClick={() => setStep(steps[stepIdx + 1].id)}
              >
                Next
              </Button>
            ) : (
              <Button
                size="sm"
                disabled
                title="Cloud org creation is not yet available from self-hosted"
              >
                Create org
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PlanStep({
  billing, setBilling, tier, setTier,
}: {
  billing: BillingCycle; setBilling: (b: BillingCycle) => void;
  tier: Tier; setTier: (t: Tier) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setBilling("monthly")}
          className={cn(
            "rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors",
            billing === "monthly" ? "bg-accent/15 text-accent" : "text-muted hover:text-fg-2",
          )}
        >
          Monthly
        </button>
        <button
          type="button"
          onClick={() => setBilling("annual")}
          className={cn(
            "rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors",
            billing === "annual" ? "bg-accent/15 text-accent" : "text-muted hover:text-fg-2",
          )}
        >
          Annual
        </button>
      </div>
      <p className="text-[11px] text-faint">
        Seats are people — agents and API keys are not seats.
      </p>
      <div className="grid grid-cols-2 gap-2">
        {TIERS.map((t) => {
          const price = billing === "monthly" ? t.monthly : t.annual;
          const isEnterprise = t.id === "enterprise";
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setTier(t.id)}
              className={cn(
                "flex flex-col gap-1 rounded-[9px] border p-2.5 text-left transition-colors",
                tier === t.id
                  ? "border-accent/50 bg-accent/[0.04]"
                  : "border-line-2 bg-surface-2 hover:border-line-hover",
              )}
            >
              <span className="text-[12.5px] font-semibold">{t.name}</span>
              <span className="text-[11px] text-muted">
                {isEnterprise ? "Contact sales" : price === 0 ? "Free" : `$${price}/mo`}
              </span>
              <span className="mt-0.5 text-[10px] text-faint">
                {t.features.join(" · ")}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AccountStep({
  orgName, setOrgName, contactName, setContactName,
  email, setEmail, setEmailTouched, emailError, slug,
}: {
  orgName: string; setOrgName: (v: string) => void;
  contactName: string; setContactName: (v: string) => void;
  email: string; setEmail: (v: string) => void;
  setEmailTouched: (v: boolean) => void;
  emailError: string | null;
  slug: string;
}) {
  return (
    <div className="space-y-3">
      <div>
        <label className="mb-1 block text-[11px] font-medium text-muted">Your name</label>
        <Input value={contactName} onChange={(e) => setContactName(e.target.value)} placeholder="Ada Lovelace" />
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-medium text-muted">Work email</label>
        <Input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          onBlur={() => setEmailTouched(true)}
          placeholder="ada@company.com"
        />
        {emailError && <p className="mt-1 text-[10.5px] text-st-review">{emailError}</p>}
      </div>
      <div>
        <label className="mb-1 block text-[11px] font-medium text-muted">Organization name</label>
        <Input value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="Acme Corp" />
      </div>
      {slug && (
        <div className="rounded-[7px] bg-surface-2 px-2.5 py-1.5">
          <span className="text-[10.5px] text-faint">URL: </span>
          <code className="font-mono text-[11px] text-muted">{slug}.graphban.dev</code>
        </div>
      )}
      <div className="rounded-[7px] border border-line-2 bg-surface-2 px-2.5 py-2">
        <p className="text-[11px] text-muted">
          Sign in with <span className="font-medium text-fg-2">GitHub SSO</span> — your GitHub identity
          becomes your org admin identity.
        </p>
      </div>
    </div>
  );
}

function ReviewStep({
  billing, tier, orgName, contactName, email, slug,
}: {
  billing: BillingCycle; tier: Tier;
  orgName: string; contactName: string; email: string; slug: string;
}) {
  const tierName = TIERS.find((t) => t.id === tier)?.name ?? tier;
  const price = billing === "monthly"
    ? TIERS.find((t) => t.id === tier)?.monthly ?? 0
    : TIERS.find((t) => t.id === tier)?.annual ?? 0;

  return (
    <div className="space-y-3">
      <div className="rounded-[9px] border border-line-2 bg-surface-2 p-3">
        <div className="space-y-1.5 text-[12px]">
          <div className="flex justify-between">
            <span className="text-muted">Org</span>
            <span className="text-fg-2">{orgName || "—"}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">Contact</span>
            <span className="text-fg-2">{contactName || "—"}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">Email</span>
            <span className="text-fg-2">{email || "—"}</span>
          </div>
          {slug && (
            <div className="flex justify-between">
              <span className="text-muted">URL</span>
              <code className="font-mono text-[11px] text-fg-2">{slug}.graphban.dev</code>
            </div>
          )}
          <div className="flex justify-between">
            <span className="text-muted">Plan</span>
            <span className="text-fg-2">
              {tierName} {price > 0 ? `· $${price}/mo (${billing})` : ""}
            </span>
          </div>
        </div>
      </div>

      <div className="rounded-[9px] border border-line-2 p-3">
        <p className="mb-2 text-[11.5px] font-medium text-fg-2">What gets linked</p>
        <ul className="space-y-1 text-[11.5px] text-muted">
          <li>✓ Code graph summaries (pushed outbound from this box)</li>
          <li>✓ Items, claims, and memory shards (synced bidirectionally)</li>
          <li className="text-faint">✗ Raw vectors stay on this box — the cloud re-embeds</li>
          <li className="text-faint">✗ Source code never leaves this box</li>
        </ul>
        <p className="mt-2 text-[10.5px] text-faint">
          The connection is outbound only. Code, memory shards and item content stay on this box
          unless you explicitly sync them.
        </p>
      </div>
    </div>
  );
}
