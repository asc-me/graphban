import { Navigate, Route, Routes } from "react-router-dom";

import { AppFrame } from "@/components/shell/AppFrame";
import { useAuth } from "@/features/auth/AuthContext";
import { LoginPage } from "@/features/auth/LoginPage";
import { ActivityView } from "@/features/activity/ActivityView";
import { AdminView } from "@/features/admin/AdminView";
import { CodeGraphView } from "@/features/code/CodeGraphView";
import { DashboardView } from "@/features/dashboard/DashboardView";
import { EmbedFeedbackPage } from "@/features/feedback/EmbedFeedbackPage";
import { FeedbackKitView } from "@/features/feedback/FeedbackKitView";
import { LinksGraphView } from "@/features/links/LinksGraphView";
import { FleetView } from "@/features/fleet/FleetView";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { MemoryReviewView } from "@/features/memory/MemoryReviewView";
import { InviteAcceptPage } from "@/features/onboarding/InviteAcceptPage";
import { OrganizationView } from "@/features/organization/OrganizationView";
import { PrdEditorView } from "@/features/prds/PrdEditorView";
import { PrdListView } from "@/features/prds/PrdListView";
import { ProfileView } from "@/features/profile/ProfileView";
import { EmbedRoadmapPage } from "@/features/roadmap/EmbedRoadmapPage";
import { RoadmapView } from "@/features/roadmap/RoadmapView";
import { SettingsView } from "@/features/settings/SettingsView";
import { RequestsView } from "@/features/requests/RequestsView";
import { TrackerView } from "@/features/tracker/TrackerView";

export function App() {
  return (
    <Routes>
      {/* Public, unauthenticated embed targets. */}
      <Route path="/embed/feedback" element={<EmbedFeedbackPage />} />
      <Route path="/embed/roadmap" element={<EmbedRoadmapPage />} />
      {/* Emailed org-invite landing — works signed in or out (AL-74b). */}
      <Route path="/invite/:token" element={<InviteAcceptPage />} />
      <Route path="*" element={<AuthedApp />} />
    </Routes>
  );
}

function AuthedApp() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center font-mono text-[12px] text-faint">
        loading…
      </div>
    );
  }
  if (!user) return <LoginPage />;

  return (
    <Routes>
      <Route element={<AppFrame />}>
        <Route index element={<Navigate to="/tracker" replace />} />
        <Route path="/tracker" element={<TrackerView />} />
        <Route path="/requests" element={<RequestsView />} />
        <Route path="/dashboard" element={<DashboardView />} />
        <Route path="/links" element={<LinksGraphView />} />
        <Route path="/code" element={<CodeGraphView />} />
        <Route path="/roadmap" element={<RoadmapView />} />
        <Route path="/mcp-tools" element={<McpToolsView />} />
        <Route path="/fleet" element={<FleetView />} />
        <Route path="/activity" element={<ActivityView />} />
        <Route path="/memory-review" element={<MemoryReviewView />} />
        <Route path="/prds" element={<PrdListView />} />
        <Route path="/prds/:id" element={<PrdEditorView />} />
        <Route path="/feedback-kit" element={<FeedbackKitView />} />
        <Route path="/organization" element={<OrganizationView />} />
        {/* Operator console — the API 404s for non-admins, and the view renders a
            neutral "not available" if so, so the route itself discloses nothing. */}
        <Route path="/admin" element={<AdminView />} />
        <Route path="/settings" element={<SettingsView />} />
        <Route path="/profile" element={<ProfileView />} />
        <Route path="*" element={<Navigate to="/tracker" replace />} />
      </Route>
    </Routes>
  );
}
