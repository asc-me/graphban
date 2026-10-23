/**
 * Parse a server timestamp, treating a zone-less one as UTC.
 *
 * Postgres returns `…+00:00`, but SQLite hands the same column back naive — and JS
 * reads a zone-less ISO string as *local* time. West of UTC that puts every timestamp
 * in the future, so `Date.now() - then` goes negative and a row hours old reads "0s ago".
 */
function parseTs(iso: string): number {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(zoned ? iso : `${iso}Z`).getTime();
}

/**
 * Relative time, or null when there is no timestamp.
 *
 * Null rather than a dash: the caller decides what absence means in its own column.
 */
export function relTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const s = Math.max(0, Math.floor((Date.now() - parseTs(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
