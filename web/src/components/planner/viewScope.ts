import * as React from "react";

/** One saved or preset view: a status filter plus the time window and auto-hide. */
export interface ViewDef {
  id: string;
  label: string;
  status: string;
  /** Days of activity to keep; 0 = all. */
  window: number;
  /** Hide done/closed rows this many days after completion; 0 = never. */
  hide: number;
  title?: string;
}

export type ViewPage = "tracker" | "prds";

/** The live (possibly edited) state of the bar: the chosen view's id plus its current knobs. */
export interface ViewState {
  view: string;
  status: string;
  window: number;
  hide: number;
}

export const PRESETS: Record<ViewPage, ViewDef[]> = {
  tracker: [
    { id: "current", label: "Current", status: "all", window: 90, hide: 14, title: "Last 90 days · done hidden after 14 days" },
    { id: "active", label: "In flight", status: "active", window: 0, hide: 0, title: "In progress, review and blocked" },
    { id: "backlog", label: "Backlog", status: "backlog", window: 0, hide: 0, title: "Everything not yet scheduled" },
    { id: "all", label: "Everything", status: "all", window: 0, hide: 0, title: "No window, nothing hidden" },
  ],
  prds: [
    { id: "current", label: "Current", status: "all", window: 90, hide: 14, title: "Last 90 days · closed hidden after 14 days" },
    { id: "review", label: "In review", status: "review", window: 0, hide: 0, title: "Waiting on review" },
    { id: "draft", label: "Drafts", status: "draft", window: 0, hide: 0, title: "Every draft, including stale ones" },
    { id: "all", label: "Everything", status: "all", window: 0, hide: 0, title: "No window, nothing hidden" },
  ],
};

export const WINDOW_OPTS: [number, string][] = [[30, "30d"], [90, "90d"], [180, "180d"], [0, "All"]];
export const HIDE_OPTS: [number, string][] = [[0, "Never"], [3, "3d"], [7, "7d"], [14, "14d"], [30, "30d"]];

const DAY_MS = 86_400_000;

function ageDays(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : (now - t) / DAY_MS;
}

export interface Scoped<T> {
  shown: T[];
  /** Outside the time window. */
  oldHidden: number;
  /** Inside the window but finished longer ago than the auto-hide. */
  doneHidden: number;
}

/**
 * Apply a view's window and auto-hide. `activity` is when the row last moved, `finishedAt`
 * is when it reached done/closed (null for rows still open). A row with no usable date is
 * kept: hiding what we cannot date would silently lose it.
 */
export function scopeRows<T>(
  rows: T[],
  v: Pick<ViewState, "window" | "hide">,
  pick: { activity: (r: T) => string | null | undefined; finishedAt: (r: T) => string | null | undefined },
  now: number = Date.now(),
): Scoped<T> {
  const inWindow = rows.filter((r) => {
    if (!v.window) return true;
    const a = ageDays(pick.activity(r), now);
    return a === null || a <= v.window;
  });
  const shown = inWindow.filter((r) => {
    if (!v.hide) return true;
    const a = ageDays(pick.finishedAt(r), now);
    return a === null || a <= v.hide;
  });
  return { shown, oldHidden: rows.length - inWindow.length, doneHidden: inWindow.length - shown.length };
}

export function statusMatches(status: string, filter: string, active: readonly string[]): boolean {
  if (filter === "all") return true;
  if (filter === "active") return active.includes(status);
  return status === filter;
}

interface Stored {
  saved: ViewDef[];
  defaultId: string;
}

const storageKey = (page: ViewPage) => `gb_views_${page}`;

function read(page: ViewPage): Stored {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey(page)) ?? "{}") as Partial<Stored>;
    const saved = Array.isArray(raw.saved)
      ? raw.saved.filter(
          (v): v is ViewDef =>
            !!v && typeof v.id === "string" && typeof v.label === "string" && typeof v.status === "string" &&
            typeof v.window === "number" && typeof v.hide === "number",
        )
      : [];
    return { saved, defaultId: typeof raw.defaultId === "string" ? raw.defaultId : "current" };
  } catch {
    return { saved: [], defaultId: "current" };
  }
}

/**
 * Saved views, the default view and the live edit state for one page. Persists per user in
 * localStorage until the GRPH-1133 prefs store carries it.
 */
export function useViews(page: ViewPage) {
  const [stored, setStored] = React.useState<Stored>(() => read(page));
  const views = React.useMemo(() => [...PRESETS[page], ...stored.saved], [page, stored.saved]);
  const defaultView = views.find((v) => v.id === stored.defaultId) ?? views[0];
  const [state, setState] = React.useState<ViewState>(() => {
    const d = [...PRESETS[page], ...read(page).saved].find((v) => v.id === read(page).defaultId) ?? PRESETS[page][0];
    return { view: d.id, status: d.status, window: d.window, hide: d.hide };
  });

  const persist = (next: Stored) => {
    setStored(next);
    try {
      localStorage.setItem(storageKey(page), JSON.stringify(next));
    } catch {
      /* storage unavailable — views last this session */
    }
  };

  const current = views.find((v) => v.id === state.view) ?? views[0];
  const edited = current.status !== state.status || current.window !== state.window || current.hide !== state.hide;

  return {
    views,
    state,
    current,
    edited,
    defaultId: defaultView.id,
    canSetDefault: !edited && state.view !== defaultView.id,
    patch: (p: Partial<ViewState>) => setState((s) => ({ ...s, ...p })),
    select: (v: ViewDef) => setState({ view: v.id, status: v.status, window: v.window, hide: v.hide }),
    reset: () => setState((s) => ({ ...s, status: current.status, window: current.window, hide: current.hide })),
    setDefault: () => persist({ ...stored, defaultId: state.view }),
    save: () => {
      const n = stored.saved.length + 1;
      const id = `saved-${Date.now().toString(36)}-${n}`;
      const nv: ViewDef = { id, label: `My view ${n}`, status: state.status, window: state.window, hide: state.hide };
      persist({ ...stored, saved: [...stored.saved, nv] });
      setState({ ...state, view: id });
    },
  };
}

export type ViewsApi = ReturnType<typeof useViews>;

/** `90d · hide done >14d` — the scope button's summary. */
export function scopeSummary(v: Pick<ViewState, "window" | "hide">, doneWord: string): string {
  const w = v.window ? `${v.window}d` : "All time";
  return v.hide ? `${w} · hide ${doneWord} >${v.hide}d` : w;
}

/** The footer line's hidden-by-view bits, or "" when nothing is hidden. */
export function hiddenSummary(
  s: Pick<Scoped<unknown>, "oldHidden" | "doneHidden">,
  v: Pick<ViewState, "window" | "hide">,
  doneWord: string,
): string {
  const bits: string[] = [];
  if (s.oldHidden) bits.push(`${s.oldHidden} older than ${v.window}d`);
  if (s.doneHidden) bits.push(`${s.doneHidden} ${doneWord} more than ${v.hide}d ago`);
  return bits.join(" · ");
}
