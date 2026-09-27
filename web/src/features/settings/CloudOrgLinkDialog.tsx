import { ArrowRight, Check, ExternalLink, Github, Shield } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { settingsPath } from "@/lib/routes";

type Step = "plan" | "account" | "review";
type Billing = "monthly" | "annual";
type TierId = "free" | "pro" | "team" | "enterprise";

interface TierDef {
  id: TierId;
  name: string;
  monthlyPrice: string;
  annualPrice: string;
  seats: string;
  features: string[];
  highlight?: boolean;
}

const TIERS: TierDef[] = [
  {
    id: "free",
    name: "Free",
    monthlyPrice: "$0",
    annualPrice: "$0",
    seats: "1 seat",
    features: ["1 project", "50 MCP calls / mo", "Community support"],
  },
  {
    id: "pro",
    name: "Pro",
    monthlyPrice: "$20",
    annualPrice: "$16",
    seats: "3 seats",
    features: ["5 projects", "5 000 MCP calls / mo", "Code-graph sync", "Email support"],
    highlight: true,
  },
  {
    id: "team",
    name: "Team",
    monthlyPrice: "$60",
    annualPrice: "$48",
    seats: "10 seats",
    features: ["Unlimited projects", "25 000 MCP calls / mo", "Fleet supervisor", "Priority support"],
  },
  {
    id: "enterprise",
    name: "Enterprise",
    monthlyPrice: "Custom",
    annualPrice: "Custom",
    seats: "Unlimited",
    features: ["SSO / SAML", "Audit log", "Dedicated infra", "Talk to sales"],
  },
];

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

function isWorkEmail(email: string): boolean {
  const free = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"];
  const m = email.match(/@(.+)$/);
  if (!m) return false;
  return !free.includes(m[1].toLowerCase());
}

export function CloudOrgLinkDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const navigate = useNavigate();
  const [step, setStep] = React.useState<Step>("plan");
  const [billing, setBilling] = React.useState<Billing>("monthly");
  const [tier, setTier] = React.useState<TierId>("pro");

  const [fullName, setFullName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [orgName, setOrgName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [slugEdited, setSlugEdited] = React.useState(false);
  const [emailTouched, setEmailTouched] = React.useState(false);

  React.useEffect(() => {
    if (open) {
      setStep("plan");
      setBilling("monthly");
      setTier("pro");
      setFullName("");
      setEmail("");
      setOrgName("");
      setSlug("");
      setSlugEdited(false);
      setEmailTouched(false);
    }
  }, [open]);

  React.useEffect(() => {
    if (!slugEdited && orgName) {
      setSlug(slugify(orgName));
    }
  }, [orgName, slugEdited]);

  const selectedTier = TIERS.find((t) => t.id === tier)!;
  const emailValid = email.length > 0 && isWorkEmail(email);
  const emailShowError = emailTouched && email.length > 0 && !emailValid;
  const accountReady = fullName.trim() && emailValid && orgName.trim() && slug.length > 0;

  function goLinkExisting() {
    onOpenChange(false);
    navigate(settingsPath("deployment/sync"));
  }

  function goEnterprise() {
    onOpenChange(false);
    window.open("mailto:sales@graphban.dev?subject=Enterprise%20inquiry", "_blank");
  }

  const steps: Step[] = ["plan", "account", "review"];
  const stepIdx = steps.indexOf(step);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Connect to Graphban Cloud</DialogTitle>
        </DialogHeader>

        <StepIndicator current={stepIdx} labels={["Plan", "Account", "Review"]} />

        <div className="mt-4">
          {step === "plan" && (
            <PlanStep
              billing={billing}
              onBillingChange={setBilling}
              tier={tier}
              onTierChange={(t) => {
                if (t === "enterprise") {
                  goEnterprise();
                } else {
                  setTier(t);
                }
              }}
              onNext={() => setStep("account")}
            />
          )}
          {step === "account" && (
            <AccountStep
              fullName={fullName}
              onFullNameChange={setFullName}
              email={email}
              onEmailChange={(v) => { setEmail(v); if (!emailTouched) setEmailTouched(true); }}
              onEmailBlur={() => setEmailTouched(true)}
              emailShowError={emailShowError}
              orgName={orgName}
              onOrgNameChange={setOrgName}
              slug={slug}
              onSlugChange={(v) => { setSlug(v); setSlugEdited(true); }}
              onNext={() => setStep("review")}
              onBack={() => setStep("plan")}
              disabled={!accountReady}
            />
          )}
          {step === "review" && (
            <ReviewStep
              tier={selectedTier}
              billing={billing}
              fullName={fullName}
              email={email}
              orgName={orgName}
              slug={slug}
              onBack={() => setStep("account")}
              onLinkExisting={goLinkExisting}
            />
          )}
        </div>

        <div className="mt-4 border-t border-line pt-3 text-center">
          <button
            type="button"
            onClick={goLinkExisting}
            className="text-[11.5px] text-muted hover:text-fg-2"
          >
            Already have an account? <span className="text-accent">Link this instance</span>
          </button>
        </div>
        <DialogClose asChild>
          <button
            type="button"
            className="absolute right-4 top-4 text-faint hover:text-fg"
            aria-label="Close"
          />
        </DialogClose>
      </DialogContent>
    </Dialog>
  );
}

function StepIndicator({ current, labels }: { current: number; labels: string[] }) {
  return (
    <div className="flex items-center gap-1.5">
      {labels.map((label, i) => (
        <React.Fragment key={label}>
          {i > 0 && (
            <div className={cn("h-px flex-1", i <= current ? "bg-accent/40" : "bg-line")} />
          )}
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full font-mono text-[9px] font-semibold",
                i < current
                  ? "bg-accent text-bg"
                  : i === current
                    ? "border border-accent/50 bg-accent/10 text-accent"
                    : "border border-line-2 text-faint",
              )}
            >
              {i < current ? <Check size={10} strokeWidth={3} /> : i + 1}
            </span>
            <span
              className={cn(
                "font-mono text-[9.5px] uppercase tracking-wide",
                i <= current ? "text-fg-2" : "text-faint",
              )}
            >
              {label}
            </span>
          </div>
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
  billing: Billing;
  onBillingChange: (b: Billing) => void;
  tier: TierId;
  onTierChange: (t: TierId) => void;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-[12.5px] text-muted">
          Pick a plan for your cloud org.
        </p>
        <BillingToggle billing={billing} onChange={onBillingChange} />
      </div>

      <div className="grid grid-cols-2 gap-2">
        {TIERS.map((t) => {
          const selected = tier === t.id;
          const price = billing === "monthly" ? t.monthlyPrice : t.annualPrice;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onTierChange(t.id)}
              className={cn(
                "flex flex-col rounded-[11px] border p-3 text-left transition-colors",
                selected
                  ? "border-accent/50 bg-accent/[0.04]"
                  : "border-line-2 bg-surface hover:border-line-hover",
                t.highlight && !selected && "border-line-hover",
              )}
            >
              <div className="flex items-baseline justify-between">
                <span className="text-[13px] font-semibold">{t.name}</span>
                <span className="font-mono text-[12px] text-fg-2">{price}</span>
              </div>
              {price !== "Custom" && (
                <span className="font-mono text-[9.5px] text-faint">
                  /mo{billing === "annual" ? " · billed annually" : ""}
                </span>
              )}
              <span className="mt-1.5 font-mono text-[10px] text-muted">{t.seats}</span>
              <ul className="mt-2 space-y-1">
                {t.features.map((f) => (
                  <li key={f} className="flex items-center gap-1.5 text-[11px] text-muted">
                    <Check size={10} className="flex-none text-accent" />
                    {f}
                  </li>
                ))}
              </ul>
            </button>
          );
        })}
      </div>

      <p className="rounded-[9px] border border-line bg-surface px-3 py-2 text-[11.5px] leading-relaxed text-muted">
        <Shield size={12} className="mr-1.5 inline-block text-faint" />
        Seats are people. Agents and API keys do not count against your seat limit.
      </p>

      <div className="flex justify-end">
        <Button size="sm" onClick={onNext}>
          Continue <ArrowRight size={13} />
        </Button>
      </div>
    </div>
  );
}

function BillingToggle({
  billing,
  onChange,
}: {
  billing: Billing;
  onChange: (b: Billing) => void;
}) {
  return (
    <div className="flex rounded-[8px] border border-line-2 bg-surface p-0.5">
      {(["monthly", "annual"] as const).map((b) => (
        <button
          key={b}
          type="button"
          onClick={() => onChange(b)}
          className={cn(
            "rounded-[6px] px-2.5 py-1 font-mono text-[10px] uppercase tracking-wide transition-colors",
            billing === b
              ? "bg-surface-3 text-fg-2"
              : "text-faint hover:text-muted",
          )}
        >
          {b}
          {b === "annual" && (
            <span className="ml-1 text-accent">−20%</span>
          )}
        </button>
      ))}
    </div>
  );
}

function AccountStep({
  fullName,
  onFullNameChange,
  email,
  onEmailChange,
  onEmailBlur,
  emailShowError,
  orgName,
  onOrgNameChange,
  slug,
  onSlugChange,
  onNext,
  onBack,
  disabled,
}: {
  fullName: string;
  onFullNameChange: (v: string) => void;
  email: string;
  onEmailChange: (v: string) => void;
  onEmailBlur: () => void;
  emailShowError: boolean;
  orgName: string;
  onOrgNameChange: (v: string) => void;
  slug: string;
  onSlugChange: (v: string) => void;
  onNext: () => void;
  onBack: () => void;
  disabled: boolean;
}) {
  return (
    <div className="space-y-3.5">
      <div>
        <Label>Your name</Label>
        <Input
          value={fullName}
          onChange={(e) => onFullNameChange(e.target.value)}
          placeholder="Ada Lovelace"
          autoFocus
        />
      </div>

      <div>
        <Label>Work email</Label>
        <Input
          type="email"
          value={email}
          onChange={(e) => onEmailChange(e.target.value)}
          onBlur={onEmailBlur}
          placeholder="ada@company.com"
        />
        {emailShowError && (
          <p className="mt-1 text-[11px] text-st-review">
            Use a work email — free providers (Gmail, Yahoo…) are not supported.
          </p>
        )}
      </div>

      <div>
        <Label>Organization name</Label>
        <Input
          value={orgName}
          onChange={(e) => onOrgNameChange(e.target.value)}
          placeholder="Acme Corp"
        />
      </div>

      <div>
        <Label>
          Slug <span className="text-faint-2">(URL-safe identifier)</span>
        </Label>
        <Input
          value={slug}
          onChange={(e) => onSlugChange(e.target.value)}
          placeholder="acme-corp"
        />
        {slug && (
          <p className="mt-1 font-mono text-[11px] text-faint">
            cloud.graphban.dev/<span className="text-muted">{slug}</span>
          </p>
        )}
      </div>

      <div className="pt-1">
        <Button variant="outline" size="sm" className="w-full" disabled>
          <Github size={14} />
          Continue with GitHub SSO
        </Button>
        <p className="mt-1.5 text-center text-[10.5px] text-faint">
          Verifies your identity — you can set a password later.
        </p>
      </div>

      <div className="flex justify-between pt-1">
        <Button variant="ghost" size="sm" onClick={onBack}>
          Back
        </Button>
        <Button size="sm" onClick={onNext} disabled={disabled}>
          Continue <ArrowRight size={13} />
        </Button>
      </div>
    </div>
  );
}

function ReviewStep({
  tier,
  billing,
  fullName,
  email,
  orgName,
  slug,
  onBack,
  onLinkExisting,
}: {
  tier: TierDef;
  billing: Billing;
  fullName: string;
  email: string;
  orgName: string;
  slug: string;
  onBack: () => void;
  onLinkExisting: () => void;
}) {
  const price = billing === "monthly" ? tier.monthlyPrice : tier.annualPrice;
  return (
    <div className="space-y-4">
      <div className="rounded-[11px] border border-line bg-surface p-3.5">
        <div className="mb-2.5 font-mono text-[10px] uppercase tracking-wide text-faint">
          Summary
        </div>
        <dl className="space-y-1.5 text-[12.5px]">
          <Row label="Plan" value={`${tier.name} · ${price}/mo`} />
          <Row label="Billing" value={billing === "annual" ? "Annual" : "Monthly"} />
          <Row label="Name" value={fullName} />
          <Row label="Email" value={email} />
          <Row label="Organization" value={orgName} />
          <Row label="URL" value={`cloud.graphban.dev/${slug}`} last />
        </dl>
      </div>

      <div className="rounded-[11px] border border-line-2 bg-surface-2 p-3.5">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-wide text-faint">
          What gets linked
        </div>
        <ul className="space-y-2 text-[12px] leading-relaxed text-muted">
          <li className="flex items-start gap-2">
            <Check size={13} className="mt-0.5 flex-none text-accent" />
            <span>
              <span className="text-fg-2">Code-graph summaries</span> are pushed to the cloud
              so triage and clustering can reason across your repo.
            </span>
          </li>
          <li className="flex items-start gap-2">
            <Check size={13} className="mt-0.5 flex-none text-accent" />
            <span>
              The connection is <span className="text-fg-2">outbound only</span> — the cloud
              never reaches into this box.
            </span>
          </li>
        </ul>
        <div className="mt-3 rounded-[9px] border border-line bg-surface px-3 py-2">
          <p className="text-[11.5px] leading-relaxed text-muted">
            <span className="font-medium text-fg-2">Stays on this box:</span> code, memory
            shards, item content, vectors. Only summaries and structure are pushed — the cloud
            re-embeds from those.
          </p>
        </div>
      </div>

      <div className="flex justify-between pt-1">
        <Button variant="ghost" size="sm" onClick={onBack}>
          Back
        </Button>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onLinkExisting}>
            <ExternalLink size={13} />
            Link existing
          </Button>
          <Button size="sm" disabled title="Cloud org creation is not yet available from self-hosted">
            Create org
          </Button>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value, last }: { label: string; value: string; last?: boolean }) {
  return (
    <div className={cn("flex items-baseline justify-between gap-3", !last && "border-b border-line/50 pb-1.5")}>
      <dt className="text-muted">{label}</dt>
      <dd className="truncate font-mono text-[11.5px] text-fg-2">{value}</dd>
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint">
      {children}
    </label>
  );
}
