/**
 * Pull the human message out of an ApiError. `request()` throws with the raw response body
 * as the message, which for FastAPI is a JSON `{detail}` envelope — showing it unparsed puts
 * literal braces in front of the user. Promote/outcome 422s send an object (`reason`,
 * `blocked_by`); a string-only reader would fall through to the raw JSON and look like
 * the write silently no-op'd.
 */
export function errorDetail(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "message" in err && typeof err.message === "string") {
    try {
      const parsed = JSON.parse(err.message);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && typeof parsed.detail === "object" && parsed.detail !== null) {
        const d = parsed.detail as Record<string, unknown>;
        if (typeof d.reason === "string" && d.reason) return d.reason;
        if (typeof d.blocked_by === "string") {
          const bits = [d.blocked_by, d.trend, d.caught_state].filter(
            (x): x is string => typeof x === "string" && x.length > 0,
          );
          if (bits.length) return bits.join(" · ");
        }
      }
    } catch {
      /* not JSON — fall through */
    }
    if (err.message) return err.message;
  }
  return fallback;
}
