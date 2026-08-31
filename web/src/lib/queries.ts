import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";

import { api } from "./api";
import type { GitopsPatch, Item, LessonFilters, RequestItem } from "./types";

export const keys = {
  me: ["me"] as const,
  config: ["config"] as const,
  projects: ["projects"] as const,
  orgs: ["orgs"] as const,
  orgMembers: (id: string) => ["org-members", id] as const,
  invites: (id: string) => ["org-invites", id] as const,
  billing: (id: string) => ["org-billing", id] as const,
  items: ["items"] as const,
  counts: (projectId?: string) => ["counts", projectId] as const,
  shards: ["shards"] as const,
  lessons: (projectId: string, filters?: LessonFilters) =>
    ["lessons", projectId, filters ?? {}] as const,
  lesson: (projectId: string, id: string) => ["lesson", projectId, id] as const,
  requests: ["requests"] as const,
  apiKeys: ["api-keys"] as const,
  prds: ["prds"] as const,
  prd: (id: string) => ["prd", id] as const,
  prdVersions: (id: string) => ["prd-versions", id] as const,
  closeReport: (id: string) => ["close-report", id] as const,
  prdEvidence: (id: string) => ["prd-evidence", id] as const,
  auditCoverage: (id: string) => ["audit-coverage", id] as const,
  gitops: (projectId: string) => ["gitops", projectId] as const,
  orgGitops: (orgId: string) => ["org-gitops", orgId] as const,
};

// ── Deploy config + Organizations (hosted-only, AL-74b) ────────────────────
export function useConfig() {
  // Deploy flags rarely change within a session; cache hard so onboarding logic is stable.
  return useQuery({ queryKey: keys.config, queryFn: () => api.config(), staleTime: Infinity });
}

export function useOrgs(enabled = true) {
  return useQuery({ queryKey: keys.orgs, queryFn: () => api.orgs(), enabled });
}

export function useOrgMembers(orgId?: string) {
  return useQuery({
    queryKey: keys.orgMembers(orgId ?? ""),
    queryFn: () => api.orgMembers(orgId!),
    enabled: !!orgId,
  });
}

export function useInvites(orgId?: string) {
  return useQuery({
    queryKey: keys.invites(orgId ?? ""),
    queryFn: () => api.invites(orgId!),
    enabled: !!orgId,
  });
}

// ── Operator console (AL-94) ───────────────────────────────────────────────
/** Probe: a 404 means "not an operator" (or not hosted) — used to gate the nav entry. */
export function useIsPlatformAdmin() {
  return useQuery({
    queryKey: ["admin-me"],
    queryFn: () => api.adminWhoami(),
    retry: false,
    staleTime: Infinity,
  });
}

export function useAdminOrgs(enabled = true) {
  return useQuery({ queryKey: ["admin-orgs"], queryFn: () => api.adminOrgs(), enabled, retry: false });
}

export function useAdminUsers(enabled = true) {
  return useQuery({ queryKey: ["admin-users"], queryFn: () => api.adminUsers(), enabled, retry: false });
}

export function useAdminInvites(history = false, enabled = true) {
  return useQuery({
    queryKey: ["admin-invites", history],
    queryFn: () => api.adminInvites(history),
    enabled,
    retry: false,
  });
}

export function useAdminActivity(enabled = true) {
  return useQuery({
    queryKey: ["admin-activity"],
    queryFn: () => api.adminActivity(),
    enabled,
    retry: false,
  });
}

export function useAdminOrgRequests(enabled = true) {
  return useQuery({
    queryKey: ["admin-org-requests"],
    queryFn: () => api.adminOrgRequests(),
    enabled,
    retry: false,
  });
}

export function useAdminCreateInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; plan?: string | null }) => api.adminCreateInvite(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-invites"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });
}

export function useAdminRevokeInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.adminRevokeInvite(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-invites"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });
}

export function useAdminDecideOrgRequest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; approve: boolean; note?: string }) =>
      api.adminDecideOrgRequest(v.id, v.approve, v.note ?? ""),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-org-requests"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });
}

export function useSetOrgPlan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { orgId: string; plan: string }) => api.setOrgPlan(v.orgId, v.plan),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-orgs"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
      qc.invalidateQueries({ queryKey: keys.orgs });
    },
  });
}

export function useOrgOverview(orgId?: string) {
  return useQuery({
    queryKey: ["org-overview", orgId],
    queryFn: () => api.orgOverview(orgId!),
    enabled: !!orgId,
  });
}

export function useOrgGitops(orgId?: string) {
  return useQuery({
    queryKey: keys.orgGitops(orgId ?? ""),
    queryFn: () => api.orgGitops(orgId!),
    enabled: !!orgId,
  });
}

export function useUpdateOrgGitops(orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: GitopsPatch) => api.updateOrgGitops(orgId, body),
    onSuccess: (view) => {
      qc.setQueryData(keys.orgGitops(orgId), view);
      qc.invalidateQueries({ queryKey: ["gitops"] });
    },
  });
}

export function useDeployments(orgId?: string) {
  return useQuery({
    queryKey: ["deployments", orgId],
    queryFn: () => api.deployments(orgId!),
    enabled: !!orgId,
  });
}

export function useTeams(orgId?: string) {
  return useQuery({
    queryKey: ["teams", orgId],
    queryFn: () => api.teams(orgId!),
    enabled: !!orgId,
  });
}

/**
 * Every team write also invalidates the org roster and the project members.
 *
 * A grant MATERIALIZES — it writes real `Membership` rows — so a team change silently
 * moves what the Users screen shows. Leaving that cache stale would put two screens in
 * the same session disagreeing about who can reach what.
 */
function useTeamMutation<T>(orgId: string, fn: (v: T) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["teams", orgId] });
      qc.invalidateQueries({ queryKey: keys.orgMembers(orgId) });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
  });
}

export function useCreateTeam(orgId: string) {
  return useTeamMutation(orgId, (v: { name: string; description?: string }) =>
    api.createTeam(orgId, v.name, v.description ?? ""),
  );
}

export function useDeleteTeam(orgId: string) {
  return useTeamMutation(orgId, (teamId: string) => api.deleteTeam(teamId));
}

export function useAddTeamMember(orgId: string) {
  return useTeamMutation(orgId, (v: { teamId: string; userId: string }) =>
    api.addTeamMember(v.teamId, v.userId),
  );
}

export function useRemoveTeamMember(orgId: string) {
  return useTeamMutation(orgId, (v: { teamId: string; userId: string }) =>
    api.removeTeamMember(v.teamId, v.userId),
  );
}

export function useSetTeamGrant(orgId: string) {
  return useTeamMutation(orgId, (v: { teamId: string; projectId: string; access: string }) =>
    api.setTeamGrant(v.teamId, v.projectId, v.access),
  );
}

export function useRevokeTeamGrant(orgId: string) {
  return useTeamMutation(orgId, (v: { teamId: string; projectId: string }) =>
    api.revokeTeamGrant(v.teamId, v.projectId),
  );
}

export function useGalaxy(orgId?: string) {
  return useQuery({
    queryKey: ["galaxy", orgId],
    queryFn: () => api.orgGalaxy(orgId!),
    enabled: !!orgId,
  });
}

export function useBilling(orgId?: string) {
  return useQuery({
    queryKey: keys.billing(orgId ?? ""),
    queryFn: () => api.orgBilling(orgId!),
    enabled: !!orgId,
  });
}

export function useCreateOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.createOrg(name),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.orgs }),
  });
}

// PRD-21 D8. Each invalidates the roster AND billing: a seat freed by a removal changes
// the seat count, and a stale meter beside a shorter table is the mismatch these screens
// spend paragraphs explaining.
function useMembershipMutation<T>(orgId: string, fn: (v: T) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.orgMembers(orgId) });
      qc.invalidateQueries({ queryKey: keys.billing(orgId) });
    },
  });
}

export function useSetMemberRole(orgId: string) {
  return useMembershipMutation(orgId, (v: { userId: string; role: string }) =>
    api.setMemberRole(orgId, v.userId, v.role),
  );
}

export function useRemoveMember(orgId: string) {
  return useMembershipMutation(orgId, (userId: string) => api.removeMember(orgId, userId));
}

export function useSetProjectAccess(orgId: string) {
  return useMembershipMutation(orgId, (v: { projectId: string; userId: string; access: string }) =>
    api.setProjectAccess(v.projectId, v.userId, v.access),
  );
}

export function useCreateInvite(orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, role }: { email: string; role: string }) =>
      api.createInvite(orgId, email, role),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.invites(orgId) });
      qc.invalidateQueries({ queryKey: keys.billing(orgId) }); // seat usage moved
    },
  });
}

export function useRevokeInvite(orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) => api.revokeInvite(orgId, inviteId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.invites(orgId) });
      qc.invalidateQueries({ queryKey: keys.billing(orgId) });
    },
  });
}

export function usePrds(projectId?: string) {
  return useQuery({ queryKey: [...keys.prds, projectId], queryFn: () => api.prds(projectId), enabled: !!projectId });
}

export function useCloseReport(id: string) {
  return useQuery({ queryKey: keys.closeReport(id), queryFn: () => api.closeReport(id), enabled: !!id });
}

export function usePrdEvidence(id: string) {
  return useQuery({ queryKey: keys.prdEvidence(id), queryFn: () => api.prdEvidence(id), enabled: !!id });
}

export function useAuditCoverage(id: string) {
  return useQuery({ queryKey: keys.auditCoverage(id), queryFn: () => api.auditCoverage(id), enabled: !!id });
}

export function useIntentDiff(id: string) {
  return useQuery({
    queryKey: ["intent-diff", id],
    queryFn: () => api.intentDiff(id),
    enabled: !!id,
  });
}

export function useGrillState(id: string) {
  return useQuery({
    queryKey: ["grill", id],
    queryFn: () => api.grillState(id),
    enabled: !!id,
  });
}

export function usePrd(id: string) {
  return useQuery({ queryKey: keys.prd(id), queryFn: () => api.prd(id), enabled: !!id });
}

export function usePrdVersions(id: string) {
  return useQuery({ queryKey: keys.prdVersions(id), queryFn: () => api.prdVersions(id), enabled: !!id });
}

export function useDashboard(projectId?: string) {
  return useQuery({ queryKey: ["dashboard", projectId], queryFn: () => api.dashboard(projectId), enabled: !!projectId });
}

export function useRoadmap(projectId?: string) {
  return useQuery({ queryKey: ["roadmap", projectId], queryFn: () => api.roadmap(projectId), enabled: !!projectId });
}

export function useLinks(projectId?: string) {
  return useQuery({ queryKey: ["links", projectId], queryFn: () => api.links(projectId), enabled: !!projectId });
}

export function useMcpTools() {
  return useQuery({ queryKey: ["mcp-tools"], queryFn: () => api.mcpTools() });
}

export function useEvents(projectId?: string) {
  return useQuery({ queryKey: ["events", projectId], queryFn: () => api.events(projectId) });
}

export function useCodeMap(projectId?: string) {
  return useQuery({ queryKey: ["code-map", projectId], queryFn: () => api.codeMap(projectId) });
}

/**
 * Hubs / components / path for the graph overlay.
 *
 * Keyed on the edge types so toggling a chip refetches rather than serving a ranking computed
 * over edges the canvas is no longer drawing (PRD-20 AC-18). `enabled` keeps the request off
 * the wire until something actually opens the panel — this is the graph view's third query and
 * the heaviest of them.
 */
export function useCodeAnalysis(
  opts: { projectId?: string; edgeTypes?: string[]; limit?: number; a?: string; b?: string },
  enabled = true,
) {
  const { projectId, edgeTypes, limit, a, b } = opts;
  const types = [...(edgeTypes ?? [])].sort();
  return useQuery({
    queryKey: ["code-analysis", projectId, types.join(","), limit, a, b],
    queryFn: () => api.codeAnalysis({ projectId, edgeTypes: types, limit, a, b }),
    enabled,
  });
}

/**
 * Every credential in the caller's SCOPE (PRD-25 S5) — not just the active project's.
 *
 * Keyed on the project id even though the result is deployment-wide, because the id is what
 * resolves the scope server-side and two projects in different orgs must not share a cache
 * entry. `used_by` and `falling_back` come from the endpoint on every read rather than being
 * derived here: the tags exist so a delete refusal is predictable, and a tag computed from a
 * stale cache is exactly the drift this view was built to remove.
 */
export function useCredentials(projectId: string) {
  return useQuery({
    queryKey: ["credentials", projectId],
    queryFn: () => api.credentials(projectId),
    enabled: !!projectId,
  });
}

/** Re-index progress, PER TABLE. Polls only while a run is in flight. */
export function useReindexStatus(projectId: string) {
  return useQuery({
    queryKey: ["reindex", projectId],
    queryFn: () => api.reindexStatus(projectId),
    enabled: !!projectId,
    refetchInterval: (q) => (q.state.data?.running ? 2000 : false),
  });
}

export function usePlatform(projectId: string) {
  return useQuery({
    queryKey: ["platform", projectId],
    queryFn: () => api.platform(projectId),
    enabled: !!projectId,
  });
}

// Instance-wide cloud link + per-project sync state (AL-141). Not project-keyed — the link
// is one per instance and the payload already carries every readable project's state.
export function useSyncStatus() {
  return useQuery({ queryKey: ["sync-status"], queryFn: () => api.syncStatus() });
}

export function useGitops(projectId: string) {
  return useQuery({
    queryKey: keys.gitops(projectId),
    queryFn: () => api.gitops(projectId),
    enabled: !!projectId,
    // Production QueryClient is staleTime 15s. A cached local `test` after
    // link, or a grey org row after unlink, is the lie this page exists to
    // prevent — drop the row when the pane unmounts so remount cannot
    // first-paint it. Sync link/unlink also removeQueries(["gitops"]).
    staleTime: 0,
    gcTime: 0,
  });
}

export function useUpdateGitops(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: GitopsPatch) => api.updateGitops(projectId, body),
    onSuccess: (data) => qc.setQueryData(keys.gitops(projectId), data),
  });
}

export function useMembers(projectId: string) {
  return useQuery({ queryKey: ["members", projectId], queryFn: () => api.members(projectId), enabled: !!projectId });
}

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: api.me });
}

export function useProjects() {
  return useQuery({ queryKey: keys.projects, queryFn: () => api.projects() });
}

export function useItems(projectId?: string) {
  return useQuery({ queryKey: [...keys.items, projectId], queryFn: () => api.items(projectId), enabled: !!projectId });
}

export function useUpdateItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Item> }) => api.updateItem(id, body),
    onMutate: async ({ id, body }) => {
      await qc.cancelQueries({ queryKey: keys.items });
      const prev = qc.getQueriesData<Item[]>({ queryKey: keys.items });
      qc.setQueriesData<Item[]>({ queryKey: keys.items }, (old) =>
        old?.map((it) => (it.id === id ? { ...it, ...body } : it)),
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev?.forEach(([key, data]) => qc.setQueryData(key, data)),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.items }),
  });
}

export function useCreateItem(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Item>) => api.createItem(projectId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.items }),
  });
}

export function useReorderItems() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderedIds: string[]) => api.reorderItems(orderedIds),
    onMutate: async (orderedIds) => {
      await qc.cancelQueries({ queryKey: keys.items });
      const prev = qc.getQueriesData<Item[]>({ queryKey: keys.items });
      qc.setQueriesData<Item[]>({ queryKey: keys.items }, (old) => {
        if (!old) return old;
        const map = new Map(old.map((i) => [i.id, i]));
        return orderedIds.map((id, idx) => ({ ...map.get(id)!, sort_order: idx }));
      });
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev?.forEach(([key, data]) => qc.setQueryData(key, data)),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.items }),
  });
}

export function useShards(projectId?: string, limit?: number) {
  return useQuery({
    queryKey: [...keys.shards, projectId, limit ?? null],
    queryFn: () => api.shards(projectId, limit),
    enabled: !!projectId,
  });
}

/**
 * The shell's badge numbers, in one small request.
 *
 * The nav and the project bar previously called `useItems`, `useRequests`,
 * `useCandidateShards` and `useShards` purely to read `.length` off them, so every route paid
 * for the whole project: 765 KB of items, 740 KB of shards, 621 KB of candidates (GRPH-431).
 * Anything that needs the rows themselves still fetches them — this is only for counting.
 */
export function useCounts(projectId?: string) {
  return useQuery({
    queryKey: keys.counts(projectId),
    queryFn: () => api.counts(projectId!),
    enabled: !!projectId,
  });
}

export function useAddShard(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { text: string; scope?: string }) => api.addShard(projectId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.shards }),
  });
}

export function useCandidateShards(projectId?: string) {
  return useQuery({
    queryKey: ["shard-candidates", projectId],
    queryFn: () => api.candidateShards(projectId),
    enabled: !!projectId,
  });
}

export function useCandidateClusters(projectId?: string) {
  return useQuery({
    queryKey: ["shard-clusters", projectId],
    queryFn: () => api.candidateClusters(projectId),
    enabled: !!projectId,
  });
}

export function useScoredCandidates(projectId?: string) {
  return useQuery({
    queryKey: ["shard-scored", projectId],
    queryFn: () => api.scoredCandidates(projectId),
    enabled: !!projectId,
  });
}

// AL-227: shards the scorer published/rejected without a human — the review lane.
export function useAutoActions(projectId?: string) {
  return useQuery({
    queryKey: ["shard-auto-actions", projectId],
    queryFn: () => api.autoActions(projectId),
    enabled: !!projectId,
  });
}

function invalidateReview(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["shard-candidates"] });
  qc.invalidateQueries({ queryKey: ["shard-clusters"] });
  qc.invalidateQueries({ queryKey: ["shard-scored"] });
  qc.invalidateQueries({ queryKey: ["shard-auto-actions"] });
  qc.invalidateQueries({ queryKey: keys.shards });
}

export function useReviewShard() {
  const qc = useQueryClient();
  const invalidate = () => invalidateReview(qc);
  return {
    publish: useMutation({ mutationFn: (id: string) => api.publishShard(id), onSuccess: invalidate }),
    reject: useMutation({ mutationFn: (id: string) => api.rejectShard(id), onSuccess: invalidate }),
  };
}

// AL-227: undo an auto-action — return the shard to the candidate queue.
export function useUndoAutoShard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.undoAutoShard(id),
    onSuccess: () => invalidateReview(qc),
  });
}

export function usePromoteCluster() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { publishId: string; rejectIds: string[] }) =>
      api.promoteCluster(v.publishId, v.rejectIds),
    onSuccess: () => invalidateReview(qc),
  });
}

export function useLessons(projectId?: string, filters?: LessonFilters) {
  return useQuery({
    queryKey: keys.lessons(projectId ?? "", filters),
    queryFn: () => api.lessons(projectId!, filters),
    enabled: !!projectId,
  });
}

export function useLesson(projectId?: string, id?: string) {
  return useQuery({
    queryKey: keys.lesson(projectId ?? "", id ?? ""),
    queryFn: () => api.lesson(projectId!, id!),
    enabled: !!projectId && !!id,
  });
}

function invalidateLessons(qc: ReturnType<typeof useQueryClient>, projectId: string, id: string) {
  qc.invalidateQueries({ queryKey: ["lessons", projectId] });
  qc.invalidateQueries({ queryKey: keys.lesson(projectId, id) });
}

export function useRecordLessonOutcome(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; kind: string; detail: string }) =>
      api.recordLessonOutcome(v.id, { kind: v.kind, detail: v.detail }),
    onSuccess: (row, v) => {
      qc.setQueryData(keys.lesson(projectId, v.id), row);
      invalidateLessons(qc, projectId, v.id);
    },
  });
}

export function usePromoteOrgLesson(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; override_reason?: string }) =>
      api.promoteOrgLesson(v.id, v.override_reason),
    onSuccess: (row, v) => {
      qc.setQueryData(keys.lesson(projectId, v.id), row);
      invalidateLessons(qc, projectId, v.id);
    },
  });
}

export function useRequests(projectId?: string) {
  return useQuery({ queryKey: [...keys.requests, projectId], queryFn: () => api.requests(projectId), enabled: !!projectId });
}

export function useTriageQueue(projectId: string) {
  return useQuery({
    queryKey: ["triage", projectId],
    queryFn: () => api.triageQueue(projectId),
    enabled: !!projectId,
  });
}

export function useAcceptRequest(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.acceptRequest(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["triage", projectId] });
      qc.invalidateQueries({ queryKey: keys.requests });
      qc.invalidateQueries({ queryKey: keys.items });
    },
  });
}

export function useVoteRequest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, delta }: { id: string; delta: number }) => api.voteRequest(id, delta),
    onMutate: async ({ id, delta }) => {
      await qc.cancelQueries({ queryKey: keys.requests });
      const prev = qc.getQueriesData<RequestItem[]>({ queryKey: keys.requests });
      qc.setQueriesData<RequestItem[]>({ queryKey: keys.requests }, (old) =>
        old?.map((r) => (r.id === id ? { ...r, votes: Math.max(0, r.votes + delta) } : r)),
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev?.forEach(([key, data]) => qc.setQueryData(key, data)),
    onSettled: () => qc.invalidateQueries({ queryKey: keys.requests }),
  });
}

export function useLinkRequest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, itemId }: { id: string; itemId: string | null }) =>
      api.linkRequest(id, itemId),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.requests }),
  });
}

export function useFleet(projectId?: string) {
  // Polled: presence is derived from last contact, so the roster only changes when time
  // passes. Without a refetch an agent that died stays green until somebody navigates away.
  return useQuery({
    queryKey: ["fleet", projectId],
    queryFn: () => api.fleet(projectId),
    // Gated: without a project the server resolves the request against its default one,
    // so an ungated call renders another project's fleet for a frame before the real
    // one arrives. Harmless-looking, and exactly the class of thing D1 deleted.
    enabled: !!projectId,
    refetchInterval: 15000,
  });
}

/**
 * Live presence for the graph views (PRD-20 D4).
 *
 * **Polled at the interval the SERVER reports**, not a hardcoded number: presence is only as
 * fresh as the heartbeat that feeds it, and asking faster than agents report renders a
 * confidence we do not have. The payload carries `heartbeat_interval_seconds`, so the cadence
 * corrects itself on the first response rather than needing a second call to learn it.
 * `enabled` lets a view that is not showing a graph stop paying for the poll at all.
 */
export function useFleetPresence(projectId?: string, enabled = true) {
  const [intervalMs, setIntervalMs] = React.useState(50_000);
  return useQuery({
    queryKey: ["fleet-presence", projectId],
    queryFn: async () => {
      const res = await api.fleetPresence(projectId);
      if (res.heartbeat_interval_seconds > 0) {
        setIntervalMs(res.heartbeat_interval_seconds * 1000);
      }
      return res;
    },
    refetchInterval: intervalMs,
    enabled: enabled && !!projectId,
  });
}

export function useApiKeys() {
  return useQuery({ queryKey: keys.apiKeys, queryFn: () => api.apiKeys() });
}
