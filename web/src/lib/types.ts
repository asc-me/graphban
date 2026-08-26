export type Status = "backlog" | "next" | "in_progress" | "review" | "done" | "blocked";
export type Fidelity = "low" | "high";
export type RequestType = "bug" | "feature" | "enhancement" | "feedback";

export interface User {
  id: string;
  name: string;
  handle: string;
  email: string;
  avatar: string;
  initials: string;
}

export interface Project {
  id: string;
  name: string;
  /** Short key prefix: item/request/PRD keys render as <TAG>-<n> / -R<n> / -P<n> (PRD-13). */
  tag: string;
  accent: string;
  visibility: string;
  description: string;
  share_global_memory: boolean;
  auto_extract: boolean;
  mcp_enabled: boolean;
  embed_model: string;
  // Memory auto-triage toggles (AL-227).
  memory_auto_reject: boolean;
  memory_write_mode: string;
  memory_llm_judge: boolean;
  agent_adjudication: boolean;
  /** Danger mode: an agent may sign off its own work, but only when no independent agent exists. */
  allow_self_review: boolean;
}

// ── Deploy config + Organizations (hosted-only, AL-74b) ────────────────────
export type SignupMode = "open" | "invite_only" | "closed";

export interface AppConfig {
  hosted_mode: boolean;
  signup_mode: SignupMode;
}

export type OrgRole = "owner" | "admin" | "member";

export interface Org {
  id: string;
  name: string;
  plan: string;
  role: OrgRole;
}

export interface OrgProjectAccess {
  project_id: string;
  tag: string;
  name: string;
  level: "write" | "read";
}

export interface OrgMember {
  user: User;
  role: OrgRole;
  /** Projects in this org they can reach. Empty is "reaches nothing", never "unknown". */
  access: OrgProjectAccess[];
  /** Newest recorded write, or null for "no write on record" — not inactivity. */
  last_write_at: string | null;
}

// ── Linked deployments (PRD-21 D6) ─────────────────────────────────────────
export interface Deployment {
  /** The sync credential's name — the cloud stores no other deployment identity. */
  label: string;
  credential_id: string;
  prefix: string;
  project_id: string;
  project_tag: string;
  project_name: string;
  /** Self-reported on the push. A hint: the same box answers differently per network. */
  base_url: string;
  last_push_at: string | null;
  node_count: number;
  /** `never` is a link nobody finished; `stale` is a box that stopped. Not the same. */
  freshness: "in_sync" | "stale" | "never";
  revoked: boolean;
  agents: { key: string; label: string; role: string; state: string }[];
}

// ── Teams (PRD-21 D5) ──────────────────────────────────────────────────────
export interface TeamGrant {
  project_id: string;
  tag: string;
  name: string;
  access: "write" | "read";
  /** Who this grant actually provides access to. */
  derived_user_ids: string[];
  /** Members who already had it directly — revoking the grant will NOT remove theirs. */
  direct_user_ids: string[];
}

export interface Team {
  id: string;
  org_id: string;
  name: string;
  description: string;
  members: User[];
  grants: TeamGrant[];
}

export interface Invite {
  id: string;
  /** "org" seats the invitee in an existing org; "platform" onboards a new tenant. */
  kind: "org" | "platform";
  /** null for a platform invite — the org is founded on accept. */
  org_id: string | null;
  email: string;
  role: string;
  /** Platform invites only: plan pre-assigned to the org they found. */
  plan: string | null;
  status: string;
  created_at: string;
  expires_at: string | null;
  accept_url: string;
}

export interface InvitePreview {
  /** "org" seats you in an existing org; "platform" onboards a brand-new tenant. */
  kind: "org" | "platform";
  org_name: string;
  email: string;
  role: string;
  invited_by: string;
}

export interface PlanLimits {
  max_projects: number;
  max_seats: number;
  max_shards: number;
  max_calls_per_month: number;
}

export interface Usage {
  projects: number;
  seats: number;
  shards: number;
  calls_this_month: number;
}

export interface Billing {
  plan: string;
  limits: PlanLimits;
  usage: Usage;
}

// ── Operator console (hosted + platform-admin only, AL-94) ────────────────
// Metadata only by design — no tenant content ever crosses this boundary.
export interface AdminOrgMember {
  handle: string;
  name: string;
  role: string;
  joined_at: string | null;
}

export interface AdminOrg {
  id: string;
  name: string;
  plan: string;
  created_at: string | null;
  owner_email: string | null;
  owner_name: string;
  owner_handle: string;
  usage: Usage;
  limits: PlanLimits;
  /** Everyone seated in the tenant, owner first. Listed, never actionable. */
  members: AdminOrgMember[];
}

export interface AdminUserOrg {
  id: string;
  name: string;
  role: string;
}

export interface AdminUser {
  id: string;
  name: string;
  handle: string;
  email: string;
  created_at: string | null;
  org_count: number;
  orgs: AdminUserOrg[];
  /**
   * Newest recorded write, or null for "nothing on record". Not last *activity*:
   * reads are never evented, so a browsing user and an absent one look identical
   * here — which is why null renders as its own state rather than as a dash.
   */
  last_write_at: string | null;
}

/** A platform invite with the provenance the Licensing table is built on. */
export interface AdminInvite extends Invite {
  invited_by_handle: string;
  /** Empty until the account this invite seeded has actually founded an org. */
  redeemed_org_id: string;
  redeemed_org_name: string;
  /** Past its expiry while still pending — evaluated on read, not by a sweeper. */
  expired: boolean;
}

/** One row of the operator ledger: an action taken from this plane, by an operator. */
export interface AdminActivity {
  ts: string;
  action: string;
  actor_label: string;
  target_type: string;
  target_id: string;
  meta: Record<string, unknown> | null;
}

export interface OrgRequest {
  id: string;
  user_id: string;
  reason: string;
  company: string;
  status: "pending" | "approved" | "denied";
  consumed: boolean;
  created_at: string;
  decided_at: string | null;
  decision_note: string;
}

export interface Reporter {
  name?: string;
  handle?: string;
  avatar?: string;
}

export interface PR {
  number: number;
  title: string;
  branch: string;
  state: string;
  additions: number;
  deletions: number;
  checks: string;
  ago: string;
}

export type EvidenceKind = "test" | "url" | "screenshot" | "health" | "note";

export interface EvidenceReceipt {
  kind: EvidenceKind;
  detail: string;
  url: string;
}

export interface Item {
  id: string;
  project_id: string;
  title: string;
  description: string;
  status: Status;
  tags: string[];
  touchpoints: string[];
  effort: number;
  sort_order: number;
  blocker: string;
  /** Why a reviewer sent it back. Survives the pin — the author reads it after reclaiming. */
  bounce_reason: string;
  date: string;
  reporter: Reporter;
  pr: PR | null;
  github_url: string;
  evidence: EvidenceReceipt[];
  assignee: string;
  claimed_by: string | null;
  prd_id: string | null;
  prd_section: string;
  fidelity: Fidelity;
  created_at: string;
  updated_at: string;
}

export type ShardStatus = "candidate" | "published" | "rejected";

export interface Shard {
  id: string;
  text: string;
  scope: string;
  source: string;
  status: ShardStatus;
  origin: string;
  item_id: string | null;
  project_id: string | null;
  fresh: boolean;
  /** AL-227: "" = human-only; else the signal that auto-acted ("similarity" | "llm"). */
  scoring_source: string;
  auto_confidence: number | null;
  created_at: string;
}

export interface GrillMessage {
  role: "user" | "agent";
  text: string;
}

export interface ShardCluster {
  size: number;
  representative: Shard;
  members: Shard[];
}

export interface ShardHit {
  shard: Shard;
  score: number;
}

// ── In-app AI assistant (AL-175) ───────────────────────────────────────────
export interface AssistantThread {
  id: string;
  project_id: string;
  entity_type: "item" | "prd";
  entity_id: string;
  provider: string;
  model: string;
  title: string;
}

export interface ProposedAction {
  id: string;
  tool: string;
  summary: string;
  status: "pending" | "applied" | "rejected" | "reverted";
}

export interface AssistantMessage {
  id: string;
  seq: number;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls: { id: string; name: string; input: Record<string, unknown> }[];
  tool_results: { id: string; content: string; is_error: boolean }[];
  proposed_actions: ProposedAction[];
}

export interface AssistantThreadDetail extends AssistantThread {
  messages: AssistantMessage[];
}

export interface AssistantProvider {
  id: string;
  label: string;
  kind: string;
  chat_model: string;
  models: string[];
  configured: boolean;
  active: boolean;
}

export type ReviewSuggestion = "accept" | "reject" | "review";

export interface ScoredCandidate {
  shard: Shard;
  suggestion: ReviewSuggestion;
  confidence: number;
  reasons: string[];
  duplicate_of: string | null;
}

export interface RequestItem {
  id: string;
  project_id: string;
  type: RequestType;
  title: string;
  detail: string;
  by: string;
  votes: number;
  status: string;
  linked_to: string | null;
  ago: string;
  source_url: string;
  meta: Record<string, unknown>;
  attachment_ids: string[];
  created_at: string;
}

/** A request or item that looks like this one. Advisory — never auto-merged. */
export interface DuplicateHint {
  kind: "request" | "item";
  id: string;
  title: string;
  score: number;
}

export interface TriageRow {
  request: RequestItem;
  /** Null means "compared, nothing matched" — the comparison always runs. */
  duplicate: DuplicateHint | null;
}

// ── The super galaxy (PRD-21 D3) ──────────────────────────────────────────
/** The file, and the fact found in it, that proves an edge between two repos. */
export interface EdgeEvidence {
  file: string;
  fact: string;
}

export interface GalaxyNode {
  id: string;
  tag: string;
  name: string;
  accent: string;
  /** Package names this project publishes — the registry siblings resolve against. */
  provides: string[];
  node_count: number;
  /** False = no deployment has pushed a graph yet. Distinct from "has no structure". */
  pushed: boolean;
}

export interface GalaxyEdge {
  id: string;
  src: string;
  dst: string;
  kind: "depends_on" | "serves" | "declared";
  resolved_name: string;
  /** Never empty — an edge that cannot name its proof is refused server-side. */
  evidence: EdgeEvidence[];
  weight: number;
  /** False = the evidence is no longer declared. Faint, never removed. */
  fresh: boolean;
  reason: string;
  updated_at: string | null;
}

export interface Galaxy {
  nodes: GalaxyNode[];
  edges: GalaxyEdge[];
  /** Names two projects both claim. These draw no edge, and saying so is the point. */
  collisions: { name: string; project_ids: string[] }[];
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  project_id: string | null;
  last_used: string | null;
  created_at: string;
  expires_at: string | null;
  revoked: boolean;
}

export interface ApiKeyCreated extends ApiKey {
  plaintext: string;
}

export interface ChatResponse {
  reply: string;
  shards: ShardHit[];
}

export type LlmMode = "stub" | "local" | "cloud";

export interface PlatformConfig {
  project_id: string;
  llm_mode: LlmMode;
  local_base_url: string;
  local_model: string;
  cloud_provider: string;
  cloud_model: string;
  github_connected: boolean;
  github_account: string;
  github_repo: string;
  github_scope: string;
  gdrive_connected: boolean;
  gdrive_account: string;
  gdrive_folder: string;
  rate_limit_per_min: number;
  turnstile_sitekey: string;
  turnstile_secret_set: boolean;
  active_chat_provider: string;
  /** Provider actually serving chat right now (live). "stub" → no real AI is in effect. */
  effective_chat_provider: string;
  provider_config: Record<string, ProviderConfigView>;
  public_share_enabled: boolean;
  share_token: string | null;
  /** AL-137 D8: whether this project's code graph pushes to the linked cloud. */
  sync_graph: boolean;
}

export type SyncProjectStatus = "live" | "stale" | "paused" | "unsynced" | "empty";

export interface SyncProjectState {
  project_id: string;
  name: string;
  /** Whether the current user may push/purge this project (drives control enablement). */
  writable: boolean;
  sync_graph: boolean;
  total_nodes: number;
  synced_nodes: number;
  pending: number;
  last_synced_at: string | null;
  status: SyncProjectStatus;
}

export interface SyncBundle {
  bundle_version: number;
  project_id: string;
  nodes: unknown[];
  edges: unknown[];
  node_count: number;
  edge_count: number;
}

export interface SyncStatus {
  linked: boolean;
  /** "web" = DB link set from this page · "env" = baked-in SYNC_CLOUD_URL · "" = unlinked. */
  source: "web" | "env" | "";
  cloud_url: string;
  org: string;
  credential_set: boolean;
  linked_at: string | null;
  projects: SyncProjectState[];
}

export type ProviderKind = "stub" | "anthropic" | "openai" | "ollama";

export interface AiProvider {
  id: string;
  label: string;
  kind: ProviderKind;
  embeds: boolean;
  base_url: string;
  chat_model: string;
  embed_model: string;
  auth: boolean;
}

export interface ProviderConfigView {
  base_url: string;
  chat_model: string;
  embed_model: string;
  key_set: boolean;
}

export interface ProviderConfigUpdate {
  api_key?: string;
  base_url?: string;
  chat_model?: string;
  embed_model?: string;
}

export interface Member {
  user: User;
  role: string;
  access: string;
}

// Mirrors `STATUSES` in backend/app/services/prds.py. `closed` was missing here for as
// long as it existed there, and `Record<PrdStatus, …>` was exhaustive over the WRONG
// union — so the compiler had nothing to say while the PRD page crashed on real data
// (GRPH-458). `test_prd_status_union` fails if the two drift again.
export type PrdStatus = "draft" | "review" | "approved" | "closed";

/** The completion standard (PRD-15). `approved` is REACHED when every dimension is
 *  resolved or deferred — it is not a status anyone picks. */
export type GrillOutcome = "resolved" | "deferred" | "unanswered";

export interface GrillDimensionState {
  outcome: GrillOutcome;
  note: string;
  turn_seq: number | null;
  /** A provider id, `stub` (answers counted, substance NOT assessed), or `author`. */
  graded_by: string;
  question: string;
}

export interface IntentDiffLine { op: "+" | "-" | "="; text: string }

export interface IntentDiffSection {
  title: string;
  state: "unchanged" | "modified" | "renamed" | "added" | "removed";
  was?: string;
  lines?: IntentDiffLine[];
}

export interface IntentDiff {
  governed: boolean;
  baseline_version: string | null;
  pending: { reason_type: string; reason: string; requested_by: string } | null;
  sections: IntentDiffSection[];
  changed?: number;
}

export interface GrillState {
  prd_id: string;
  turns: { seq: number; role: string; text: string; via: string; actor: string }[];
  questions: number;
  answers: number;
  grilled: boolean;
  dimensions: Record<string, GrillDimensionState>;
  outstanding: string[];
  deferred: string[];
  complete: boolean;
}

export interface PrdSummary {
  id: string;
  title: string;
  status: PrdStatus;
  version: string;
  linked: string[];
  updated: string;
}

export interface Prd extends PrdSummary {
  project_id: string;
  body: string;
  created_at: string;
  updated_at: string;
}

export interface PrdCoverageSection {
  section: string;
  /** False for framing prose (Problem, Goals, Non-goals, …) — never counted as a gap. */
  implementable: boolean;
  item_count: number;
  done: number;
  by_status: Record<string, number>;
  gap: boolean;
  high_fidelity: number;
  open_high_fidelity: number;
  item_ids: string[];
}

export interface PrdCoverage {
  prd_id: string;
  title: string;
  status: PrdStatus;
  sections: PrdCoverageSection[];
  section_count: number;
  /** Buildable sections — the denominator coverage is measured against. */
  implementable_sections: number;
  sections_with_tasks: number;
  gaps: string[];
  total_items: number;
  done_items: number;
  percent_done: number;
  open_high_fidelity: number;
}

export interface PrdVersion {
  id: number;
  version: string;
  date: string;
  note: string;
  body: string;
  created_at: string;
}

export interface RoadmapPhase {
  key: string;
  name: string;
  window: string;
  color: string;
  done: number;
  total: number;
  milestones: { title: string; tag: string; done: boolean }[];
}

export type LinkType = "dependency" | "code" | "semantic" | "tag";

export interface GraphLink {
  id: number;
  a: string;
  b: string;
  type: LinkType;
  confidence: number;
  reason: string;
}

// ── Code structure graph ──────────────────────────────────────────────────
export type CodeKind = "module" | "file" | "symbol";
export type CodeEdgeType = "imports" | "calls" | "owns" | "tested_by" | "references";

export interface CodeNode {
  id: string;
  path: string;
  kind: string;
  name: string;
  lang: string;
  summary: string;
  fresh: boolean;
}

export interface CodeEdge {
  src: string;
  dst: string;
  type: CodeEdgeType;
}

export interface CodeHit {
  node: CodeNode;
  score: number;
}

export interface CodeAnswer {
  reply: string;
  nodes: CodeHit[];
}

/** A dependency that leaves this repo (PRD-21 D4). */
export interface ProjectStub {
  edge_id: string;
  project_id: string;
  tag: string;
  name: string;
  accent: string;
  kind: string;
  resolved_name: string;
  fresh: boolean;
  evidence: { file: string; fact: string }[];
  /** Paths in THIS project's graph that declare it — where the arrow attaches. */
  anchor_paths: string[];
  /** The declaring file is named by the manifest but not described here. */
  unanchored: boolean;
}

export interface CodeMap {
  nodes: CodeNode[];
  edges: CodeEdge[];
  node_count: number;
  edge_count: number;
  /** Empty outside an org — no siblings to depend on, which differs from having none. */
  outbound: ProjectStub[];
}

export interface CodeLinkedItem {
  id: string;
  title: string;
  status: string;
  relation: string;
}

export interface CodeLinkedRequest {
  id: string;
  title: string;
  type: string;
  status: string;
  relation: string;
}

export interface CodeNeighbors {
  path: string;
  node: CodeNode | null;
  outgoing: { dst: string; type: CodeEdgeType }[];
  incoming: { src: string; type: CodeEdgeType }[];
  items_touching: { id: string; title: string; status: string }[];
  linked_items: CodeLinkedItem[];
  linked_requests: CodeLinkedRequest[];
}

/**
 * One hub row. Both directions are returned, and both are shown — PRD-20 AC-18 turns on the
 * distinction between them, and a single "degree" number hides it. `backend/app/mcp_server.py`
 * is inbound 5 / outbound 18 on the live graph: it imports the most and is not a hub.
 */
export interface CodeHub {
  path: string;
  inbound: number;
  outbound: number;
  kind: string;
  described: boolean;
}

export interface CodeComponent {
  anchor: string;
  size: number;
  members: string[];
}

/** One step of a shortest path. `forward` is whether the edge points the way you walked it. */
export interface CodePathHop {
  src: string;
  dst: string;
  type: CodeEdgeType;
  forward: boolean;
}

export interface CodePath {
  a: string;
  b: string;
  found: boolean;
  /** Endpoints the graph has never heard of, so "not found" and "not described" stay apart. */
  missing: string[];
  hops: CodePathHop[];
}

export interface CodeAnalysis {
  hubs: CodeHub[];
  components: CodeComponent[];
  path: CodePath | null;
}

export type CodeRelation = "affects" | "implements" | "fixes" | "tests" | "references";

export interface CodeRef {
  id: number;
  ref_type: string;
  ref_id: string;
  path: string;
  relation: string;
}

export interface CodeForRefRow {
  path: string;
  relation: string;
  node: CodeNode | null;
}

export interface McpToolInfo {
  name: string;
  description: string;
  params: string[];
  calls: number;
  status: string;
}

export interface Event {
  id: number;
  ts: string | null;
  actor_type: "user" | "apikey" | "system";
  actor_id: string;
  actor_label: string;
  /** The human on whose behalf the action ran (AL-197). */
  principal?: string;
  /** The agent that performed it (API key name or "assistant:<provider>"), if any. */
  agent?: string;
  surface: "mcp" | "rest" | "public";
  action: string;
  target_type: string;
  target_id: string;
  project_id: string | null;
  meta: Record<string, unknown> | null;
}

export interface EventPage {
  results: Event[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface DashboardData {
  items_total: number;
  items_by_status: Record<Status, number>;
  effort_total: number;
  done_count: number;
  in_progress_count: number;
  blocked_count: number;
  requests_total: number;
  requests_by_type: Record<string, number>;
  requests_by_status: Record<string, number>;
  shard_count: number;
  prd_count: number;
  mcp_calls: number;
  recent_items: { id: string; title: string; status: Status; date: string }[];
}

export interface DuplicateHit {
  kind: "item" | "request";
  id: string;
  title: string;
  score: number;
  type?: string | null;
  status?: string | null;
}

export interface PublicSubmitResponse {
  request: RequestItem;
  duplicates: DuplicateHit[];
}

// ---- Delivery acceptance (PRD-12) ------------------------------------------------------
// Shapes are mirrored from the service, not re-derived: `close_report` is the one surface
// that reads the ORIGINAL baseline rather than the governing one, which is what lets a
// reviewer see intent that a rebaseline removed.
export interface CloseReportSection {
  section: string;
  current_title: string;
  introduced_at: string;
  dropped_at: string | null;
  framing: boolean;
  fate: "delivered" | "undelivered" | "dropped" | "framing";
  delivered_items: string[];
  planned_items: string[];
  history: { version: string; change: string; from?: string; to?: string }[];
  disposition: { section: string; disposition: string; target: string | null; reason: string } | null;
}

export interface CloseReport {
  governed: boolean;
  original_version: string | null;
  governing_version?: string;
  chain?: { version: string; reason_type: string; reason: string }[];
  sections: CloseReportSection[];
  dropped: string[];
  never_delivered?: string[];
  expanded_scope: string[];
  added_after_approval: { id: string; status: string; section: string; inferred: boolean }[];
  drift?: { accumulated: number; current: number; total: number };
  closed: {
    closed_at: string; closed_by: string; mode: string; baseline_version: string;
    verdict: string; disclosure: string | null;
    dispositions: { section: string; disposition: string; target: string | null; reason: string }[];
  } | null;
}

export interface EvidenceRollup {
  governed: boolean;
  baseline_version: string | null;
  sections: {
    section: string;
    delivered_items: string[];
    receipts: Record<string, number>;
    falsifiable: number;
    unfalsifiable: number;
    corroboration: "corroborated" | "partial" | "unknown";
  }[];
  unsupported: string[];
  uncorroborated: string[];
}

export interface AuditCoverage {
  governed: boolean;
  covered: string[];
  uncovered: string[];
  complete?: boolean;
}


/** PRD-17 D5. `state` is DERIVED server-side — an agent that died never reported it. */
export interface FleetAgent {
  id: string;
  key: string;
  label: string;
  active_role: string;
  state: string;
  capabilities: Record<string, unknown>;
  /** Display prefix of the credential this agent authenticated with (`gb_sk_ab12`) — never
   *  the plaintext, which is not stored. Null when the key row is gone. */
  credential: string | null;
  /** `single` when the credential was minted all-in-one and a role hint cannot narrow it. */
  credential_posture: string | null;
  /** The seat this agent redeemed at registration. Null means un-enrolled — the single-agent
   *  posture, which is safe but is NOT a fleet, and a forgotten code looks identical to a
   *  deliberate one unless the roster says so. */
  enrolment_id: string | null;
  /** Holds a seat — part of a fleet. False is the single-agent posture: legitimate, but
   *  grouped apart because it answers a different question. */
  enrolled: boolean;
  /** Hidden from the roster by a human. The row always survives — durable work references
   *  this id as a plain string. */
  dismissed: boolean;
  worktree: string;
  branch: string;
  branch_orphaned: boolean;
  last_seen_at: string | null;
  /** `phase` is DERIVED server-side from signals every vendor already writes — no adapter
   *  reports it (GRPH-522). `stale` and `unknown` are admissions, not activities: render
   *  them as such, never as an idle or healthy row. */
  holdings: { id: string; title: string; status: string;
              phase: string; phase_basis: string; bounced: boolean }[];
}

/** One live reservation, resolved to the agent and the human behind it (PRD-20 D4). */
export interface HeldArea {
  area: string;
  item_id: string | null;
  expires_at: string | null;
  agent_id: string | null;
  agent_label: string;
  active_role: string | null;
  state: string;
  user_id: string | null;
  user_initials: string;
  /** A hex colour — `User.avatar`, the same one the person's avatar wears everywhere else. */
  user_color: string | null;
  /** Present on `held`: which nodes the area resolved to. Plural — one area covers many. */
  node_paths?: string[];
  /** True when the area came from `predict_areas` rather than declared touchpoints. */
  predicted?: boolean;
  /** Present on `off_map`: why it could not be placed. */
  reason?: "undescribed" | "stale";
}

export interface FleetPresence {
  served_at: string;
  heartbeat_interval_seconds: number;
  held: HeldArea[];
  /** Held work the graph cannot place. Reported, never dropped — see PRD-20 G8. */
  off_map: HeldArea[];
  truncated: boolean;
  total: number;
}

export interface FleetOverview {
  agents: FleetAgent[];
  online: number;
  total: number;
  /** Live agents per role, plus `all-in-one` — the unspecialised single-dev posture. */
  by_role: Record<string, number>;
  /** `single-agent` when nobody has specialised; `fleet` once any role is held. */
  posture: string;
  roles: string[];
  presence_ttl_seconds: number;
  heartbeat_interval_seconds: number;
  review_queue: {
    id: string; key: string; title: string; branch: string;
    built_by: string | null; built_by_label: string | null; reviewed_by: string | null;
  }[];
  clusters: {
    items: string[]; areas: string[]; predicted: boolean;
    held_by: string | null; blocked_on: string | null;
  }[];
  /** Seats for this project. No part of the code ever comes back — a seat is named by role
   *  and wave, and lives thirty minutes. */
  seats: FleetSeat[];
  /** Credentials that reach this project. Display prefix only, never key material. */
  credentials: FleetCredential[];
  /** Waves that still own an un-revoked seat or key — newest first. History is not offered:
   *  ending a wave that owns nothing is noise on a destructive control. */
  waves: string[];
}

export interface FleetCredential {
  id: string;
  name: string;
  /** Display fragment, e.g. `gb_sk_ab12`. */
  prefix: string;
  /** Set when this key was minted for a wave — a wave artifact rather than somebody's
   *  long-lived credential. End wave sweeps these and only these. */
  wave: string | null;
  revoked: boolean;
  posture: string | null;
  roles: string[];
  agents: number;
  expires_at: string | null;
}

export interface FleetSeat {
  id: string;
  role: string;
  wave: string | null;
  /** Derived on read, never swept: `unused` | `consumed` | `expired` | `revoked`. */
  state: string;
  consumed_by: string | null;
  reissued_from: string | null;
  expires_at: string | null;
}

/** PRD-21 D2 — the org's first cross-project aggregate. */
export interface OrgOverviewProject {
  id: string;
  tag: string;
  name: string;
  accent: string;
  items: Record<string, number>;
  open_items: number;
  claims: { item_id: string; title: string; agent: string | null; claimed_at: string | null }[];
  nodes: number;
  last_push_at: string | null;
  /** `never` is a link nobody finished, not a box that stopped. Different words on purpose. */
  sync: "live" | "never";
}

export interface OrgOverview {
  org_id: string;
  plan: string | null;
  projects: OrgOverviewProject[];
  totals: {
    projects: number;
    open_items: number;
    claims: number;
    nodes: number;
    never_synced: number;
  };
  usage: Record<string, number>;
  limits: Record<string, number>;
}

/** The integers the app shell renders as badges. See `api.counts` (GRPH-431). */
export type ShellCounts = {
  items: number;
  items_in_progress: number;
  requests: number;
  review: number;
};
