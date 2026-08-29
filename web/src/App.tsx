import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AppFrame } from "@/components/shell/AppFrame";
import { useAuth } from "@/features/auth/AuthContext";
import { LoginPage } from "@/features/auth/LoginPage";
import { ResetPasswordPage } from "@/features/auth/ResetPasswordPage";
import { ActivityView } from "@/features/activity/ActivityView";
import { AdminView } from "@/features/admin/AdminView";
import { OperatorHome } from "@/features/admin/OperatorHome";
import { OperatorLicensing } from "@/features/admin/OperatorLicensing";
import { OperatorOrgs } from "@/features/admin/OperatorOrgs";
import { OperatorUsers } from "@/features/admin/OperatorUsers";
import { CodeGraphView } from "@/features/code/CodeGraphView";
import { DashboardView } from "@/features/dashboard/DashboardView";
import { EmbedFeedbackPage } from "@/features/feedback/EmbedFeedbackPage";
import { FeedbackKitView } from "@/features/feedback/FeedbackKitView";
import { LinksGraphView } from "@/features/links/LinksGraphView";
import { FleetView } from "@/features/fleet/FleetView";
import { GalaxyView } from "@/features/galaxy/GalaxyView";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { InviteAcceptPage } from "@/features/onboarding/InviteAcceptPage";
import { OrgAdminShell } from "@/features/orgadmin/OrgAdminShell";
import { OrgOverviewView } from "@/features/orgadmin/OrgOverviewView";
import { OrgBranding } from "@/features/orgadmin/OrgBranding";
import { SPECULATIVE_ENABLED } from "@/features/orgadmin/Speculative";
import { OrgBilling } from "@/features/orgadmin/OrgBilling";
import { OrgIntegrations } from "@/features/orgadmin/OrgIntegrations";
import { OrgDeployments } from "@/features/orgadmin/OrgDeployments";
import { OrgTeams } from "@/features/orgadmin/OrgTeams";
import { OrgUsers } from "@/features/orgadmin/OrgUsers";

import { PrdEditorView } from "@/features/prds/PrdEditorView";
import { PrdListView } from "@/features/prds/PrdListView";
import { ProfileView } from "@/features/profile/ProfileView";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { useProjectCtx } from "@/features/ProjectContext";
import { EmbedRoadmapPage } from "@/features/roadmap/EmbedRoadmapPage";
import { RoadmapView } from "@/features/roadmap/RoadmapView";
import { HomeView } from "@/features/home/HomeView";
import { SettingsView } from "@/features/settings/SettingsView";
import { RequestsView } from "@/features/requests/RequestsView";
import { TrackerView } from "@/features/tracker/TrackerView";
import { TriageView } from "@/features/triage/TriageView";
import { useConfig } from "@/lib/queries";
import { ORG_BASE, lastProjectTag, projectPath } from "@/lib/routes";

export function App() {
  return (
    <Routes>
      {/* Public, unauthenticated embed targets. */}
      <Route path="/embed/feedback" element={<EmbedFeedbackPage />} />
      <Route path="/embed/roadmap" element={<EmbedRoadmapPage />} />
      {/* Emailed org-invite landing — works signed in or out (AL-74b). */}
      <Route path="/invite/:token" element={<InviteAcceptPage />} />
      {/* Emailed password-reset landing (GRPH-570). Public by necessity: the premise is that
          the visitor cannot sign in, so it cannot live behind AuthedApp. */}
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route path="*" element={<AuthedApp />} />
    </Routes>
  );
}

/**
 * The project-scoped views, mounted twice (PRD-21 D1.2).
 *
 * On a hosted deployment they live under `/p/:tag/…`, and the flat paths become
 * resolvers that redirect into them. On self-host they stay at the root exactly as
 * today and `/org/*` is never registered at all — a self-host build has no org to
 * serve, so it must not carry routes that imply one.
 */
const PROJECT_VIEWS: [string, React.ReactNode][] = [
  ["tracker", <TrackerView />],
  ["requests", <RequestsView />],
  ["triage", <TriageView />],
  ["dashboard", <DashboardView />],
  ["links", <LinksGraphView />],
  ["code", <CodeGraphView />],
  ["roadmap", <RoadmapView />],
  ["mcp-tools", <McpToolsView />],
  ["fleet", <FleetView />],
  ["activity", <ActivityView />],
  ["memory-review", <MemoryReviewView />],
  ["prds", <PrdListView />],
  ["prds/:id", <PrdEditorView />],
  ["feedback-kit", <FeedbackKitView />],
];

function AuthedApp() {
  const { user, loading } = useAuth();
  const { data: config, isLoading: configLoading } = useConfig();

  if (loading || configLoading) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-[12px] text-faint">
        loading…
      </div>
    );
  }
  if (!user) return <LoginPage />;
  const hosted = config?.hosted_mode ?? false;

  return (
    <Routes>
      {/* Operator plane — its own shell, outside AppFrame, cross-tenant. */}
      <Route path="/admin" element={<AdminView />}>
        <Route index element={<OperatorHome />} />
        <Route path="orgs" element={<OperatorOrgs />} />
        <Route path="users" element={<OperatorUsers />} />
        <Route path="licensing" element={<OperatorLicensing />} />
      </Route>

      <Route element={<AppFrame />}>
        {/* ---- the project plane ---- */}
        {hosted
          ? PROJECT_VIEWS.map(([path, el]) => (
              <Route key={path} path={`/p/:tag/${path}`} element={el} />
            ))
          : PROJECT_VIEWS.filter(([path]) => !["dashboard", "mcp-tools", "feedback-kit"].includes(path)).map(
              ([path, el]) => <Route key={path} path={`/${path}`} element={el} />,
            )}
        {hosted && <Route path="/p/:tag" element={<ProjectHome />} />}
        {!hosted && (
          <>
            <Route path="/home" element={<HomeView />} />
            <Route path="/dashboard" element={<Navigate to="/home" replace />} />
            <Route path="/mcp-tools" element={<Navigate to="/settings/project/mcp" replace />} />
            <Route path="/feedback-kit" element={<Navigate to="/settings/project/feedback-kit" replace />} />
          </>
        )}

        {/* ---- the org plane (hosted only) ---- */}
        {hosted && (
          <>
            <Route path={ORG_BASE} element={<OrgOverviewView />} />
            <Route path={`${ORG_BASE}/galaxy`} element={<GalaxyView />} />
            <Route path={`${ORG_BASE}/admin`} element={<OrgAdminShell />}>
              <Route index element={<Navigate to="users" replace />} />
              <Route path="users" element={<OrgUsers />} />
              <Route path="teams" element={<OrgTeams />} />
              <Route path="deployments" element={<OrgDeployments />} />
              {/* Gated on the ROUTE as well as the nav item. A speculative screen still
                  reachable by URL gets linked to, screenshotted and demoed — and a nav
                  gate on its own reads as an oversight rather than a decision, so the
                  next person cannot tell which it was (PRD-21 D9). */}
              {SPECULATIVE_ENABLED && (
                <Route path="branding" element={<OrgBranding />} />
              )}
              <Route path="integrations" element={<OrgIntegrations />} />
              <Route path="billing" element={<OrgBilling />} />
            </Route>
            {/* Legacy flat paths resolve to the tag route rather than 404. */}
            {PROJECT_VIEWS.map(([path]) => (
              <Route key={`flat-${path}`} path={`/${path}`} element={<FlatRedirect />} />
            ))}
            <Route path="/organization" element={<Navigate to={ORG_BASE} replace />} />
          </>
        )}

        {!hosted && (
          <Route path="/organization" element={<Navigate to="/settings/deployment/sync" replace />} />
        )}
        <Route path="/settings/*" element={<SettingsView />} />
        <Route path="/profile" element={<ProfileView />} />
        <Route index element={<HomeRedirect hosted={hosted} />} />
        <Route path="*" element={<HomeRedirect hosted={hosted} />} />
      </Route>
    </Routes>
  );
}

/**
 * A flat path on a hosted deployment: resolve a project and move to its tag URL.
 *
 * `replace` so the back button does not ping-pong between the flat path and the tag one.
 * Resolution order is last-used then first-readable; when neither exists there is no
 * project to show, so the org plane is the honest destination rather than a project
 * page for a project that does not exist.
 */
function FlatRedirect() {
  const { pathname } = useLocation();
  const { projects, loading } = useProjectCtx();
  if (loading) return null;

  const view = pathname.replace(/^\/+/, "");
  const remembered = lastProjectTag();
  const target =
    projects.find((p) => p.tag === remembered) ?? projects[0] ?? null;
  if (!target?.tag) return <Navigate to={ORG_BASE} replace />;
  return <Navigate to={projectPath(target.tag, view)} replace />;
}

function HomeRedirect({ hosted }: { hosted: boolean }) {
  const { projects, loading } = useProjectCtx();
  if (loading) return null;
  if (!hosted) return <Navigate to="/home" replace />;

  const remembered = lastProjectTag();
  const target = projects.find((p) => p.tag === remembered) ?? projects[0] ?? null;
  if (!target?.tag) return <Navigate to={ORG_BASE} replace />;
  return <Navigate to={projectPath(target.tag, "tracker")} replace />;
}
