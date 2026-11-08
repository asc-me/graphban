import { SetupStatusCard } from "@/features/home/SetupStatusCard";
import { useProjectCtx } from "@/features/ProjectContext";
import { ProjectHome } from "@/features/projecthome/ProjectHome";

/**
 * Self-host `/home`. The same view as hosted project home (GRPH-1067).
 * Flat links and no galaxy come from the route, not from a second layout.
 * The "Needs attention" focal number is gone on purpose.
 * The Setup status card (GRPH-1442) sits above it until the agent connection is proven or dismissed.
 */
export function HomeView() {
  const { active } = useProjectCtx();
  return (
    <>
      {active?.id ? (
        <div className="px-5 pt-4">
          <SetupStatusCard projectId={active.id} />
        </div>
      ) : null}
      <ProjectHome />
    </>
  );
}
