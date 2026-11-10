import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import { consoleHref } from "@/lib/routes";

/**
 * Leave this origin for the matching path on the cloud console.
 *
 * The org plane and the operator console render in graphban-control. A bookmark
 * that still hits this app has to follow them, or it reads as a missing page.
 */
export function ConsoleRedirect() {
  const { pathname, search, hash } = useLocation();
  const href = consoleHref(pathname);
  useEffect(() => {
    if (!href) return;
    window.location.replace(href + search + hash);
  }, [href, search, hash]);
  if (!href) {
    return (
      <p className="p-6 text-body text-muted">
        This page lives on the console, and this build has no console URL.
      </p>
    );
  }
  return null;
}
