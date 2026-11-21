import { useQuery } from "@tanstack/react-query";
import * as React from "react";
import { useLocation } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/features/auth/AuthContext";
import { OrganizationsSection } from "@/features/profile/OrganizationsSection";
import { PasswordCard } from "@/features/profile/PasswordCard";
import {
  changedLabels,
  draftFrom,
  handleCheckProblem,
  handleFormatProblem,
  patchFrom,
  type ProfileDraft,
} from "@/features/profile/profileForm";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { DOT_COLOURS } from "@/lib/dot";
import { errorDetail } from "@/lib/errors";
import { PROJECT_PATTERNS } from "@/lib/projectFill";
import { useConfig } from "@/lib/queries";
import type { User } from "@/lib/types";

/** How long after the handle field loses focus before the server is asked. */
export const HANDLE_CHECK_DELAY_MS = 350;

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="rounded-[14px] border border-line-2 bg-surface-2 p-5">
      <h2 className="mb-3 font-mono text-[10px] uppercase tracking-wide text-faint">{title}</h2>
      {children}
    </section>
  );
}

function AccountRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-3 border-t border-line-2 py-2 text-body first:border-t-0">
      <span className="w-40 flex-none text-muted">{label}</span>
      <span className="min-w-0 flex-1 text-fg-2">{value}</span>
    </div>
  );
}

export function ProfileView() {
  const { user } = useAuth();
  if (!user) return null;
  return <ProfileForm user={user} />;
}

function ProfileForm({ user }: { user: User }) {
  const { setUser } = useAuth();
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  const { hash } = useLocation();
  const { data: memberships = [] } = useQuery({
    queryKey: ["my-memberships"],
    queryFn: () => api.myMemberships(),
  });

  const saved = React.useMemo(() => draftFrom(user), [user]);
  const [draft, setDraft] = React.useState<ProfileDraft>(saved);
  const [serverProblem, setServerProblem] = React.useState<string | null>(null);
  const [checking, setChecking] = React.useState(false);
  /** The handle the server last said was free. */
  const [freeHandle, setFreeHandle] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState("");
  const timer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const checkSeq = React.useRef(0);

  React.useEffect(() => () => clearTimeout(timer.current), []);

  // `#organizations` scrolls to its section (the Organizations menu entry).
  React.useEffect(() => {
    if (hash === "#organizations") document.getElementById("organizations")?.scrollIntoView?.();
  }, [hash]);

  const set = (patch: Partial<ProfileDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const handleChanged = draft.handle !== saved.handle;
  const formatProblem = handleChanged ? handleFormatProblem(draft.handle) : null;
  const handleProblem = formatProblem ?? (handleChanged ? serverProblem : null);

  function onHandleChange(value: string) {
    clearTimeout(timer.current);
    checkSeq.current += 1; // an answer for an older value is stale
    setServerProblem(null);
    setChecking(false);
    set({ handle: value.trim().replace(/^@/, "").toLowerCase() });
  }

  function onHandleBlur() {
    clearTimeout(timer.current);
    const value = draft.handle;
    if (value === saved.handle || handleFormatProblem(value)) return;
    const seq = ++checkSeq.current;
    setChecking(true);
    timer.current = setTimeout(() => {
      api
        .handleCheck(value)
        .then((r) => {
          if (seq !== checkSeq.current) return;
          const problem = handleCheckProblem(r);
          setServerProblem(problem);
          setFreeHandle(problem === null ? value : null);
        })
        .catch((err) => {
          if (seq === checkSeq.current) setServerProblem(errorDetail(err, "could not check the handle"));
        })
        .finally(() => {
          if (seq === checkSeq.current) setChecking(false);
        });
    }, HANDLE_CHECK_DELAY_MS);
  }

  const changes = changedLabels(saved, draft);
  const nameBlank = draft.name.trim() === "";
  const lettersBad = draft.initials.trim().length < 1 || draft.initials.trim().length > 3;
  const blockedReason: string | null = nameBlank
    ? "Name can't be empty"
    : lettersBad
      ? "Letters must be 1–3 characters"
      : handleProblem
        ? handleProblem
        : checking
          ? "Checking the handle…"
          : handleChanged && freeHandle !== draft.handle
            ? "Leave the handle field to check it"
            : null;

  async function save() {
    setSaving(true);
    setSaveError("");
    try {
      const next = await api.updateMe(patchFrom(saved, draft));
      setUser({ ...user, ...next });
      setDraft(draftFrom({ ...user, ...next }));
      setServerProblem(null);
    } catch (err) {
      setSaveError(errorDetail(err, "could not save the profile"));
    } finally {
      setSaving(false);
    }
  }

  function discard() {
    clearTimeout(timer.current);
    checkSeq.current += 1;
    setChecking(false);
    setServerProblem(null);
    setSaveError("");
    setDraft(saved);
  }

  const preview = (
    <aside aria-label="Where your dot shows" className="rounded-[14px] border border-line-2 bg-surface-2 p-5">
      <h2 className="mb-3 font-mono text-[10px] uppercase tracking-wide text-faint">Where your dot shows</h2>
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Avatar initials={draft.initials} color={draft.avatar} color2={draft.avatar2 || null} pattern={draft.pattern} size={28} />
          <span className="text-[12px] text-muted">Top bar</span>
        </div>
        <div className="flex items-center gap-2">
          <Avatar initials={draft.initials} color={draft.avatar} color2={draft.avatar2 || null} pattern={draft.pattern} size={20} />
          <span className="text-[12px] text-fg-2">{draft.name || "Your name"}</span>
          <span className="font-mono text-small text-muted">@{draft.handle}</span>
        </div>
        <div className="flex items-center gap-2">
          <Avatar initials={draft.initials} color={draft.avatar} color2={draft.avatar2 || null} pattern={draft.pattern} size={44} />
          <span className="text-[12px] text-muted">Members list</span>
        </div>
      </div>
    </aside>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Profile</h1>
        <p className="mt-0.5 text-body text-muted">Your name, your dot, and your account.</p>
      </div>

      <div className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto p-4 sm:p-6">
        <div className="mb-6 flex flex-wrap items-center gap-4 rounded-[14px] border border-line-2 bg-surface-2 p-5">
          <Avatar initials={saved.initials} color={saved.avatar} color2={saved.avatar2 || null} pattern={saved.pattern} size={64} />
          <div className="min-w-0">
            <div className="text-[17px] font-semibold">{user.name}</div>
            <div className="font-mono text-[12px] text-muted">@{user.handle}</div>
            <div className="mt-0.5 break-all text-body text-muted">{user.email}</div>
          </div>
          <span className="ml-auto rounded-md border border-line-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-muted">
            {hosted ? "Cloud" : "Local"}
          </span>
        </div>

        <div className="grid grid-cols-1 gap-5 min-[900px]:grid-cols-[minmax(0,1fr)_260px]">
          <div className="min-w-0 space-y-5">
            <Card title="Name and handle">
              <div className="space-y-3">
                <label className="block">
                  <span className="mb-1 block text-[12px] text-muted">Name</span>
                  <Input value={draft.name} onChange={(e) => set({ name: e.target.value })} aria-invalid={nameBlank} />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[12px] text-muted">Handle</span>
                  <Input
                    value={draft.handle}
                    onChange={(e) => onHandleChange(e.target.value)}
                    onBlur={onHandleBlur}
                    aria-invalid={handleProblem !== null}
                    aria-describedby="handle-note"
                    className="font-mono"
                  />
                </label>
                <p id="handle-note" role="status" className={cn("text-[12px]", handleProblem ? "text-st-blocked" : "text-muted")}>
                  {handleProblem ??
                    "Items show the new handle right away. Nobody else in your orgs can take the old one for 30 days."}
                </p>
              </div>
            </Card>

            <Card title="Profile dot">
              <div className="space-y-4">
                <label className="block">
                  <span className="mb-1 block text-[12px] text-muted">Letters</span>
                  <Input
                    value={draft.initials}
                    maxLength={3}
                    onChange={(e) => set({ initials: e.target.value })}
                    aria-invalid={lettersBad}
                    className="w-24 font-mono"
                  />
                </label>
                <fieldset>
                  <legend className="mb-1 text-[12px] text-muted">Fill</legend>
                  <div className="flex gap-2">
                    {(["Solid", "Ombre"] as const).map((m) => {
                      const ombre = m === "Ombre";
                      const on = ombre ? draft.avatar2 !== "" : draft.avatar2 === "";
                      return (
                        <Button
                          key={m}
                          type="button"
                          size="sm"
                          variant={on ? "default" : "outline"}
                          aria-pressed={on}
                          onClick={() =>
                            set({
                              avatar2: ombre
                                ? (DOT_COLOURS.find((c) => c !== draft.avatar) ?? draft.avatar)
                                : "",
                            })
                          }
                        >
                          {m}
                        </Button>
                      );
                    })}
                  </div>
                </fieldset>
                <Swatches label="Colour" value={draft.avatar} onPick={(c) => set({ avatar: c })} />
                {draft.avatar2 !== "" && (
                  <Swatches label="Second colour" value={draft.avatar2} onPick={(c) => set({ avatar2: c })} />
                )}
                <fieldset>
                  <legend className="mb-1 text-[12px] text-muted">Pattern</legend>
                  <div className="flex flex-wrap gap-2">
                    {PROJECT_PATTERNS.map((p) => (
                      <button
                        key={p.id}
                        type="button"
                        title={p.name}
                        aria-label={`Pattern ${p.name}`}
                        aria-pressed={draft.pattern === p.id}
                        onClick={() => set({ pattern: p.id })}
                        className={cn(
                          "rounded-full border-2 p-0.5",
                          draft.pattern === p.id ? "border-fg" : "border-transparent",
                        )}
                      >
                        <Avatar initials="" color={draft.avatar} color2={draft.avatar2 || null} pattern={p.id} size={28} />
                      </button>
                    ))}
                  </div>
                </fieldset>
              </div>
            </Card>

            <Card title="Account">
              {hosted ? (
                <>
                  <AccountRow label="Signed in with" value={`Email · ${user.email}`} />
                  <AccountRow label="Password" value="Set — change it below" />
                  <AccountRow label="Your API keys" value={keysText(user)} />
                  <AccountRow label="Member since" value={<span className="text-muted">Not reported yet</span>} />
                </>
              ) : (
                <>
                  <AccountRow label="Box" value={<span className="font-mono">{window.location.host}</span>} />
                  <AccountRow label="Password" value="Set — change it below" />
                  <AccountRow label="Your API keys" value={keysText(user)} />
                  <AccountRow label="Projects on this box" value={String(memberships.length)} />
                </>
              )}
              <div className="mt-3 border-t border-line-2 pt-4">
                <PasswordCard />
              </div>
            </Card>

            <OrganizationsSection user={user} />
          </div>

          <div className="min-w-0 min-[900px]:sticky min-[900px]:top-0 min-[900px]:self-start">{preview}</div>
        </div>

        {changes.length > 0 && <div className="h-24" />}
      </div>

      {changes.length > 0 && (
        <div role="region" aria-label="Unsaved changes" className="flex flex-none flex-wrap items-center gap-3 border-t border-line bg-header px-5 py-3">
          <div className="min-w-0 flex-1 text-body">
            <span className="text-fg-2">Changed: {changes.join(", ")}</span>
            {blockedReason && <div className="text-[12px] text-st-blocked">{blockedReason}</div>}
            {saveError && <div className="text-[12px] text-st-blocked">{saveError}</div>}
          </div>
          <Button type="button" variant="outline" size="sm" onClick={discard} disabled={saving}>
            Discard
          </Button>
          <Button type="button" size="sm" onClick={save} disabled={saving || blockedReason !== null}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      )}
    </div>
  );
}

function keysText(user: User): string {
  const n = user.api_keys?.count;
  if (n === undefined) return "Not reported yet";
  return n === 0 ? "None" : `${n} · ${user.api_keys?.projects.join(", ")}`;
}

function Swatches({ label, value, onPick }: { label: string; value: string; onPick: (c: string) => void }) {
  return (
    <fieldset>
      <legend className="mb-1 text-[12px] text-muted">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {DOT_COLOURS.map((c) => (
          <button
            key={c}
            type="button"
            aria-label={`${label} ${c}`}
            aria-pressed={value === c}
            onClick={() => onPick(c)}
            style={{ background: c }}
            className={cn("h-7 w-7 rounded-full border-2", value === c ? "border-fg" : "border-transparent")}
          />
        ))}
      </div>
    </fieldset>
  );
}
