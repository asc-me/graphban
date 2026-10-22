import { ProjectHome } from "@/features/projecthome/ProjectHome";

/**
 * Dashboard's content lives on project home (GRPH-1067).
 * The route and the nav entry are GRPH-1066; until that lands, this page is the home.
 */
export function DashboardView() {
  return <ProjectHome />;
}
