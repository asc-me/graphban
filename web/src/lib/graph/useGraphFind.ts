import * as React from "react";

/** Case-insensitive substring match over whatever text a view considers a node's name. */
export function matchIds(ids: string[], query: string, textOf: (id: string) => string): Set<string> {
  const q = query.trim().toLowerCase();
  if (!q) return new Set();
  return new Set(ids.filter((id) => textOf(id).toLowerCase().includes(q)));
}

export interface GraphFind {
  query: string;
  setQuery: (q: string) => void;
  /** Empty when the query is blank — callers should treat "no query" and "no hits" differently. */
  matches: Set<string>;
  /** True when a query is present, whether or not it matched anything. */
  active: boolean;
  inputRef: React.RefObject<HTMLInputElement | null>;
  clear: () => void;
}

/**
 * The `/`-focused find box (PRD-20 D2).
 *
 * `active` deliberately tracks the QUERY, not the hit count: a search that found nothing must
 * dim the graph and say so, rather than falling back to looking exactly like no search at all.
 * That is the same absence-is-not-a-clean-result rule the presence design turns on.
 */
export function useGraphFind(ids: string[], textOf: (id: string) => string): GraphFind {
  const [query, setQuery] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const textRef = React.useRef(textOf);
  textRef.current = textOf;

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      const tag = el?.tagName;
      // Never steal the key from something the user is typing into.
      if (tag === "INPUT" || tag === "TEXTAREA" || el?.isContentEditable) return;
      e.preventDefault();
      inputRef.current?.focus();
      inputRef.current?.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const matches = React.useMemo(
    () => matchIds(ids, query, (id) => textRef.current(id)),
    [ids, query],
  );

  const clear = React.useCallback(() => setQuery(""), []);

  return { query, setQuery, matches, active: query.trim().length > 0, inputRef, clear };
}
