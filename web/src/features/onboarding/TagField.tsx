import * as React from "react";

import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";

/**
 * The project tag field, shared by both creation surfaces (PRD-13 / AL-260).
 *
 * A tag is the short prefix a project's item, request, and PRD keys render with —
 * `GRPH-12`, `GRPH-R33`, `GRPH-P4`. It is set at creation and changeable afterwards.
 *
 * Neither derivation nor the availability rule is reimplemented here. Both are server
 * calls, because a second implementation in TypeScript would drift from the one that
 * actually assigns and enforces the tag — and the availability rule in particular is
 * not guessable from the client: it excludes tags this deployment previously held and
 * prefixes reserved by ids issued before tags existed.
 */
export type TagStatus =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "ok" }
  | { kind: "unavailable"; reason: string }
  | { kind: "error" };

const TAG_FORMAT = /^[A-Z][A-Z0-9]{1,3}$/;
const TAG_HINT = "2–4 letters or digits, starting with a letter";

export function useTagField(name: string) {
  const [tag, setTagRaw] = React.useState("");
  const [touched, setTouched] = React.useState(false);
  const [status, setStatus] = React.useState<TagStatus>({ kind: "idle" });

  function setTag(next: string) {
    setTouched(true);
    setTagRaw(next.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 4));
  }

  function reset() {
    setTagRaw("");
    setTouched(false);
    setStatus({ kind: "idle" });
  }

  // Suggest from the name until the user takes over the field. Once they've typed a
  // tag, the name no longer overwrites it — otherwise editing the name would silently
  // discard a deliberate choice.
  React.useEffect(() => {
    if (touched || !name.trim()) return;
    let cancelled = false;
    const t = setTimeout(() => {
      api
        .tagSuggestion(name.trim())
        .then((r) => {
          if (!cancelled) setTagRaw(r.tag);
        })
        .catch(() => {
          /* a failed suggestion just leaves the field empty; the server derives on submit */
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [name, touched]);

  React.useEffect(() => {
    if (!tag) {
      setStatus({ kind: "idle" });
      return;
    }
    let cancelled = false;
    setStatus({ kind: "checking" });
    const t = setTimeout(() => {
      api
        .tagCheck(tag)
        .then((r) => {
          if (cancelled) return;
          setStatus(r.available ? { kind: "ok" } : { kind: "unavailable", reason: r.reason });
        })
        .catch(() => {
          if (!cancelled) setStatus({ kind: "error" });
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [tag]);

  return { tag, setTag, status, reset, blocked: status.kind === "unavailable" };
}

export function TagField({
  tag,
  setTag,
  status,
  compact = false,
}: {
  tag: string;
  setTag: (v: string) => void;
  status: TagStatus;
  /** The New project wizard's look: a 120px mono input and a "Checking…" line while the
   *  availability call is in flight (GRPH-1587). */
  compact?: boolean;
}) {
  return (
    <div>
      <label
        htmlFor="project-tag"
        className="mb-1.5 block font-mono text-[10px] uppercase tracking-wide text-faint"
      >
        Tag
      </label>
      <Input
        id="project-tag"
        value={tag}
        onChange={(e) => setTag(e.target.value)}
        placeholder="e.g. GRPH"
        maxLength={4}
        aria-invalid={status.kind === "unavailable" || status.kind === "error" || (!!tag && !TAG_FORMAT.test(tag))}
        className={compact ? "w-[120px] font-mono uppercase tracking-wide" : "font-mono uppercase"}
      />
      <p className="mt-1.5 font-mono text-[10px] text-faint" role="status">
        {compact && status.kind === "checking" && TAG_FORMAT.test(tag) ? (
          <span className="inline-flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="pw-anim h-2.5 w-2.5 rounded-full border-[1.5px] border-line border-t-muted"
              style={{ animation: "pwSpin .8s linear infinite" }}
            />
            Checking…
          </span>
        ) : tag && !TAG_FORMAT.test(tag) ? (
          <span className="text-danger">{TAG_HINT}</span>
        ) : status.kind === "unavailable" ? (
          <span className="text-danger">{status.reason}</span>
        ) : status.kind === "error" ? (
          <span className="text-danger">Couldn’t check whether this tag is available. Edit it to retry.</span>
        ) : tag ? (
          <>
            Keys look like <span className="text-fg-2">{tag}-12</span>,{" "}
            <span className="text-fg-2">{tag}-R33</span>, <span className="text-fg-2">{tag}-P4</span>
          </>
        ) : (
          TAG_HINT
        )}
      </p>
    </div>
  );
}
