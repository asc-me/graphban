import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import { consoleHref } from "@/lib/routes";

/**
 * Leave this origin for the same path on the cloud console.
 *
 * The org plane and the operator console render in graphban-control. A bookmark
 * that still hits this app has to follow them, or it reads as a missing page.
 */
export function ConsoleRedirect() {
  const { pathname, search, hash } = useLocation();
  useEffect(() => {
    window.location.replace(consoleHref(pathname) + search + hash);
  }, [pathname, search, hash]);
  return null;
}
