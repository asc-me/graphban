import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { ConsoleRedirect } from "@/components/ConsoleRedirect";
import { AppFrame, ShellBootSkeleton } from "@/components/shell/AppFrame";
import { useAuth } from "@/features/auth/AuthContext";
import { LoginPage } from "@/features/auth/LoginPage";
import { ResetPasswordPage } from "@/features/auth/ResetPasswordPage";
import { ActivityView } from "@/features/activity/ActivityView";
import { CodeGraphView } from "@/features/code/CodeGraphView";
import { StackView } from "@/features/stack/StackView";
import { EmbedFeedbackPage } from "@/features/feedback/EmbedFeedbackPage";
import { FeedbackKitView } from "@/features/feedback/FeedbackKitView";
import { PublicBoardPage } from "@/features/public/PublicBoardPage";
import { PublicFeedbackPage } from "@/features/public/PublicFeedbackPage";
import { PublicRoadmapPage } from "@/features/public/PublicRoadmapPage";
import { PublicTrackingPage } from "@/features/public/PublicTrackingPage";
import { LinksGraphView } from "@/features/links/LinksGraphView";
import { FleetPage } from "@/features/fleet/FleetPage";
import { OutpostsView } from "@/features/fleet/OutpostsView";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { LessonsView } from "@/features/lessons/LessonsView";
import { HarnessView } from "@/features/harness/HarnessView";
import { LiveView } from "@/features/live/LiveView";
import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { InviteAcceptPage } from "@/features/onboarding/InviteAcceptPage";
import { PrdEditorView } from "@/features/prds/PrdEditorView";
import { PrdListView } from "@/features/prds/PrdListView";
import { ProfileView } from "@/features/profile/ProfileView";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { useProjectCtx } from "@/features/ProjectContext";
import { EmbedRoadmapPage } from "@/features/roadmap/EmbedRoadmapPage";
import { RoadmapView } from "@/features/roadmap/RoadmapView";
import { HomeView } from "@/features/home/HomeView";
import { SettingsView } from "@/features/settings/SettingsView";
import { UsageView } from "@/features/usage/UsageView";
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
      {/* PRD-43 D5: public board pages (unauthenticated, on the current origin). */}
      <Route path="/public/:token/feedback" element={<PublicFeedbackPage />} />
      <Route path="/public/:token/roadmap" element={<PublicRoadmapPage />} />
      <Route path="/public/:token/issues" element={<PublicBoardPage kind="issues" />} />
      <Route path="/public/:token/requests" element={<PublicBoardPage kind="requests" />} />
      {/* PRD-43 D3: submitter tracking page (unauthenticated). */}
      <Route path="/track/:trackToken" element={<PublicTrackingPage />} />
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
 * today. The org plane and the operator console are not mounted in either mode —
 * those screens render in graphban-control, and a hosted bookmark under the org
 * base leaves this origin. A self-host build still does not register that base.
 */
const PROJECT_VIEWS: [string, React.ReactNode][] = [
  ["tracker", <TrackerView />],
  ["requests", <RequestsView />],
  ["triage", <TriageView />],
  ["links", <LinksGraphView />],
  ["code", <CodeGraphView />],
  ["stack", <StackView />],
  ["roadmap", <RoadmapView />],
  ["mcp-tools", <McpToolsView />],
  ["fleet", <FleetPage />],
  ["outposts", <OutpostsView />],
  ["harness", <HarnessView />],
  ["activity", <ActivityView />],
  ["live", <LiveView />],
  ["memory", <MemoryReviewView />],
  ["lessons", <LessonsView />],
  ["lessons/:id", <LessonsView />],
  ["prds", <PrdListView />],
  ["prds/:id", <PrdEditorView />],
  ["feedback-kit", <FeedbackKitView />],
];

function AuthedApp() {
  const { user, loading } = useAuth();
  const { data: config, isLoading: configLoading } = useConfig();

  if (loading || configLoading) {
    return <ShellBootSkeleton />;
  }
  if (!user) return <LoginPage />;
  const hosted = config?.hosted_mode ?? false;

  return (
    <Routes>
      {/* Operator console lives in graphban-control. A bookmark follows it. */}
      <Route path="/admin" element={<ConsoleRedirect />} />
      <Route path="/admin/*" element={<ConsoleRedirect />} />

      <Route element={<AppFrame />}>
        {/* ---- the project plane ---- */}
        {hosted
          ? PROJECT_VIEWS.map(([path, el]) => (
              <Route key={path} path={`/p/:tag/${path}`} element={el} />
            ))
          : PROJECT_VIEWS.filter(([path]) => path !== "mcp-tools").map(
              ([path, el]) => <Route key={path} path={`/${path}`} element={el} />,
            )}
        {/* fleet.v1 / fleet.v2 are old bookmarks for the two Fleet tabs. */}
        {hosted
          ? <>
              <Route path="/p/:tag/fleet.v1" element={<FleetTabRedirect tab="manage" />} />
              <Route path="/p/:tag/fleet.v2" element={<FleetTabRedirect tab="harness" />} />
            </>
          : <>
              <Route path="/fleet.v1" element={<FleetTabRedirect tab="manage" />} />
              <Route path="/fleet.v2" element={<FleetTabRedirect tab="harness" />} />
            </>}
        {/* memory-review / memory-triage are old bookmarks for the one Memory page. */}
        {hosted
          ? <>
              <Route path="/p/:tag/memory-review" element={<MemoryRedirect />} />
              <Route path="/p/:tag/memory-triage" element={<MemoryRedirect />} />
            </>
          : <>
              <Route path="/memory-review" element={<MemoryRedirect />} />
              <Route path="/memory-triage" element={<MemoryRedirect />} />
            </>}
        {hosted && <Route path="/p/:tag" element={<ProjectHome />} />}
        {hosted && <Route path="/p/:tag/dashboard" element={<DashboardRedirect />} />}
        {!hosted && (
          <>
            <Route path="/home" element={<HomeView />} />
            <Route path="/dashboard" element={<Navigate to="/home" replace />} />
            <Route path="/mcp-tools" element={<Navigate to="/settings/project/mcp" replace />} />
          </>
        )}

        {/* ---- org-plane bookmarks (hosted only) leave for the console ---- */}
        {hosted && (
          <>
            <Route path={ORG_BASE} element={<ConsoleRedirect />} />
            <Route path={`${ORG_BASE}/*`} element={<ConsoleRedirect />} />
            {/* Legacy flat paths resolve to the tag route rather than 404. */}
            {PROJECT_VIEWS.map(([path]) => (
              <Route key={`flat-${path}`} path={`/${path}`} element={<FlatRedirect />} />
            ))}
            {/* Retired page. FlatRedirect lands on /p/:tag/dashboard, which then goes home. */}
            <Route path="/dashboard" element={<FlatRedirect />} />
            <Route path="/fleet.v1" element={<FleetTabRedirect tab="manage" />} />
            <Route path="/fleet.v2" element={<FleetTabRedirect tab="harness" />} />
            <Route path="/memory-review" element={<FlatRedirect to="memory" />} />
            <Route path="/memory-triage" element={<FlatRedirect to="memory" />} />
            <Route path="/organization" element={<Navigate to={ORG_BASE} replace />} />
          </>
        )}

        {!hosted && (
          <Route path="/organization" element={<Navigate to="/settings/deployment/sync" replace />} />
        )}
        <Route path="/settings/project/feedback-kit" element={<Navigate to="/feedback-kit" replace />} />
        <Route path="/settings/*" element={<SettingsView />} />
        <Route path="/profile" element={<ProfileView />} />
        <Route path="/usage" element={<UsageView />} />
        <Route index element={<HomeRedirect hosted={hosted} />} />
        <Route path="*" element={<HomeRedirect hosted={hosted} />} />
      </Route>
    </Routes>
  );
}

/** Old hosted dashboard bookmark. The page is project home (GRPH-1066). */
function DashboardRedirect() {
  const { tag = "" } = useParams();
  return <Navigate to={projectPath(tag)} replace />;
}

/** Old `fleet.v1` / `fleet.v2` bookmark: the one Fleet page with the matching tab. */
function FleetTabRedirect({ tab }: { tab: "manage" | "harness" }) {
  const { pathname } = useLocation();
  const dest = pathname.replace(/\/fleet\.v[12]\/?$/, "/fleet");
  return <Navigate to={`${dest}?tab=${tab}`} replace />;
}

/** Old `memory-review` / `memory-triage` bookmark: the one Memory page. */
function MemoryRedirect() {
  const { pathname, search } = useLocation();
  const dest = pathname.replace(/\/memory-(review|triage)\/?$/, "/memory");
  return <Navigate to={`${dest}${search}`} replace />;
}

/**
 * A flat path on a hosted deployment: resolve a project and move to its tag URL.
 *
 * `replace` so the back button does not ping-pong between the flat path and the tag one.
 * Resolution order is last-used then first-readable; when neither exists there is no
 * project to show, so the org plane is the honest destination rather than a project
 * page for a project that does not exist.
 */
function FlatRedirect({ to }: { to?: string }) {
  const { pathname, search } = useLocation();
  const { projects, loading } = useProjectCtx();
  if (loading) return null;

  const view = to ?? pathname.replace(/^\/+/, "");
  const remembered = lastProjectTag();
  const target =
    projects.find((p) => p.tag === remembered) ?? projects[0] ?? null;
  if (!target?.tag) return <Navigate to={ORG_BASE} replace />;
  return <Navigate to={`${projectPath(target.tag, view)}${search}`} replace />;
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
