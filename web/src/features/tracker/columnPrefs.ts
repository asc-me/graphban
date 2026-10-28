import * as React from "react";

import { BOARD_COLUMNS } from "@/lib/meta";
import type { Status } from "@/lib/types";

export type ColumnMode = "show" | "collapse" | "hide";

export type ColumnPrefs = Partial<Record<Status, ColumnMode>>;

/** Per-user board column settings; localStorage until the GRPH-1133 prefs store exists. */
export const TRACKER_COLUMNS_KEY = "gb_tracker_columns";

function read(): ColumnPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(TRACKER_COLUMNS_KEY) ?? "{}") as Record<string, unknown>;
    const out: ColumnPrefs = {};
    for (const s of BOARD_COLUMNS) {
      const m = raw[s];
      if (m === "collapse" || m === "hide") out[s] = m;
    }
    return out;
  } catch {
    return {};
  }
}

export function useColumnPrefs() {
  const [prefs, setPrefs] = React.useState<ColumnPrefs>(read);

  const persist = (next: ColumnPrefs) => {
    setPrefs(next);
    try {
      localStorage.setItem(TRACKER_COLUMNS_KEY, JSON.stringify(next));
    } catch {
      // Storage unavailable: the setting holds for this session only.
    }
  };

  const setMode = (status: Status, mode: ColumnMode) => {
    const next = { ...prefs };
    if (mode === "show") delete next[status];
    else next[status] = mode;
    persist(next);
  };

  const modeOf = (status: Status): ColumnMode => prefs[status] ?? "show";
  const hidden = BOARD_COLUMNS.filter((s) => modeOf(s) === "hide").length;
  const collapsed = BOARD_COLUMNS.filter((s) => modeOf(s) === "collapse").length;

  return { modeOf, setMode, reset: () => persist({}), hidden, collapsed };
}

export type ColumnPrefsApi = ReturnType<typeof useColumnPrefs>;

export function columnsSummary(hidden: number, collapsed: number): string {
  const parts = [hidden && `${hidden} hidden`, collapsed && `${collapsed} collapsed`].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Columns";
}
