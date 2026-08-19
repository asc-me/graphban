import * as React from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useProjects } from "@/lib/queries";
import {
  lastProjectTag,
  projectPath,
  rememberProjectTag,
  tagFromPath,
  viewFromPath,
} from "@/lib/routes";
import type { Project } from "@/lib/types";

interface ProjectState {
  projects: Project[];
  active: Project | null;
  activeId: string;
  /** Switch project, keeping the current view. Navigation — not a stored variable. */
  setActiveId: (id: string) => void;
  loading: boolean;
  /** The URL named a project that does not resolve for this caller. */
  notFound: boolean;
}

const Ctx = React.createContext<ProjectState | null>(null);

/**
 * The active project, derived from the URL (PRD-21 D1.1).
 *
 * The old provider synced a module-level `activeProjectId` in a `useEffect`. That is
 * safe only while a switcher is the sole thing that moves the project — a render
 * separates the change from the user's next action. A project in the URL breaks it: the
 * route changes synchronously on a deep link and on back/forward while the effect fires
 * a render later, so anything issued in that gap targets the previous project.
 *
 * So: the route is read directly, nothing caches it, and no effect mediates it. The API
 * client has no ambient project left to go stale — every write takes one explicitly.
 *
 * **A tag is a lookup, never a grant.** It resolves against the projects this caller can
 * already read, so a tag belonging to another org simply is not in the list and renders
 * as not-found — indistinguishable from a tag that never existed. That is why tags being
 * short and guessable is not a weakness.
 */
export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const { data: projects = [], isLoading } = useProjects();
  const { pathname } = useLocation();
  const navigate = useNavigate();

  const routeTag = tagFromPath(pathname);
  const byTag = routeTag
    ? projects.find((p) => p.tag?.toLowerCase() === routeTag.toLowerCase()) ?? null
    : null;

  // No tag in the path (a flat route, or the org plane): fall back to last-used, then to
  // the first readable project. Only ever a hint for *which page to show*.
  const fallback =
    projects.find((p) => p.tag === lastProjectTag()) ?? projects[0] ?? null;
  const active = routeTag ? byTag : fallback;
  const notFound = !!routeTag && !byTag && !isLoading;

  // Remembering is a side effect on navigation, not a source of truth — nothing reads it
  // back within a render, so it cannot desynchronise from the route.
  React.useEffect(() => {
    if (active?.tag && routeTag) rememberProjectTag(active.tag);
  }, [active?.tag, routeTag]);

  const setActiveId = React.useCallback(
    (id: string) => {
      const next = projects.find((p) => p.id === id);
      if (!next?.tag) return;
      navigate(projectPath(next.tag, viewFromPath(pathname)));
    },
    [projects, navigate, pathname],
  );

  const value = React.useMemo(
    () => ({
      projects,
      active,
      activeId: active?.id ?? "",
      setActiveId,
      loading: isLoading,
      notFound,
    }),
    [projects, active, setActiveId, isLoading, notFound],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useProjectCtx() {
  const ctx = React.useContext(Ctx);
  if (!ctx) throw new Error("useProjectCtx must be used within ProjectProvider");
  return ctx;
}
