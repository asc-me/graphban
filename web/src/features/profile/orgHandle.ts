/** The server's `handle_taken` refusal (409) on accepting an invite or changing an org-only handle. */
export interface HandleClash {
  orgId: string;
  orgName: string;
  suggested: string;
}

/** Read a `handle_taken` clash out of an ApiError, or null when the error is something else. */
export function handleClash(err: unknown): HandleClash | null {
  if (!err || typeof err !== "object" || !("message" in err) || typeof err.message !== "string") return null;
  try {
    const d = JSON.parse(err.message)?.detail;
    if (d && typeof d === "object" && d.code === "handle_taken" && typeof d.suggested === "string") {
      return { orgId: String(d.org_id ?? ""), orgName: String(d.org_name ?? ""), suggested: d.suggested };
    }
  } catch {
    /* not JSON */
  }
  return null;
}
