import type { FleetOverview } from "@/lib/types";
/**
 * Typed fetch client. Access token is kept in memory; the refresh token lives in
 * localStorage so a reload can silently re-auth. On a 401 the client attempts one
 * refresh, then retries the original request.
 */
import type {
  AdminOrg,
  AdminUser,
  AiProvider,
  ApiKey,
  ApiKeyCreated,
  AppConfig,
  AuditCoverage,
  Billing,
  ChatResponse,
  CloseReport,
  EvidenceRollup,
  OrgRequest,
  ProviderConfigUpdate,
  CodeAnswer,
  CodeForRefRow,
  CodeHit,
  CodeMap,
  CodeNeighbors,
  CodeRef,
  DashboardData,
  GraphLink,
  Invite,
  InvitePreview,
  Item,
  EventPage,
  GrillMessage,
  GrillState,
  IntentDiff,
  McpToolInfo,
  Org,
  OrgMember,
  ShardCluster,
  Member,
  PlatformConfig,
  Prd,
  PrdCoverage,
  PrdStatus,
  PrdSummary,
  PrdVersion,
  Project,
  RequestItem,
  RoadmapPhase,
  ScoredCandidate,
  AssistantThread,
  AssistantThreadDetail,
  AssistantProvider,
  ProposedAction,
  Shard,
  ShardHit,
  Status,
  SyncBundle,
  SyncStatus,
  User,
} from "./types";

const REFRESH_KEY = "al_refresh";

let accessToken: string | null = null;

// The project the app is currently scoped to. Writes (create item / shard / PRD,
// platform settings) target this project. ProjectProvider keeps it in sync with the
// active project so no create silently falls back to a non-existent default.
let activeProjectId: string | undefined;
export function setActiveProjectId(id: string | undefined) {
  activeProjectId = id;
}
function projectQuery(): string {
  return activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
}

export function setRefreshToken(t: string | null) {
  if (t) localStorage.setItem(REFRESH_KEY, t);
  else localStorage.removeItem(REFRESH_KEY);
}
export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}
export function setAccessToken(t: string | null) {
  accessToken = t;
}
export function hasSession(): boolean {
  return !!accessToken || !!getRefreshToken();
}

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function refresh(): Promise<boolean> {
  const rt = getRefreshToken();
  if (!rt) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: rt }),
  });
  if (!res.ok) {
    setRefreshToken(null);
    accessToken = null;
    return false;
  }
  const data = await res.json();
  accessToken = data.access_token;
  setRefreshToken(data.refresh_token);
  return true;
}

async function request<T>(path: string, opts: RequestInit = {}, retry = true): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers as Record<string, string>),
  };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const res = await fetch(`/api${path}`, { ...opts, headers });

  if (res.status === 401 && retry && (await refresh())) {
    return request<T>(path, opts, false);
  }
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  async login(email: string, password: string): Promise<User> {
    const data = await request<{ access_token: string; refresh_token: string }>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      false,
    );
    accessToken = data.access_token;
    setRefreshToken(data.refresh_token);
    return this.me();
  },
  async register(
    name: string,
    email: string,
    handle: string,
    password: string,
    inviteToken?: string,
  ): Promise<User> {
    const data = await request<{ access_token: string; refresh_token: string }>(
      "/auth/register",
      {
        method: "POST",
        body: JSON.stringify({ name, email, handle, password, invite_token: inviteToken ?? null }),
      },
      false,
    );
    accessToken = data.access_token;
    setRefreshToken(data.refresh_token);
    return this.me();
  },
  async logout() {
    // Best-effort server-side revocation (AL-59): bumps token_version so this
    // session's refresh token can't outlive the logout. Clear locally regardless.
    try {
      await request<void>("/auth/logout", { method: "POST" }, false);
    } catch {
      /* already expired / offline — local clear below is what matters for the UI */
    }
    accessToken = null;
    setRefreshToken(null);
  },
  async changePassword(currentPassword: string, newPassword: string): Promise<void> {
    // The server revokes EVERY session on a password change, including this one, so it
    // hands back a fresh pair. Storing it is what stops a successful change reading as an
    // instant logout — without this the next request 401s and the user is bounced to the
    // login page wondering whether the change even took.
    const data = await request<{ access_token: string; refresh_token: string }>(
      "/auth/me/password",
      {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      },
    );
    accessToken = data.access_token;
    setRefreshToken(data.refresh_token);
  },
  // Fleet (PRD-17 D5). One read for the whole view: a roster that arrives before the review
  // queue would show an idle reviewer beside work it could already be taking.
  fleet: (projectId?: string) =>
    request<FleetOverview>(`/fleet${projectId ? `?project_id=${projectId}` : ""}`),
  mintFleetKey: (body: { project_id: string; role: string; wave: string; label?: string }) =>
    request<{ id: string; plaintext: string; role: string; wave: string; prefix: string }>(
      "/fleet/keys", { method: "POST", body: JSON.stringify(body) }),
  issueSeats: (body: { project_id: string; roles: string[]; wave?: string }) =>
    request<{ wave: string; seats: { id: string; role: string; code: string; expires_at: string }[] }>(
      "/fleet/seats", { method: "POST", body: JSON.stringify(body) }),
  revokeUnusedSeats: (projectId: string, wave?: string) =>
    request<{ revoked: number }>("/fleet/seats/revoke-unused",
      { method: "POST", body: JSON.stringify({ project_id: projectId, wave }) }),
  revokeKey: (keyId: string) =>
    request<unknown>(`/api-keys/${keyId}`, { method: "DELETE" }),
  reissueSeat: (seatId: string) =>
    request<{ id: string; role: string; code: string; reissued_from: string }>(
      `/fleet/seats/${seatId}/reissue`, { method: "POST" }),
  endWavePreview: (projectId: string, wave: string) =>
    request<{ keys: number; seats: number; agents: number; leases: number; reservations: number }>(
      `/fleet/end-wave?project_id=${projectId}&wave=${wave}`),
  endWave: (projectId: string, wave: string) =>
    request<{ keys_revoked: number; leases_released: number; reservations_released: number }>(
      "/fleet/end-wave", { method: "POST", body: JSON.stringify({ project_id: projectId, wave }) }),
  me: () => request<User>("/auth/me"),
  myMemberships: () =>
    request<{ project_id: string; project_name: string; accent: string; role: string; access: string }[]>(
      "/auth/me/memberships",
    ),

  // Deploy flags (hosted vs. self-host) the SPA reads before login.
  config: () => request<AppConfig>("/config"),

  projects: () => request<Project[]>("/projects"),
  createProject: (body: {
    name: string;
    /** Omit to let the server derive one from `name`. */
    tag?: string;
    accent?: string;
    description?: string;
    org_id?: string;
  }) => request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),

  // Tag derivation lives server-side so the form and the API can't drift apart — the
  // implementation that matters is the one that actually assigns the tag (PRD-13).
  tagSuggestion: (name: string) =>
    request<{ tag: string }>(`/projects/tag-suggestion?name=${encodeURIComponent(name)}`),
  // Its own endpoint, not a PATCH field: moving a tag records tag history so keys
  // rendered under the old one keep resolving (PRD-13).
  retagProject: (id: string, tag: string) =>
    request<Project>(`/projects/${id}/retag`, { method: "POST", body: JSON.stringify({ tag }) }),
  tagCheck: (tag: string) =>
    request<{ tag: string; available: boolean; reason: string }>(
      `/projects/tag-check?tag=${encodeURIComponent(tag)}`,
    ),

  // ── Organizations (hosted-only, AL-74b) ───────────────────────────────
  orgs: () => request<Org[]>("/orgs"),
  createOrg: (name: string) =>
    request<Org>("/orgs", { method: "POST", body: JSON.stringify({ name }) }),
  orgMembers: (orgId: string) => request<OrgMember[]>(`/orgs/${orgId}/members`),
  invites: (orgId: string) => request<Invite[]>(`/orgs/${orgId}/invites`),
  createInvite: (orgId: string, email: string, role: string) =>
    request<Invite>(`/orgs/${orgId}/invites`, {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),
  revokeInvite: (orgId: string, inviteId: string) =>
    request<void>(`/orgs/${orgId}/invites/${inviteId}`, { method: "DELETE" }),
  previewInvite: (token: string) => request<InvitePreview>(`/invites/${token}/preview`),
  acceptInvite: (token: string) =>
    request<Org>("/invites/accept", { method: "POST", body: JSON.stringify({ token }) }),
  orgBilling: (orgId: string) => request<Billing>(`/orgs/${orgId}/billing`),
  setOrgPlan: (orgId: string, plan: string) =>
    request<Org>(`/orgs/${orgId}/plan`, { method: "PUT", body: JSON.stringify({ plan }) }),
  requestAdditionalOrg: (body: { reason: string; company?: string }) =>
    request<OrgRequest>("/orgs/requests", { method: "POST", body: JSON.stringify(body) }),

  // ── Operator console (AL-94). Every call 404s unless the caller is a
  // platform admin on a hosted deployment, so the surface is invisible to tenants.
  adminWhoami: () => request<{ is_platform_admin: boolean; email: string }>("/admin/me"),
  adminOrgs: () => request<AdminOrg[]>("/admin/orgs"),
  adminUsers: () => request<AdminUser[]>("/admin/users"),
  adminInvites: () => request<Invite[]>("/admin/invites"),
  adminCreateInvite: (body: { email: string; plan?: string | null }) =>
    request<Invite>("/admin/invites", { method: "POST", body: JSON.stringify(body) }),
  adminRevokeInvite: (id: string) => request<void>(`/admin/invites/${id}`, { method: "DELETE" }),
  adminOrgRequests: () => request<OrgRequest[]>("/admin/org-requests"),
  adminDecideOrgRequest: (id: string, approve: boolean, note = "") =>
    request<OrgRequest>(`/admin/org-requests/${id}`, {
      method: "POST",
      body: JSON.stringify({ approve, note }),
    }),

  items: (projectId?: string) =>
    request<Item[]>(`/items${projectId ? `?project_id=${projectId}` : ""}`),
  createItem: (body: Partial<Item>) =>
    request<Item>("/items", {
      method: "POST",
      body: JSON.stringify({ project_id: activeProjectId, ...body }),
    }),
  updateItem: (id: string, body: Partial<Item>) =>
    request<Item>(`/items/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  reorderItems: (orderedIds: string[]) =>
    request<Item[]>("/items/reorder", {
      method: "PATCH",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),

  shards: (projectId?: string) =>
    request<Shard[]>(`/memory/shards${projectId ? `?project_id=${projectId}` : ""}`),
  addShard: (body: { text: string; scope?: string; item_id?: string | null }) =>
    request<Shard>("/memory/shards", {
      method: "POST",
      body: JSON.stringify({ project_id: activeProjectId, ...body }),
    }),
  searchMemory: (query: string, top_k = 5) =>
    request<ShardHit[]>("/memory/search", {
      method: "POST",
      body: JSON.stringify({ query, top_k, project_id: activeProjectId }),
    }),
  candidateShards: (projectId?: string) =>
    request<Shard[]>(`/memory/candidates${projectId ? `?project_id=${projectId}` : ""}`),
  candidateClusters: (projectId?: string) =>
    request<ShardCluster[]>(`/memory/candidate-clusters${projectId ? `?project_id=${projectId}` : ""}`),
  scoredCandidates: (projectId?: string) =>
    request<ScoredCandidate[]>(`/memory/candidates/scored${projectId ? `?project_id=${projectId}` : ""}`),
  publishShard: (id: string) =>
    request<Shard>(`/memory/shards/${id}/publish`, { method: "POST" }),
  rejectShard: (id: string) =>
    request<Shard>(`/memory/shards/${id}/reject`, { method: "POST" }),
  promoteCluster: (publish_id: string, reject_ids: string[]) =>
    request<{ published: string; rejected: string[] }>("/memory/promote-cluster", {
      method: "POST",
      body: JSON.stringify({ publish_id, reject_ids }),
    }),
  // AL-227: the "recent auto-actions" lane + undo.
  autoActions: (projectId?: string) =>
    request<Shard[]>(`/memory/auto-actions${projectId ? `?project_id=${projectId}` : ""}`),
  undoAutoShard: (id: string) =>
    request<Shard>(`/memory/shards/${id}/undo-auto`, { method: "POST" }),

  requests: (projectId?: string) =>
    request<RequestItem[]>(`/requests${projectId ? `?project_id=${projectId}` : ""}`),
  voteRequest: (id: string, delta = 1) =>
    request<RequestItem>(`/requests/${id}/vote`, {
      method: "POST",
      body: JSON.stringify({ delta }),
    }),
  linkRequest: (id: string, itemId: string | null) =>
    request<RequestItem>(`/requests/${id}/link`, {
      method: "POST",
      body: JSON.stringify({ item_id: itemId }),
    }),

  apiKeys: () => request<ApiKey[]>("/api-keys"),
  createApiKey: (
    name: string,
    projectId: string | null,
    expiresInDays?: number | null,
    scopes?: string[],
  ) =>
    request<ApiKeyCreated>("/api-keys", {
      method: "POST",
      body: JSON.stringify({
        name,
        project_id: projectId,
        expires_in_days: expiresInDays ?? null,
        // Omitted → the backend default (["read","write"]). A sync credential passes
        // ["sync"] and must pin to a project; the backend rejects a global one.
        ...(scopes ? { scopes } : {}),
      }),
    }),
  revokeApiKey: (id: string) => request<void>(`/api-keys/${id}`, { method: "DELETE" }),

  prds: (projectId?: string) =>
    request<PrdSummary[]>(`/prds${projectId ? `?project_id=${projectId}` : ""}`),
  prd: (id: string) => request<Prd>(`/prds/${id}`),
  prdCoverage: (id: string) => request<PrdCoverage>(`/prds/${id}/coverage`),
  decomposePrd: (id: string, create: boolean) =>
    request<{ prd_id: string; proposals: { section: string; title: string }[]; created: string[] }>(
      `/prds/${id}/decompose?create=${create}`,
      { method: "POST" },
    ),
  createPrd: (title: string, template = "standard", body?: string) =>
    request<Prd>("/prds", {
      method: "POST",
      body: JSON.stringify({ title, template, project_id: activeProjectId, body }),
    }),
  updatePrd: (id: string, body: { title?: string; status?: PrdStatus; body?: string }) =>
    request<Prd>(`/prds/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  prdVersions: (id: string) => request<PrdVersion[]>(`/prds/${id}/versions`),
  snapshotPrd: (id: string, note: string) =>
    request<Prd>(`/prds/${id}/versions`, { method: "POST", body: JSON.stringify({ note }) }),
  linkPrd: (id: string, itemId: string, add: boolean) =>
    request<Prd>(`/prds/${id}/link`, { method: "POST", body: JSON.stringify({ item_id: itemId, add }) }),
  prdAi: (id: string, command: string) =>
    request<{ text: string }>(`/prds/${id}/ai`, {
      method: "POST",
      body: JSON.stringify({ command }),
    }),
  grillState: (id: string) => request<GrillState>(`/prds/${id}/grill`),

  intentDiff: (id: string) => request<IntentDiff>(`/prds/${id}/intent-diff`),
  // Delivery acceptance (PRD-12). `close-report` reads the ORIGINAL baseline, which is
  // why it is the spine of the reviewer view: a section a rebaseline removed does not
  // exist in the governing one, so no other surface can show it was cut.
  closeReport: (id: string) => request<CloseReport>(`/prds/${id}/close-report`),
  prdEvidence: (id: string) => request<EvidenceRollup>(`/prds/${id}/evidence`),
  auditCoverage: (id: string) => request<AuditCoverage>(`/prds/${id}/audit-coverage`),

  grillDefer: (id: string, dimension: string, reason: string) =>
    request<GrillState>(`/prds/${id}/grill/defer`, {
      method: "POST",
      body: JSON.stringify({ dimension, reason }),
    }),

  grillApply: (id: string, history: GrillMessage[]) =>
    request<{ body: string; decisions_captured: number }>(`/prds/${id}/grill/apply`, {
      method: "POST",
      body: JSON.stringify({ history }),
    }),
  async grillStream(
    id: string,
    message: string,
    history: GrillMessage[],
    onDelta: (text: string) => void,
    retry = true,
  ): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const res = await fetch(`/api/prds/${id}/grill/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message, history }),
    });
    if (res.status === 401 && retry && (await refresh())) {
      return this.grillStream(id, message, history, onDelta, false);
    }
    if (!res.ok || !res.body) throw new Error(`grill stream failed: ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = frame.match(/^event: (.*)$/m)?.[1];
        const data = frame.match(/^data: ([\s\S]*)$/m)?.[1];
        if (event === "delta" && data !== undefined) onDelta(JSON.parse(data).text);
      }
    }
  },

  // ── In-app AI assistant (AL-175) ──────────────────────────────────────────
  assistantThreads: (projectId: string, entityType: string, entityId: string) =>
    request<AssistantThread[]>(
      `/assistant/threads?project_id=${projectId}&entity_type=${entityType}&entity_id=${encodeURIComponent(entityId)}`,
    ),
  createAssistantThread: (body: { project_id: string; entity_type: string; entity_id: string; provider?: string }) =>
    request<AssistantThread>("/assistant/threads", { method: "POST", body: JSON.stringify(body) }),
  getAssistantThread: (id: string) => request<AssistantThreadDetail>(`/assistant/threads/${id}`),
  assistantProviders: (projectId: string) =>
    request<{ providers: AssistantProvider[] }>(`/assistant/providers?project_id=${projectId}`),
  setThreadModel: (id: string, provider: string, model: string) =>
    request<AssistantThread>(`/assistant/threads/${id}/model`, {
      method: "POST",
      body: JSON.stringify({ provider, model }),
    }),
  applyAction: (id: string) =>
    request<{ status: string; result: string }>(`/assistant/actions/${id}/apply`, { method: "POST" }),
  rejectAction: (id: string) =>
    request<{ status: string }>(`/assistant/actions/${id}/reject`, { method: "POST" }),

  /** Stream one assistant turn over SSE, dispatching each event to a handler. */
  async assistantStream(
    threadId: string,
    message: string,
    handlers: {
      onDelta?: (text: string) => void;
      onToolCall?: (c: { id: string; name: string; input: Record<string, unknown> }) => void;
      onToolResult?: (r: { id: string; content: string; is_error: boolean }) => void;
      onProposed?: (a: ProposedAction) => void;
      onError?: (message: string) => void;
    },
    retry = true,
  ): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const res = await fetch(`/api/assistant/threads/${threadId}/message`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message }),
    });
    if (res.status === 401 && retry && (await refresh())) {
      return this.assistantStream(threadId, message, handlers, false);
    }
    if (!res.ok || !res.body) throw new Error(`assistant stream failed: ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = frame.match(/^event: (.*)$/m)?.[1];
        const data = frame.match(/^data: ([\s\S]*)$/m)?.[1];
        if (data === undefined) continue;
        if (event === "delta") handlers.onDelta?.(JSON.parse(data).text);
        else if (event === "tool_call") handlers.onToolCall?.(JSON.parse(data));
        else if (event === "tool_result") handlers.onToolResult?.(JSON.parse(data));
        else if (event === "proposed_action") handlers.onProposed?.(JSON.parse(data));
        else if (event === "error") handlers.onError?.(JSON.parse(data).message);
      }
    }
  },

  dashboard: (projectId?: string) =>
    request<DashboardData>(`/dashboard${projectId ? `?project_id=${projectId}` : ""}`),
  roadmap: (projectId?: string) =>
    request<RoadmapPhase[]>(`/roadmap${projectId ? `?project_id=${projectId}` : ""}`),
  links: (projectId?: string) =>
    request<GraphLink[]>(`/links${projectId ? `?project_id=${projectId}` : ""}`),
  mcpTools: () => request<{ live: number; tools: McpToolInfo[] }>("/mcp/tools"),
  events: (projectId?: string, limit = 100) =>
    request<EventPage>(`/events?limit=${limit}${projectId ? `&project_id=${projectId}` : ""}`),

  platform: () => request<PlatformConfig>(`/platform${projectQuery()}`),
  updatePlatform: (body: Partial<PlatformConfig>) =>
    request<PlatformConfig>(`/platform${projectQuery()}`, { method: "PATCH", body: JSON.stringify(body) }),
  aiProviders: () => request<{ providers: AiProvider[] }>("/platform/providers"),
  saveProviders: (body: { active_chat_provider?: string; providers?: Record<string, ProviderConfigUpdate> }) =>
    request<PlatformConfig>(`/platform${projectQuery()}`, { method: "PATCH", body: JSON.stringify(body) }),
  githubConnect: (account: string, repo: string) =>
    request<PlatformConfig>(`/platform/github/connect${projectQuery()}`, { method: "POST", body: JSON.stringify({ account, repo }) }),
  githubDisconnect: () => request<PlatformConfig>(`/platform/github/disconnect${projectQuery()}`, { method: "POST" }),
  gdriveConnect: (account: string, folder: string) =>
    request<PlatformConfig>(`/platform/gdrive/connect${projectQuery()}`, { method: "POST", body: JSON.stringify({ account, folder }) }),
  gdriveDisconnect: () => request<PlatformConfig>(`/platform/gdrive/disconnect${projectQuery()}`, { method: "POST" }),
  gdriveSync: () =>
    request<{
      folder: string;
      prds_dir: string;
      exported: string[];
      imported: string[];
      updated_db: string[];
      updated_file: string[];
      conflicts: string[];
      in_sync: number;
    }>(`/platform/gdrive/sync${projectQuery()}`, { method: "POST" }),

  // Local↔cloud sync link (AL-141). Status/link/unlink are instance-wide; push/purge/
  // export/import are per-project (the credential is resolved server-side).
  syncStatus: () => request<SyncStatus>("/sync/status"),
  // Toggle a specific project's graph-push opt-out — parameterized by project_id because the
  // Sync page scopes to any readable project, not only the globally-active one.
  syncSetGraph: (project_id: string, sync_graph: boolean) =>
    request<PlatformConfig>(`/platform?project_id=${encodeURIComponent(project_id)}`, {
      method: "PATCH",
      body: JSON.stringify({ sync_graph }),
    }),
  syncLink: (cloud_url: string, api_key: string, org: string) =>
    request<SyncStatus>("/sync/link", { method: "POST", body: JSON.stringify({ cloud_url, api_key, org }) }),
  syncUnlink: () => request<SyncStatus>("/sync/link", { method: "DELETE" }),
  syncPush: (project_id: string) =>
    request<{ project_id: string; pushed?: number; removed?: number; unchanged?: number; skipped?: boolean; reason?: string }>(
      "/sync/push", { method: "POST", body: JSON.stringify({ project_id }) }),
  syncPurge: (project_id: string) =>
    request<{ project_id: string; deleted_nodes?: number; deleted_edges?: number }>(
      "/sync/purge", { method: "POST", body: JSON.stringify({ project_id }) }),
  syncExport: (project_id: string) =>
    request<SyncBundle>(`/sync/export?project_id=${encodeURIComponent(project_id)}`),
  syncImport: (project_id: string, bundle: { nodes?: unknown[]; edges?: unknown[] }, prune: boolean) =>
    request<{ project_id: string; nodes_upserted: number; edges_upserted: number }>(
      "/sync/import", { method: "POST", body: JSON.stringify({ project_id, nodes: bundle.nodes ?? [], edges: bundle.edges ?? [], prune }) }),

  updateProject: (id: string, body: Partial<Project>) =>
    request<Project>(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  members: (id: string) => request<Member[]>(`/projects/${id}/members`),

  chat: (message: string, projectId?: string) =>
    request<ChatResponse>("/agent/chat", {
      method: "POST",
      body: JSON.stringify({ message, project_id: projectId }),
    }),

  /** Stream a chat reply over SSE, invoking onDelta as tokens arrive. */
  async chatStream(
    message: string,
    handlers: { onDelta: (text: string) => void; onShards?: (shards: ShardHit[]) => void },
    projectId?: string,
    retry = true,
  ): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const res = await fetch("/api/agent/chat/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message, project_id: projectId }),
    });
    if (res.status === 401 && retry && (await refresh())) {
      return this.chatStream(message, handlers, projectId, false);
    }
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = frame.match(/^event: (.*)$/m)?.[1];
        const data = frame.match(/^data: ([\s\S]*)$/m)?.[1];
        if (!event || data === undefined) continue;
        if (event === "delta") handlers.onDelta(JSON.parse(data).text);
        else if (event === "shards") handlers.onShards?.(JSON.parse(data));
      }
    }
  },

  // ── Code structure graph ──────────────────────────────────────────────
  codeMap: (projectId?: string) =>
    request<CodeMap>(`/agent/code/map${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  codeNeighbors: (path: string, projectId?: string) =>
    request<CodeNeighbors>(
      `/agent/code/neighbors?path=${encodeURIComponent(path)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),
  codeChat: (message: string, projectId?: string) =>
    request<CodeAnswer>("/agent/code", {
      method: "POST",
      body: JSON.stringify({ message, project_id: projectId }),
    }),

  // ── upstream "report an issue with Graphban" ───────────────────────
  upstreamConfig: () => request<{ enabled: boolean; target: string }>("/reports/upstream"),
  upstreamReport: (body: { type: string; title: string; detail?: string }) =>
    request<{ ok: boolean; request_id: string | null; duplicates: unknown[] }>("/reports/upstream", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── item/request ↔ code bridge ────────────────────────────────────────
  codeForRef: (refId: string, projectId?: string) =>
    request<CodeForRefRow[]>(
      `/agent/code/for?ref_id=${encodeURIComponent(refId)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),
  codeLink: (body: { ref_id: string; path: string; relation?: string }, projectId?: string) =>
    request<CodeRef>(`/agent/code/link${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  codeUnlink: (body: { ref_id: string; path: string; relation?: string }, projectId?: string) =>
    request<{ removed: number }>(`/agent/code/unlink${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Stream a code-graph answer over SSE. Emits a `nodes` event, then `delta`s, then `done`. */
  async codeChatStream(
    message: string,
    handlers: { onDelta: (text: string) => void; onNodes?: (nodes: CodeHit[]) => void },
    projectId?: string,
    retry = true,
  ): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const res = await fetch("/api/agent/code/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ message, project_id: projectId }),
    });
    if (res.status === 401 && retry && (await refresh())) {
      return this.codeChatStream(message, handlers, projectId, false);
    }
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = frame.match(/^event: (.*)$/m)?.[1];
        const data = frame.match(/^data: ([\s\S]*)$/m)?.[1];
        if (!event || data === undefined) continue;
        if (event === "delta") handlers.onDelta(JSON.parse(data).text);
        else if (event === "nodes") handlers.onNodes?.(JSON.parse(data));
      }
    }
  },
};

export type { Status };
