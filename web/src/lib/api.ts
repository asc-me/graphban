import type { Credential, CredentialIn, FleetOverview, FleetPresence, OrgOverview, ReindexStatus, ScopeDefaults, ShellCounts } from "@/lib/types";
/**
 * Typed fetch client. Access token is kept in memory; the refresh token lives in
 * localStorage so a reload can silently re-auth. On a 401 the client attempts one
 * refresh, then retries the original request.
 */
import type {
  AdminActivity,
  CodeAnalysis,
  Deployment,
  Team,
  Galaxy,
  OrgProjectAccess,
  TriageRow,
  AdminInvite,
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

/**
 * There is deliberately no ambient project here (PRD-21 D1.1).
 *
 * A module-level active-project id synced from a `useEffect` was safe only while the
 * switcher was the sole thing that moved the project: a render always separated the
 * change from the user's next action. Putting the project in the URL destroys that —
 * the route changes synchronously on a deep link and on back/forward, while the effect
 * fires one render later, so a request issued in that window targets the *previous*
 * project. `github/connect` is the worst case: it wires an integration into the wrong
 * project's config, and for a user who belongs to two orgs the server has nothing to
 * reject, because they can read both.
 *
 * So every project-scoped write takes the id as its first argument and is uncallable
 * without one. The route is the only source; nothing caches it.
 */
function projectQuery(projectId: string): string {
  return `?project_id=${encodeURIComponent(projectId)}`;
}

/**
 * Merge the route's project into a write body, and refuse a disagreement.
 *
 * Overwriting a caller-supplied `project_id` silently would relocate the exact bug this
 * decision deletes — a wrong-project write, one layer down and harder to see. So a body
 * that names a different project throws in development and is corrected in production:
 * the route wins either way, but in development somebody finds out.
 */
function withProject<T extends object>(projectId: string, body: T) {
  const stated = (body as { project_id?: string }).project_id;
  if (import.meta.env.DEV && stated && stated !== projectId) {
    throw new Error(
      `project mismatch: body says ${stated}, route says ${projectId}. ` +
        "The route is the only source of the active project (PRD-21 D1.1).",
    );
  }
  return { ...body, project_id: projectId };
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
  // ---- forgotten password (GRPH-359 / GRPH-570) ----
  //
  // The request endpoint answers 202 with one sentence whether or not the address is
  // registered, DELIBERATELY — it must not be usable to discover who has an account here.
  // So this returns nothing: there is no answer to branch on, and a caller that tried would
  // be reintroducing the oracle the API refuses to be.
  async requestPasswordReset(email: string): Promise<void> {
    await request<{ detail: string }>(
      "/auth/password-reset",
      { method: "POST", body: JSON.stringify({ email }) },
      false,   // no refresh-retry: there is no session to refresh, that is why we are here
    );
  },
  async confirmPasswordReset(token: string, newPassword: string): Promise<User> {
    // Same reasoning as changePassword: the server revokes every session and hands back a
    // fresh pair, so storing it is what makes the user land SIGNED IN rather than at a login
    // form they have just proved they can get past.
    const data = await request<{ access_token: string; refresh_token: string }>(
      "/auth/password-reset/confirm",
      { method: "POST", body: JSON.stringify({ token, new_password: newPassword }) },
      false,   // no refresh-retry; a 400 here means the link is spent or expired
    );
    accessToken = data.access_token;
    setRefreshToken(data.refresh_token);
    return request<User>("/auth/me");
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
  // Presence on the graph (PRD-20 D4). Separate from `fleet` on purpose: the roster is a
  // page, this is polled by whichever graph view is open, and merging them would make every
  // Fleet render pay for node resolution it does not use.
  fleetPresence: (projectId?: string) =>
    request<FleetPresence>(`/fleet/presence${projectId ? `?project_id=${projectId}` : ""}`),
  mintFleetKey: (body: { project_id: string; role: string; wave: string; label?: string }) =>
    request<{ id: string; plaintext: string; role: string; wave: string; prefix: string }>(
      "/fleet/keys", { method: "POST", body: JSON.stringify(body) }),
  issueSeats: (body: { project_id: string; roles: string[]; wave?: string }) =>
    request<{ wave: string; seats: { id: string; role: string; code: string; expires_at: string }[] }>(
      "/fleet/seats", { method: "POST", body: JSON.stringify(body) }),
  revokeUnusedSeats: (projectId: string, wave?: string) =>
    request<{ revoked: number }>("/fleet/seats/revoke-unused",
      { method: "POST", body: JSON.stringify({ project_id: projectId, wave }) }),
  dismissAgent: (agentId: string, undo = false) =>
    request<{ id: string; dismissed: boolean }>(`/fleet/agents/${agentId}/dismiss`,
      { method: "POST", body: JSON.stringify({ undo }) }),
  revokeExpiredKeys: (projectId: string) =>
    request<{ revoked: number }>("/fleet/keys/revoke-expired",
      { method: "POST", body: JSON.stringify({ project_id: projectId }) }),
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
  deployments: (orgId: string) => request<Deployment[]>(`/orgs/${orgId}/deployments`),
  orgOverview: (orgId: string) => request<OrgOverview>(`/orgs/${orgId}/overview`),
  teams: (orgId: string) => request<Team[]>(`/orgs/${orgId}/teams`),
  createTeam: (orgId: string, name: string, description = "") =>
    request<Team>(`/orgs/${orgId}/teams`, {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),
  deleteTeam: (teamId: string) =>
    request<{ users: number; projects: number; memberships_kept: number }>(
      `/teams/${teamId}`,
      { method: "DELETE" },
    ),
  addTeamMember: (teamId: string, userId: string) =>
    request<Team>(`/teams/${teamId}/members/${userId}`, { method: "POST" }),
  removeTeamMember: (teamId: string, userId: string) =>
    request<Team>(`/teams/${teamId}/members/${userId}`, { method: "DELETE" }),
  setTeamGrant: (teamId: string, projectId: string, access: string) =>
    request<Team>(`/teams/${teamId}/grants/${projectId}`, {
      method: "PUT",
      body: JSON.stringify({ access }),
    }),
  /** Reports what survived — access held directly or via another team is recomputed. */
  revokeTeamGrant: (teamId: string, projectId: string) =>
    request<{ affected: number; kept_access: string[] }>(
      `/teams/${teamId}/grants/${projectId}`,
      { method: "DELETE" },
    ),
  orgGalaxy: (orgId: string) => request<Galaxy>(`/orgs/${orgId}/galaxy`),
  setMemberRole: (orgId: string, userId: string, role: string) =>
    request<OrgMember>(`/orgs/${orgId}/members/${userId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  /** Returns what was revoked, so the UI can say it rather than report bare success. */
  removeMember: (orgId: string, userId: string) =>
    request<{ removed_role: string; projects_revoked: string[] }>(
      `/orgs/${orgId}/members/${userId}`,
      { method: "DELETE" },
    ),
  setProjectAccess: (projectId: string, userId: string, access: string) =>
    request<OrgProjectAccess>(`/projects/${projectId}/members/${userId}`, {
      method: "PUT",
      body: JSON.stringify({ access }),
    }),
  orgBilling: (orgId: string) => request<Billing>(`/orgs/${orgId}/billing`),
  setOrgPlan: (orgId: string, plan: string) =>
    request<Org>(`/orgs/${orgId}/plan`, { method: "PUT", body: JSON.stringify({ plan }) }),
  requestAdditionalOrg: (body: { reason: string; company?: string }) =>
    request<OrgRequest>("/orgs/requests", { method: "POST", body: JSON.stringify(body) }),

  // ── Operator console (AL-94). Every call 404s unless the caller is a
  // platform admin on a hosted deployment, so the surface is invisible to tenants.
  adminWhoami: () =>
    request<{
      is_platform_admin: boolean;
      email: string;
      signup_mode: string;
      invite_expiry_days: number;
    }>("/admin/me"),
  adminOrgs: () => request<AdminOrg[]>("/admin/orgs"),
  adminUsers: () => request<AdminUser[]>("/admin/users"),
  /** `history` widens the read from outstanding invites to every one ever issued. */
  adminInvites: (history = false) =>
    request<AdminInvite[]>(`/admin/invites${history ? "?history=true" : ""}`),
  /** The operator ledger — actions taken from this plane, not tenant activity. */
  adminActivity: (limit = 12) => request<AdminActivity[]>(`/admin/activity?limit=${limit}`),
  adminCreateInvite: (body: { email: string; plan?: string | null }) =>
    request<AdminInvite>("/admin/invites", { method: "POST", body: JSON.stringify(body) }),
  adminRevokeInvite: (id: string) => request<void>(`/admin/invites/${id}`, { method: "DELETE" }),
  adminOrgRequests: () => request<OrgRequest[]>("/admin/org-requests"),
  adminDecideOrgRequest: (id: string, approve: boolean, note = "") =>
    request<OrgRequest>(`/admin/org-requests/${id}`, {
      method: "POST",
      body: JSON.stringify({ approve, note }),
    }),

  items: (projectId?: string) =>
    request<Item[]>(`/items${projectId ? `?project_id=${projectId}` : ""}`),
  // The shell's badge numbers. It used to fetch four whole collections to call `.length` on
  // them — 2.1 MB on every route to draw three badges and a stat (GRPH-431).
  counts: (projectId: string) => request<ShellCounts>(`/projects/${projectId}/counts`),
  createItem: (projectId: string, body: Partial<Item>) =>
    request<Item>("/items", {
      method: "POST",
      body: JSON.stringify(withProject(projectId, body)),
    }),
  updateItem: (id: string, body: Partial<Item>) =>
    request<Item>(`/items/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  reorderItems: (orderedIds: string[]) =>
    request<Item[]>("/items/reorder", {
      method: "PATCH",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),

  shards: (projectId?: string, limit?: number) =>
    request<Shard[]>(
      `/memory/shards${projectId ? `?project_id=${projectId}` : ""}${
        projectId && limit ? `&limit=${limit}` : ""
      }`,
    ),
  addShard: (projectId: string, body: { text: string; scope?: string; item_id?: string | null }) =>
    request<Shard>("/memory/shards", {
      method: "POST",
      body: JSON.stringify(withProject(projectId, body)),
    }),
  searchMemory: (projectId: string, query: string, top_k = 5) =>
    request<ShardHit[]>("/memory/search", {
      method: "POST",
      body: JSON.stringify({ query, top_k, project_id: projectId }),
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
  /** What is waiting to be triaged, each row carrying its closest duplicate. */
  triageQueue: (projectId: string) =>
    request<TriageRow[]>(`/requests/triage?project_id=${encodeURIComponent(projectId)}`),
  /** Turn a request into tracked work. One transaction — item and link land together. */
  acceptRequest: (id: string) =>
    request<{ request: RequestItem; item: Item }>(`/requests/${id}/accept`, { method: "POST" }),
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
    toolTiers?: string[],
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
        // Optional MCP tool tiers (GRPH-571). Omitted/empty → the core manifest. Never
        // widens what the key may CALL — only what its `tools/list` advertises.
        ...(toolTiers?.length ? { tool_tiers: toolTiers } : {}),
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
  createPrd: (projectId: string, title: string, template = "standard", body?: string) =>
    request<Prd>("/prds", {
      method: "POST",
      body: JSON.stringify({ title, template, project_id: projectId, body }),
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

  platform: (projectId: string) => request<PlatformConfig>(`/platform${projectQuery(projectId)}`),
  updatePlatform: (projectId: string, body: Partial<PlatformConfig>) =>
    request<PlatformConfig>(`/platform${projectQuery(projectId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  aiProviders: () => request<{ providers: AiProvider[] }>("/platform/providers"),

  // ---- Credentials (PRD-25). Deployment-scoped: the project id in the query resolves the
  // SCOPE server-side (org under hosted multi-tenancy, the deployment otherwise). It is not a
  // filter — every credential the caller's scope owns comes back, which is the whole point of
  // the view that replaced the per-project provider table.
  credentials: (projectId: string) =>
    request<{ credentials: Credential[] }>(`/platform/credentials${projectQuery(projectId)}`),
  createCredential: (projectId: string, body: CredentialIn) =>
    request<{ id: string; state: string }>(`/platform/credentials${projectQuery(projectId)}`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateCredential: (projectId: string, id: string, body: Partial<CredentialIn>) =>
    request<{ id: string; state: string }>(`/platform/credentials/${id}${projectQuery(projectId)}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  deleteCredential: (projectId: string, id: string) =>
    request<void>(`/platform/credentials/${id}${projectQuery(projectId)}`, { method: "DELETE" }),
  retryCredential: (projectId: string, id: string) =>
    request<{ id: string; state: string; last_error: string; validation_attempts: number }>(
      `/platform/credentials/${id}/retry${projectQuery(projectId)}`, { method: "POST" }),
  setScopeDefaults: (projectId: string, body: ScopeDefaults) =>
    request<ScopeDefaults & { scope: string }>(`/platform/credentials/defaults${projectQuery(projectId)}`, {
      method: "PUT", body: JSON.stringify(body),
    }),
  setProjectCredential: (projectId: string, body: { credential_id?: string | null; model_override?: string }) =>
    request<{ project_id: string; credential_id: string | null; model_override: string }>(
      `/platform/credentials/project${projectQuery(projectId)}`, {
        method: "PUT", body: JSON.stringify(body),
      }),
  startReindex: (projectId: string) =>
    request<{ started: { table: string; total: number }[] }>(`/platform/reindex${projectQuery(projectId)}`, {
      method: "POST",
    }),
  reindexStatus: (projectId: string) =>
    request<ReindexStatus>(`/platform/reindex${projectQuery(projectId)}`),
  saveProviders: (projectId: string, body: { active_chat_provider?: string; providers?: Record<string, ProviderConfigUpdate> }) =>
    request<PlatformConfig>(`/platform${projectQuery(projectId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  githubConnect: (projectId: string, account: string, repo: string) =>
    request<PlatformConfig>(`/platform/github/connect${projectQuery(projectId)}`, { method: "POST", body: JSON.stringify({ account, repo }) }),
  githubDisconnect: (projectId: string) =>
    request<PlatformConfig>(`/platform/github/disconnect${projectQuery(projectId)}`, { method: "POST" }),
  gdriveConnect: (projectId: string, account: string, folder: string) =>
    request<PlatformConfig>(`/platform/gdrive/connect${projectQuery(projectId)}`, { method: "POST", body: JSON.stringify({ account, folder }) }),
  gdriveDisconnect: (projectId: string) =>
    request<PlatformConfig>(`/platform/gdrive/disconnect${projectQuery(projectId)}`, { method: "POST" }),
  gdriveSync: (projectId: string) =>
    request<{
      folder: string;
      prds_dir: string;
      exported: string[];
      imported: string[];
      updated_db: string[];
      updated_file: string[];
      conflicts: string[];
      in_sync: number;
    }>(`/platform/gdrive/sync${projectQuery(projectId)}`, { method: "POST" }),

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
  /**
   * Hubs, components and (given `a` and `b`) a shortest path — one read, because the service
   * was written to answer all three for this view. `edgeTypes` scopes every answer to the
   * edge kinds currently shown, so the panel cannot contradict the canvas beside it.
   */
  codeAnalysis: (opts: {
    projectId?: string;
    edgeTypes?: string[];
    limit?: number;
    a?: string;
    b?: string;
  } = {}) => {
    const q = new URLSearchParams();
    if (opts.projectId) q.set("project_id", opts.projectId);
    // Omitted, never sent empty: `edge_types=` would ask for nothing and read as everything.
    if (opts.edgeTypes?.length) q.set("edge_types", opts.edgeTypes.join(","));
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.a) q.set("a", opts.a);
    if (opts.b) q.set("b", opts.b);
    return request<CodeAnalysis>(`/agent/code/analysis?${q.toString()}`);
  },

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
