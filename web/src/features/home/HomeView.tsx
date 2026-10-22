import { ProjectHome } from "@/features/projecthome/ProjectHome";

/**
 * Self-host `/home`. The same view as hosted project home (GRPH-1067).
 * Flat links and no galaxy come from the route, not from a second layout.
 * The "Needs attention" focal number is gone on purpose.
 */
export function HomeView() {
  return <ProjectHome />;
}
