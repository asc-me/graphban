from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator

# Imports nothing from `app`, so this cannot cycle back through schemas.
from app.services import tool_tiers as tool_tiers_svc


class ORMModel(BaseModel):
    # populate_by_name so the `id` fields aliased to a rendered `key` (PRD-13) can still
    # be constructed by field name in tests and hand-built payloads.
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# A user-visible key is RENDERED from the project's current tag + the entity's number;
# the stored id is frozen and internal. These aliases are what keep a stored id out of
# every response — including reference fields like `prd_id`, which would otherwise leak
# an old tag through the back door after a retag.
def _key(alias: str, **kw):
    return Field(validation_alias=alias, **kw)


# ---- Auth ----
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class PasswordChangeIn(BaseModel):
    current_password: str
    # Same floor as RegisterIn, spelled the same way rather than read from
    # `settings.min_password_length` — that setting is currently unused, and having one
    # path honour it while the other hardcodes 8 is how the two quietly diverge.
    new_password: str = Field(min_length=8, max_length=200)


class PasswordResetRequestIn(BaseModel):
    email: str


class PasswordResetConfirmIn(BaseModel):
    token: str
    # Same floor as RegisterIn and PasswordChangeIn, spelled the same way for the reason
    # given there: one path honouring `settings.min_password_length` while the others
    # hardcode 8 is how the three quietly diverge.
    new_password: str = Field(min_length=8, max_length=200)


class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    handle: str
    password: str = Field(min_length=8, max_length=200)
    # Hosted onboarding (AL-74b): a valid org-invite token lets a user sign up even
    # when open self-serve registration is closed, and auto-joins them to the org.
    invite_token: str | None = None

    @field_validator("password")
    @classmethod
    def _password_not_trivial(cls, v: str) -> str:
        # Cheap floor against the worst passwords; length is the main lever (AL-72).
        if v.strip() != v or len(set(v)) < 4:
            raise ValueError("password is too weak")
        return v


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(ORMModel):
    id: str
    name: str
    handle: str
    email: EmailStr
    avatar: str
    initials: str


# ---- Organizations (hosted-only, AL-74b) ----
class OrgCreate(BaseModel):
    name: str


class OrgOut(BaseModel):
    id: str
    name: str
    plan: str
    role: str  # the requesting user's role in this org


class OrgProjectAccessOut(BaseModel):
    """One project this member can reach, and at what level."""

    project_id: str
    tag: str = ""
    name: str = ""
    level: str  # write | read


class OrgMemberOut(BaseModel):
    user: UserOut
    role: str
    # Which projects they can reach. Empty means no project access at all — a real and
    # distinct state from "we did not look", which is why it is a list and never null.
    access: list[OrgProjectAccessOut] = []
    # Newest recorded write. NULL is "no write on record", NOT inactivity: reads leave no
    # ledger row, so a member who only browses is indistinguishable from one who left.
    last_write_at: datetime | None = None


# ---- Teams (hosted-only, PRD-21 D5) ----
class TeamCreate(BaseModel):
    name: str
    description: str = ""


class TeamGrantIn(BaseModel):
    access: str  # write | read


class TeamGrantOut(BaseModel):
    project_id: str
    tag: str = ""
    name: str = ""
    access: str
    # Who this grant currently reaches, split by whether it is what provides the access.
    # A `direct` member keeps theirs when the grant is revoked, and saying so is the
    # difference between a revoke that does what an admin expects and one that does less.
    derived_user_ids: list[str] = []
    direct_user_ids: list[str] = []


class TeamOut(BaseModel):
    id: str
    org_id: str
    name: str
    description: str = ""
    members: list[UserOut] = []
    grants: list[TeamGrantOut] = []


class GrantRevokedOut(BaseModel):
    """What survived a revoke. "Revoked" and "everyone lost access" are different facts."""

    affected: int
    kept_access: list[str] = []


class MemberRoleIn(BaseModel):
    role: str  # admin | member — owner is never grantable


class ProjectAccessIn(BaseModel):
    access: str  # write | read | none


class MemberRemovedOut(BaseModel):
    """What a removal actually took away, so the caller can say it rather than report a
    bare success."""

    removed_role: str
    projects_revoked: list[str] = []


class InviteCreate(BaseModel):
    email: EmailStr
    role: str = "member"  # admin | member (owner is never invitable)


class InviteOut(ORMModel):
    id: str
    kind: str = "org"  # org | platform (AL-91)
    org_id: str | None = None  # NULL for a platform invite — the org doesn't exist yet
    email: EmailStr
    role: str
    plan: str | None = None  # platform invites only: plan preset for the org they found
    status: str
    created_at: datetime
    expires_at: datetime | None = None
    accept_url: str = ""  # convenience: the link emailed to the invitee (copy-able by admins)


class PlatformInviteCreate(BaseModel):
    email: EmailStr
    plan: str | None = None  # optional preset, e.g. seed a design partner onto `team`


# ---- Additional-org requests (hosted-only, AL-92) ----
class OrgRequestCreate(BaseModel):
    reason: str = ""
    company: str = ""


class OrgRequestOut(ORMModel):
    id: str
    user_id: str
    reason: str = ""
    company: str = ""
    status: str  # pending | approved | denied
    consumed: bool = False
    created_at: datetime
    decided_at: datetime | None = None
    decision_note: str = ""


class OrgRequestDecision(BaseModel):
    approve: bool
    note: str = ""


# ---- Operator console (hosted-only, AL-94) ----
# Deliberately METADATA ONLY: no tenant content (items, memory, PRDs, requests, code)
# ever appears here — that boundary is what keeps the Phase 6 isolation guarantee true.
class AdminWhoamiOut(BaseModel):
    is_platform_admin: bool = True
    email: EmailStr
    # Deployment policy the console states rather than guesses. Both are env config —
    # read-only from here, which is why the Licensing screen reports them instead of
    # offering a control that would silently do nothing.
    signup_mode: str = "open"
    invite_expiry_days: int = 14


class AdminOrgMemberOut(BaseModel):
    """Who is seated in a tenant. Identity + role only; an operator can see that a
    membership exists and never act on it — role changes belong to the org's admins."""

    handle: str
    name: str = ""
    role: str
    joined_at: datetime | None = None


class AdminOrgOut(BaseModel):
    id: str
    name: str
    plan: str
    created_at: datetime | None = None
    owner_email: EmailStr | None = None
    owner_name: str = ""
    owner_handle: str = ""
    usage: UsageOut
    limits: PlanLimitsOut
    members: list[AdminOrgMemberOut] = []


class AdminUserOrgOut(BaseModel):
    id: str
    name: str
    role: str


class AdminUserOut(BaseModel):
    id: str
    name: str
    handle: str
    email: EmailStr
    created_at: datetime | None = None
    org_count: int = 0
    orgs: list[AdminUserOrgOut] = []
    # Last *write* — the newest event this account is the actor of. Deliberately not
    # called "last active": reads leave no ledger row, so a user who only browsed is
    # indistinguishable from one who never signed in. NULL means "no write on record",
    # which is a different fact from "inactive" and is rendered as its own state.
    last_write_at: datetime | None = None


class AdminInviteOut(InviteOut):
    """A platform invite as the operator plane sees it — the invite plus its provenance.

    ``redeemed_org_*`` is populated only once the invite has been accepted AND the
    account it seeded owns an org; an accepted invite whose owner has not founded
    anything yet reports empty rather than guessing, so "redeemed" and "redeemed into
    something" stay separable."""

    invited_by_handle: str = ""
    redeemed_org_id: str = ""
    redeemed_org_name: str = ""
    expired: bool = False


class AdminActivityOut(BaseModel):
    """One row of the operator ledger — an action taken FROM this plane.

    Not a tenant activity feed: events inside an org are project-scoped and stay there.
    The console says so on the panel, because an operator reading six rows and inferring
    "the platform was quiet" would be reading an absence as a result."""

    ts: datetime
    action: str
    actor_label: str = ""
    target_type: str = ""
    target_id: str = ""
    meta: dict | None = None


class InvitePreviewOut(BaseModel):
    """Unauthenticated peek at an invite (by token) so the accept page can show who/
    what before the invitee logs in. Reveals only the org name + invited email/role.
    For a platform invite (AL-91) there is no org yet, so ``org_name`` is empty and
    ``kind`` tells the page to render the found-your-own-org flow instead."""
    kind: str = "org"  # org | platform
    org_name: str = ""
    email: EmailStr
    role: str
    invited_by: str


class InviteAcceptIn(BaseModel):
    token: str


# ---- Plans & quotas (hosted-only, AL-75) ----
class PlanLimitsOut(BaseModel):
    max_projects: int
    max_seats: int
    max_shards: int
    max_calls_per_month: int


class UsageOut(BaseModel):
    projects: int
    seats: int
    shards: int
    calls_this_month: int


class BillingOut(BaseModel):
    plan: str
    limits: PlanLimitsOut
    usage: UsageOut


class SetPlanIn(BaseModel):
    plan: str  # free | pro | team


# ---- Projects ----
class ProjectOut(ORMModel):
    id: str
    name: str
    tag: str  # short key prefix; item/request/PRD keys render from it (PRD-13)
    accent: str
    visibility: str
    description: str
    share_global_memory: bool
    auto_extract: bool
    mcp_enabled: bool
    embed_model: str
    # The project's credential override and its model (PRD-25). Exposed because the settings
    # UI lists override RULES — project, provider AND model — and a rule that named only the
    # provider would hide the thing most often overridden: two projects share a key and want
    # different models, which is precisely what `model_override` exists for.
    credential_id: str | None = None
    model_override: str = ""
    # Memory auto-triage (AL-227); write mode replaced `memory_auto_accept` (AL-280).
    memory_write_mode: str = "review"
    memory_auto_reject: bool = True
    memory_llm_judge: bool = False
    # May an agent operate this project's quality gates (AL-282)? Never authority gates.
    agent_adjudication: bool = False
    # Danger mode (GRPH-380): may an agent sign off its own work when nobody else can?
    allow_self_review: bool = False


class ProjectCreate(BaseModel):
    name: str
    # Optional: derived from `name` when omitted. NOT on ProjectUpdate — changing a tag
    # has to go through the retag path so it records tag history (AL-258).
    tag: str | None = None
    accent: str = "#c6f24e"
    description: str = ""
    # Hosted-only (AL-74b): the org to create the project under. Optional when the
    # caller belongs to exactly one org (defaulted server-side); required otherwise.
    # Ignored on self-host, where projects have no org.
    org_id: str | None = None


# ---- Items ----
class ItemCreate(BaseModel):
    title: str
    description: str = ""
    tags: list[str] = []
    touchpoints: list[str] = []
    effort: int = 0
    status: str = "backlog"
    project_id: str = "core"
    prd_id: str | None = None
    prd_section: str = ""


class ItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    effort: int | None = None
    blocker: str | None = None
    github_url: str | None = None
    assignee: str | None = None
    touchpoints: list[str] | None = None
    prd_id: str | None = None
    prd_section: str | None = None
    evidence: list[dict] | None = None  # proof-on-done receipts (AL-53)
    # Human-confirmed fidelity flip after a prototype settles the question (GRPH-235).
    # The service whitelist already accepted `fidelity` — MCP could write it while the
    # web PATCH silently could not, so the loop this item closes had no confirm button.
    fidelity: str | None = None


class ReorderIn(BaseModel):
    ordered_ids: list[str]


class ItemOut(ORMModel):
    id: str = _key("key")
    project_id: str
    title: str
    description: str
    status: str
    tags: list[str]
    touchpoints: list[str] = []
    effort: int
    sort_order: int
    blocker: str
    date: str
    reporter: dict
    pr: dict | None
    github_url: str = ""
    evidence: list[dict] = []
    assignee: str = ""
    claimed_by: str | None = None
    # Why a reviewer sent it back (GRPH-378). A human opening a bounced item asks the same
    # question its author does, and the board had no answer either.
    bounce_reason: str = ""
    prd_id: str | None = _key("prd_key", default=None)
    prd_section: str = ""
    fidelity: str = "low"
    created_at: datetime
    updated_at: datetime

    @field_validator("touchpoints", "evidence", mode="before")
    @classmethod
    def _tp_default(cls, v):
        return v or []


# ---- Memory ----
class ShardCreate(BaseModel):
    text: str
    scope: str = "global"
    item_id: str | None = None
    project_id: str | None = "core"


class ShardOut(ORMModel):
    id: str
    text: str
    scope: str
    source: str
    status: str
    origin: str
    item_id: str | None = _key("item_key", default=None)
    project_id: str | None
    fresh: bool
    # Auto-triage provenance (AL-227): "" = human-only; else the signal that acted
    # ("similarity" | "llm") plus the confidence it acted on.
    scoring_source: str = ""
    auto_confidence: float | None = None
    created_at: datetime


class MemorySearchIn(BaseModel):
    query: str
    top_k: int = 5
    project_id: str | None = None


class ShardHit(BaseModel):
    shard: ShardOut
    score: float


class ScoredCandidate(BaseModel):
    """Advisory review suggestion for a candidate shard (AL-151)."""
    shard: ShardOut
    suggestion: str  # accept | reject | review
    confidence: float
    reasons: list[str]
    duplicate_of: str | None = None


class EffectivenessListOut(BaseModel):
    score: float | None
    trend: str
    drop_reasons: list[str]


class EligibilityOut(BaseModel):
    state: str
    independence: int | None
    distinct_projects: int | None
    distinct_users: int | None
    cluster_scan: str
    reason: str


class LessonListRow(BaseModel):
    id: str
    text: str
    scope: str
    source: str
    status: str
    origin: str
    item_id: str | None = None
    project_id: str | None
    fresh: bool
    scoring_source: str = ""
    auto_confidence: float | None = None
    created_at: datetime
    reach: str
    lesson_class: str
    suggested_class: str | None = None
    age_state: str
    caught_state: str
    effectiveness: EffectivenessListOut
    eligibility: EligibilityOut
    transferability: str


class LessonListOut(BaseModel):
    enums: dict
    results: list[LessonListRow]
    total: int
    limit: int
    offset: int
    has_more: bool


class EffectivenessDetailOut(EffectivenessListOut):
    history: list[dict]


class OutcomeOut(BaseModel):
    id: int
    shard_id: str
    kind: str
    source: str
    related_item_id: str | None = None
    related_shard_id: str | None = None
    detail: str = ""
    created_at: datetime


class LessonDetail(LessonListRow):
    effectiveness: EffectivenessDetailOut  # type: ignore[assignment]
    origin_path: str
    cluster: list[ShardOut]
    unread_cluster_tags: list[str]
    outcomes: list[OutcomeOut]
    events: list[dict]
    originating_item: dict | None = None


class LessonOutcomeIn(BaseModel):
    kind: str
    detail: str = Field(min_length=1)


class PromoteOrgIn(BaseModel):
    override_reason: str | None = None


# ---- Requests ----
class RequestCreate(BaseModel):
    type: str
    title: str
    # The model and the service have always carried a detail; only this schema did not,
    # so an authenticated caller submitting one had it silently dropped.
    detail: str = ""
    by: str = ""
    project_id: str = "core"


class RequestLinkIn(BaseModel):
    item_id: str | None = None


class RequestVoteIn(BaseModel):
    delta: int = 1


class DuplicateHintOut(BaseModel):
    """A request or item that looks like this one. Advisory — never auto-merged."""

    kind: str  # request | item
    id: str
    title: str
    score: float


class TriageRequestOut(BaseModel):
    """A queued request plus the one thing a triager needs that the row itself cannot
    say: whether it has already been reported."""

    request: RequestOut
    # Best duplicate above threshold, or null. Null means "compared, nothing matched" —
    # the comparison always runs, so it is never "we did not look".
    duplicate: DuplicateHintOut | None = None


class RequestAcceptOut(BaseModel):
    """Both halves of a triage decision, so the client never has to guess whether the
    link landed."""

    request: RequestOut
    item: ItemOut


class RequestOut(ORMModel):
    id: str = _key("key")
    project_id: str
    type: str
    title: str
    detail: str = ""
    by: str
    votes: int
    status: str
    linked_to: str | None = _key("linked_to_key", default=None)
    ago: str
    source_url: str = ""
    meta: dict = {}
    attachment_ids: list[str] = []
    created_at: datetime

    # Rows created before these columns existed can hold NULL; coerce to the default.
    @field_validator("meta", mode="before")
    @classmethod
    def _meta_default(cls, v):
        return v or {}

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def _atts_default(cls, v):
        return v or []


# ---- API keys ----
# The full scope vocabulary. `write` gates mutating MCP tools (mcp_server), `sync` gates
# code-graph ingest (authz.key_sync_ids); `read` is implicit but accepted so a key can be
# minted read-only. Validated rather than free-text because an unrecognised scope silently
# produces a DEAD key — "Sync" or "syncs" would never match the `"sync" in key.scopes` check
# and the failure only surfaces later as a 403 at push time.
#
# `gate` (GRPH-541) may write an `attestation` receipt — the proof the completion gate reads
# (authz.key_gate_ids). Deliberately NOT implied by `write`: an agent that could mint its own
# attestation could certify its own work, which is the entire thing the scope exists to stop.
# This validator earned its keep here — the first draft of the feature minted keys with a
# scope this tuple did not list, and the 422 is what said so.
API_KEY_SCOPES = ("read", "write", "sync", "gate")


class ApiKeyCreate(BaseModel):
    name: str = "agent key"
    scopes: list[str] = ["read", "write"]
    # Project the key's agent writes to by default. None = global key.
    project_id: str | None = None
    # Optional lifetime; None = non-expiring (AL-72).
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    # Optional tool tiers (GRPH-571). Empty = the core manifest, which is the default and
    # not a degraded state. Never widens what the key may CALL — only what it is told about.
    tool_tiers: list[str] = []

    @field_validator("tool_tiers")
    @classmethod
    def _known_tiers(cls, v: list[str]) -> list[str]:
        # Validated for the same reason `scopes` is, one field up: an unrecognised tier
        # would mint fine and never widen anything, and the failure surfaces much later as
        # a tool the agent cannot see and cannot explain.
        bad = tool_tiers_svc.unknown(v)
        if bad:
            raise ValueError(
                f"unknown tier(s) {bad}; allowed: {list(tool_tiers_svc.TIERS)}"
            )
        return v

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in API_KEY_SCOPES]
        if unknown:
            raise ValueError(
                f"unknown scope(s) {unknown}; allowed: {list(API_KEY_SCOPES)}"
            )
        return v


class ApiKeyOut(ORMModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    # Surfaced so the UI can show what a key was minted with. `None` means core-only.
    tool_tiers: list[str] | None = None
    project_id: str | None = None
    last_used: datetime | None
    created_at: datetime
    expires_at: datetime | None = None
    revoked: bool = False
    # Set when Fleet minted this for a wave. Null is a hand-minted key, never swept.
    # Without this on the list, a wave key and an ordinary API key look the same, and
    # End wave sweeping one of them is invisible until it happens.
    fleet_wave: str | None = None


class ApiKeyCreated(ApiKeyOut):
    plaintext: str


# ---- Platform + integrations (Phase 5) ----
class PlatformConfigOut(ORMModel):
    project_id: str
    llm_mode: str
    local_base_url: str
    local_model: str
    cloud_provider: str
    cloud_model: str
    github_connected: bool
    github_account: str
    github_repo: str
    github_scope: str
    gdrive_connected: bool
    gdrive_account: str
    gdrive_folder: str
    rate_limit_per_min: int = 20
    turnstile_sitekey: str = ""
    turnstile_secret_set: bool = False  # never expose the secret itself
    active_chat_provider: str = ""
    provider_config: dict = {}  # redacted per-provider config (api keys → key_set bool)
    public_share_enabled: bool = False
    share_token: str | None = None  # the share-link token (shown to authed members only)
    sync_graph: bool = True  # AL-137 D8: whether this project's code graph pushes to the cloud

    @computed_field
    @property
    def effective_chat_provider(self) -> str:
        """Provider that will actually serve chat for THIS project, resolved from its own
        config (mirrors platform._chat_params). 'stub' means no real AI is in effect, which
        the UI surfaces as a no-model banner. Per-project — one project's provider is never
        conflated with another's."""
        if self.active_chat_provider:
            return self.active_chat_provider
        if self.llm_mode == "local":
            return "ollama"
        if self.llm_mode == "cloud":
            return "anthropic"
        return "stub"


class PlatformUpdate(BaseModel):
    llm_mode: str | None = None
    local_base_url: str | None = None
    local_model: str | None = None
    cloud_provider: str | None = None
    cloud_model: str | None = None
    rate_limit_per_min: int | None = None
    turnstile_sitekey: str | None = None
    turnstile_secret: str | None = None
    active_chat_provider: str | None = None
    providers: dict | None = None
    public_share_enabled: bool | None = None
    sync_graph: bool | None = None  # AL-137 D8: opt out of pushing this project's code graph


class SyncLinkIn(BaseModel):
    """Link this instance to a cloud tenant (AL-141). `api_key` is the `sync`-scoped org key;
    blank on a re-link keeps the stored one (write-only round-trip, like BYOK provider keys)."""
    cloud_url: str
    api_key: str = ""
    org: str = ""


class SyncProjectState(BaseModel):
    project_id: str
    name: str
    writable: bool
    sync_graph: bool
    total_nodes: int
    synced_nodes: int
    pending: int
    last_synced_at: datetime | None = None
    status: str  # live | stale | paused | unsynced | empty


class SyncStatusOut(BaseModel):
    """Everything the Sync/Link settings page renders — link state (never the key itself) plus
    per-project sync state derived from local truth."""
    linked: bool
    source: str  # "web" (DB link) | "env" (baked-in) | "" (unlinked)
    cloud_url: str
    org: str
    credential_set: bool
    linked_at: datetime | None = None
    projects: list[SyncProjectState] = []


class GitopsField(BaseModel):
    value: str | bool | None = None
    source: str


class GitopsFields(BaseModel):
    base_branch: GitopsField
    no_push_to_base: GitopsField
    branch_name_pattern: GitopsField
    pr_title_pattern: GitopsField
    reviewer_bar: GitopsField


class GitopsControl(BaseModel):
    state: str
    writable: bool = False
    message: str = ""


class GitopsWas(BaseModel):
    base_branch: str | None = None
    no_push_to_base: bool | None = None
    branch_name_pattern: str | None = None
    pr_title_pattern: str | None = None
    reviewer_bar: str | None = None


class GitopsProjectRef(BaseModel):
    id: str
    name: str
    tag: str


class GitopsPlanRef(BaseModel):
    """The tracker parent filed when a named model was saved. Not on get_context."""
    id: str
    title: str


class GitopsView(BaseModel):
    project_id: str | None = None
    org_id: str | None = None
    fields: GitopsFields
    control: GitopsControl
    was: GitopsWas | None = None
    version_from: GitopsField
    model: GitopsField
    plan: GitopsPlanRef | None = None
    projects: list[GitopsProjectRef] = Field(default_factory=list)


class GitopsPatch(BaseModel):
    """Omit a key = no change. JSON null = clear to unmeasured/inherit."""
    base_branch: str | None = None
    no_push_to_base: bool | None = None
    branch_name_pattern: str | None = None
    pr_title_pattern: str | None = None
    reviewer_bar: str | None = None
    version_from: str | None = None
    model: str | None = None


class GithubConnectIn(BaseModel):
    account: str
    repo: str


class GdriveConnectIn(BaseModel):
    account: str
    folder: str


class GithubIssueIn(BaseModel):
    title: str
    body: str = ""
    type: str = "feature"  # feature | bug | enhancement | feedback


class ProjectRetagIn(BaseModel):
    """Changing a tag is its own operation, not a PATCH field — it has to record tag
    history so keys rendered under the old tag keep resolving (PRD-13)."""

    tag: str


class ProjectUpdate(BaseModel):
    name: str | None = None
    accent: str | None = None
    visibility: str | None = None
    description: str | None = None
    share_global_memory: bool | None = None
    auto_extract: bool | None = None
    mcp_enabled: bool | None = None
    embed_model: str | None = None
    memory_write_mode: str | None = None
    memory_auto_reject: bool | None = None
    memory_llm_judge: bool | None = None
    agent_adjudication: bool | None = None
    allow_self_review: bool | None = None

    @field_validator("memory_write_mode")
    @classmethod
    def _valid_write_mode(cls, v: str | None) -> str | None:
        # Rejected at the boundary rather than coerced, so a typo is a 422 naming the
        # allowed values instead of a project silently reverting to `review`.
        if v is not None and v not in ("review", "auto", "trusted"):
            raise ValueError("memory_write_mode must be one of: review, auto, trusted")
        return v


class MemberOut(BaseModel):
    user: UserOut
    role: str
    access: str


# ---- PRDs (Phase 3) ----
class PrdVersionOut(ORMModel):
    id: int
    version: str
    date: str
    note: str
    body: str
    # AL-239: which of these is the agreed spec, and what was deferred when it was agreed.
    is_baseline: bool = False
    grill_outcomes: dict | None = None
    supersedes_id: int | None = None
    rebaseline_reason_type: str = ""
    rebaseline_reason: str = ""
    requested_by: str = ""
    created_at: datetime


class PrdOut(ORMModel):
    id: str = _key("key")
    project_id: str
    title: str
    status: str
    version: str
    body: str
    linked: list[str] = _key("linked_keys", default_factory=list)
    updated: str
    created_at: datetime
    updated_at: datetime


class PrdSummary(ORMModel):
    id: str = _key("key")
    title: str
    status: str
    version: str
    linked: list[str] = _key("linked_keys", default_factory=list)
    updated: str


class PrdCreate(BaseModel):
    title: str
    template: str = "standard"
    project_id: str = "core"
    body: str | None = None  # raw markdown when importing a .md file


class PrdUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    body: str | None = None


class PrdVersionIn(BaseModel):
    note: str = ""


class PrdLinkIn(BaseModel):
    item_id: str
    add: bool = True


class PrdAiIn(BaseModel):
    command: str  # expand | risks | summarize


class PrdAiOut(BaseModel):
    text: str


# ---- Public feedback (Phase 2) ----
class PublicRequestIn(BaseModel):
    type: str
    title: str
    detail: str = ""
    email: str = ""
    project_id: str = "core"
    token: str | None = None  # public share token (preferred over raw project_id; AL-73)
    source_url: str = ""
    meta: dict = {}
    attachment_ids: list[str] = []
    hp: str = ""  # honeypot — must stay empty
    turnstile_token: str = ""


class DuplicateHit(BaseModel):
    kind: str  # item | request
    id: str
    title: str
    score: float
    type: str | None = None
    status: str | None = None


class PublicRequestOut(BaseModel):
    request: RequestOut
    duplicates: list[DuplicateHit]


# ---- Agent chat ----
class ChatIn(BaseModel):
    message: str
    project_id: str | None = None


# ---- Grill mode (AL-67): interactive PRD interrogation ----
class GrillMessage(BaseModel):
    role: str  # "user" | "agent"
    text: str


class GrillIn(BaseModel):
    message: str = ""  # empty on the opening turn
    history: list[GrillMessage] = []


class GrillApplyIn(BaseModel):
    history: list[GrillMessage] = []


class GrillDeferIn(BaseModel):
    dimension: str  # one of prds.DIMENSIONS
    reason: str = ""  # why it's being left open — rides onto the AL-302 baseline


class GrillPrototypeIn(BaseModel):
    # GRPH-235: hand ONE high-fidelity question to the prototype tooling.
    item_id: str  # key or id; must live in the PRD's project
    dimension: str = "open_decisions"  # the grill question the prototype settles
    note: str = ""  # optional focus for the screen prompt


class PrototypeVerdictIn(BaseModel):
    item_id: str
    attachment_id: str  # screenshot already uploaded to /api/public/attachments
    verdict: str  # the human's conclusion — what re-enters the grill is this, not pixels
    dimension: str = "open_decisions"


class RebaselineIn(BaseModel):
    # Typed so a chain reads at a glance: a run of `correction` is a spec that was wrong,
    # a run of `scope-change` is a project that moved.
    reason_type: str
    reason: str  # the requester's own words, never a paraphrase


class CloseIn(BaseModel):
    """Closing a PRD (GRPH-244). One entry per baselined section with nothing delivered:
    `{section, disposition: promoted|deferred, promote_to: item|prd, reason, title}`.
    Close gates on disposition, never on delivery — the set must match exactly."""

    dispositions: list[dict] = []
    verdict: str = ""
    # A judge that WAS configured and did not answer blocks the close (GRPH-311); one that
    # was never configured does not. Liveness is an input, not something probed here.
    judge_reachable: bool = True


class PromoteIn(BaseModel):
    """Promote dropped intent out of a PRD (GRPH-246)."""

    sections: list[str]
    # `item` keeps the intent inside this PRD and gives it work; `prd` moves it to a
    # successor, which is the path that lets a terminal state stay terminal.
    target: str = "item"
    title: str = ""


class GrillApplyOut(BaseModel):
    body: str
    decisions_captured: int = 0  # candidate memory shards created from the transcript (AL-69)


class ChatOut(BaseModel):
    reply: str
    shards: list[ShardHit]


class CodeNodeOut(ORMModel):
    id: str
    path: str
    kind: str
    name: str
    lang: str
    summary: str
    fresh: bool
    #: Commit this node was described at; "" when unknown (GRPH-54).
    revision: str = ""


class CodeHit(BaseModel):
    node: CodeNodeOut
    score: float


class CodeAnswerOut(BaseModel):
    reply: str
    nodes: list[CodeHit]


class CodeEdgeOut(BaseModel):
    src: str
    dst: str
    type: str


class ProjectStubOut(BaseModel):
    """A dependency that leaves this repo (PRD-21 D4).

    Anchored on the real node for the file that declares it, so every arrow out is
    explainable by opening one file."""

    edge_id: str
    project_id: str
    tag: str
    name: str
    accent: str
    kind: str
    resolved_name: str = ""
    fresh: bool = True
    evidence: list[dict] = []
    # Paths in THIS project's described graph that carry the declaration.
    anchor_paths: list[str] = []
    # True when the declaring file is named by the manifest but not described here — the
    # arrow is real and its anchor is missing, which is a state, not a reason to hide it.
    unanchored: bool = False


class CodeRevisionsOut(BaseModel):
    """Why `CodeMapOut.revision` is null, when it is (GRPH-54)."""

    distinct: int = 0
    unknown_nodes: int = 0
    known: list[str] = []
    truncated: int = 0


class CodeMapOut(BaseModel):
    nodes: list[CodeNodeOut]
    edges: list[CodeEdgeOut]
    node_count: int
    edge_count: int
    #: The commit this map is pinned to — null unless EVERY node agrees on one, because a
    #: map at two revisions has no revision and reporting the newest would read as current.
    revision: str | None = None
    revisions: CodeRevisionsOut = CodeRevisionsOut()
    # Empty on self-host and on any project outside an org — there are no siblings to
    # depend on, which is different from having none.
    outbound: list[ProjectStubOut] = []


class CodeOutEdge(BaseModel):
    dst: str
    type: str


class CodeInEdge(BaseModel):
    src: str
    type: str


class CodeItemRef(BaseModel):
    id: str
    title: str
    status: str


class CodeLinkedItem(BaseModel):
    id: str
    title: str
    status: str
    relation: str


class CodeLinkedRequest(BaseModel):
    id: str
    title: str
    type: str
    status: str
    relation: str


class CodeNeighborsOut(BaseModel):
    path: str
    node: CodeNodeOut | None
    outgoing: list[CodeOutEdge]
    incoming: list[CodeInEdge]
    items_touching: list[CodeItemRef]
    linked_items: list[CodeLinkedItem]
    linked_requests: list[CodeLinkedRequest]


class CodeRefIn(BaseModel):
    ref_id: str
    path: str
    relation: str = "affects"
    ref_type: str | None = None


class CodeRefOut(BaseModel):
    id: int
    ref_type: str
    ref_id: str
    path: str
    relation: str


class CodeUnlinkIn(BaseModel):
    ref_id: str
    path: str
    relation: str | None = None


class CodeForRefRow(BaseModel):
    path: str
    relation: str
    node: CodeNodeOut | None


class UpstreamReportIn(BaseModel):
    type: str = "feedback"
    title: str
    detail: str = ""
