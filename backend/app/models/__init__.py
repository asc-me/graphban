"""SQLAlchemy models for Graphban.

Kept in one module for the core slice; split out later if it grows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app import tagging
from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EmbeddingType(TypeDecorator):
    """Vector on Postgres, JSON-encoded text on SQLite (test fallback)."""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return json.dumps(list(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return json.loads(value)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    handle: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    avatar: Mapped[str] = mapped_column(String, default="#a78bfa")
    initials: Mapped[str] = mapped_column(String, default="")
    password_hash: Mapped[str] = mapped_column(String)
    # Bumped on logout / password change to revoke every outstanding token: it's
    # embedded in each JWT as `tv` and checked on decode, so a leaked or logged-out
    # refresh token stops working immediately instead of living to its 14d expiry (AL-59).
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")
    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")


def _key_of(owner, stored_id: str | None, kind: str) -> str | None:
    """Render a cross-referenced entity's key, falling back to the stored id.

    Fields like `Item.prd_id` hold another entity's frozen id, so they have to be
    rendered too — otherwise a retag leaks the old tag through a reference field even
    though the entity's own key looks right. The fallback matters: a detached object or
    a dangling reference must degrade to the stored id rather than raise inside
    serialization.
    """
    if not stored_id:
        return stored_id
    session = object_session(owner)
    if session is None:
        return stored_id
    row = session.get({"item": Item, "request": Request, "prd": Prd}[kind], stored_id)
    return row.key if row is not None else stored_id


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # Short prefix its item/request/PRD keys render with, e.g. AL -> AL-12 (PRD-13).
    # Stored uppercase, which is what makes this plain UNIQUE express case-insensitive
    # uniqueness on both engines. Changing it is one UPDATE: keys are rendered, never
    # stored, so no other row in the database moves. See app/tagging.py.
    tag: Mapped[str] = mapped_column(String, unique=True)
    accent: Mapped[str] = mapped_column(String, default="#c6f24e")
    visibility: Mapped[str] = mapped_column(String, default="private")
    description: Mapped[str] = mapped_column(Text, default="")
    share_global_memory: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_extract: Mapped[bool] = mapped_column(Boolean, default=True)
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Memory auto-triage (AL-227): let the AL-151 scorer ACT on agent candidates
    # instead of only advising, so the review queue stays small. Every auto-action
    # is audited and undoable.
    #
    # `write_mode` (AL-280) decides what happens to a NOVEL agent write:
    #   review  — stays a candidate until a human publishes it (the AL-49 boundary)
    #   auto    — publishes only when strongly corroborated (>= _AUTO_ACCEPT_MIN)
    #   trusted — publishes on write, so the agent can read back what it wrote
    # It replaced the `memory_auto_accept` boolean, which never auto-accepted
    # anything novel and so read as a broken setting rather than a strict one.
    #
    # `auto_reject` (on) is ORTHOGONAL to the mode and vetoes in all three: dedup
    # is worth keeping without a human, and `trusted` without it would accumulate
    # restatements of one fact. `llm_judge` (off) swaps the offline similarity
    # scorer for an LLM assessment.
    memory_write_mode: Mapped[str] = mapped_column(
        String, default="review", server_default="review", nullable=False
    )
    memory_auto_reject: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    memory_llm_judge: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # May an agent operate this project's QUALITY gates (AL-282 / PRD-14 D2)? Off by
    # default: it moves the AL-49 boundary, so it is the owner's decision, not a default.
    # It never reaches an AUTHORITY gate — credential minting, retag, org/tenant — which
    # stays human in every configuration (see tests/test_authority_gates.py).
    agent_adjudication: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    embed_model: Mapped[str] = mapped_column(String, default="stub-384")
    # Hosted SaaS only (AL-74): the owning organization. NULL on self-host, where
    # the org layer is inert. In hosted mode authz additionally requires the caller
    # to belong to this org, so a project never leaks outside its tenant.
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    memberships: Mapped[list[Membership]] = relationship(back_populates="project")


class Organization(Base):
    """A hosted-SaaS tenant (AL-74). Self-host never creates these; the org layer
    is gated by HOSTED_MODE and invisible to self-hosted deployments."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, default="free")  # free | pro | team (AL-75)
    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrgMembership(Base):
    """A user's seat in an organization (hosted-only, AL-74)."""

    __tablename__ = "org_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String, default="member")  # owner | admin | member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_membership"),)


class OrgRequest(Base):
    """A user's request to found an ADDITIONAL organization (hosted-only, AL-92).

    Every account gets one org for free; founding another is privileged. A user asks
    here, an operator approves or denies, and an approved request grants exactly ONE
    additional org — ``consumed`` flips when it's spent, so approval can't be reused.
    A standing (unlimited) entitlement comes from the plan instead
    (``Plan.may_found_additional_orgs``, carried by the enterprise tier)."""

    __tablename__ = "org_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # oreq_...
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    company: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|approved|denied
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decision_note: Mapped[str] = mapped_column(String, default="")


class OrgUsage(Base):
    """Per-org, per-month usage counter for plan enforcement (hosted-only, AL-75).

    Only the metered MCP-call count lives here; project/seat/shard usage is derived
    by counting rows on demand. Keyed by (org_id, period) where period is 'YYYY-MM'
    (UTC), so each month starts fresh — no reset job needed."""

    __tablename__ = "org_usage"

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String, primary_key=True)  # 'YYYY-MM' (UTC)
    mcp_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class OrgInvite(Base):
    """A pending invitation, of one of two kinds (hosted-only, AL-74b / AL-91).

    - ``kind="org"`` — seats the invitee in an EXISTING org (``org_id`` set). Created
      by an org owner/admin.
    - ``kind="platform"`` — authorizes a brand-new account to sign up and found its
      OWN org (``org_id`` NULL, since that org doesn't exist yet). Created by a
      platform operator; may carry a ``plan`` to pre-assign to the org they found.

    Both share one pipeline: the unguessable ``token`` addresses the accept link,
    ``status`` moves pending → accepted or revoked, and an invite past ``expires_at``
    is refused even while still ``pending``. Rows are kept after acceptance for
    provenance."""

    __tablename__ = "org_invites"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # inv_...
    kind: Mapped[str] = mapped_column(String, default="org", index=True)  # org | platform
    # NULL for a platform invite — the org is founded on accept.
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String, index=True)  # invitee (may not have an account yet)
    role: Mapped[str] = mapped_column(String, default="member")  # admin | member (never owner)
    # Platform invites only: plan to stamp on the org the invitee founds (e.g. seed a
    # design partner straight onto `team`). NULL = the default free plan.
    plan: Mapped[str | None] = mapped_column(String, nullable=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    invited_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|accepted|revoked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    role: Mapped[str] = mapped_column(String, default="member")  # owner/admin/member
    access: Mapped[str] = mapped_column(String, default="read")  # write/read/none

    user: Mapped[User] = relationship(back_populates="memberships")
    project: Mapped[Project] = relationship(back_populates="memberships")


class Item(Base):
    __tablename__ = "items"

    # FROZEN at issue time and never rewritten — identity, not display (PRD-13). The key
    # a human sees is rendered from the project's current tag + `number`; retagging a
    # project must not move this or any of the eleven other columns that reference it.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    # Per-project, per-kind sequence. Unique within the project, so two projects can both
    # have a number 235 — which is exactly what the old global counter prevented.
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="backlog")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    touchpoints: Mapped[list] = mapped_column(JSON, default=list)  # files/globs/modules the item affects
    effort: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    blocker: Mapped[str] = mapped_column(String, default="")
    date: Mapped[str] = mapped_column(String, default="")  # display date from design
    reporter: Mapped[dict] = mapped_column(JSON, default=dict)
    pr: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    github_url: Mapped[str] = mapped_column(String, default="")  # linked issue/PR
    # Proof-on-done (AL-53): receipts that match evidence to the completion claim —
    # a list of {kind: test|url|screenshot|health|note, detail, url}.
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # Assignment / agent claiming (feature A).
    assignee: Mapped[str] = mapped_column(String, default="")  # durable owner (human or agent)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)  # agent holding the lease
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- the review handoff (PRD-17 D3) ---
    # Where the work landed. Travels to the reviewer, who otherwise cannot see the diff:
    # each worker edits in its own worktree, so "look at the branch" is the only handoff
    # that works across processes on different machines.
    branch: Mapped[str] = mapped_column(String, default="")
    # The agent that signed off. THE load-bearing invariant of PRD-17 is that this is never
    # equal to `claimed_by` — an agent cannot pass its own work. Kept as data rather than
    # inferred from the event log so the assertion at sign-off is a column comparison.
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # A bounced item goes back to its AUTHOR first (PRD-17 D-f): the agent that wrote it has
    # the context, and letting the fleet re-divvy it immediately would hand a stranger a
    # half-finished change plus a review comment about code they have never seen.
    bounce_pinned_to: Mapped[str | None] = mapped_column(String, nullable=True)
    # When the pin lapses. A pin without an expiry is a permanent assignment to an agent
    # that may already be dead, which is the queue silently losing an item.
    bounce_pinned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Spec traceability (feature D): the PRD + section this item implements.
    prd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prd_section: Mapped[str] = mapped_column(String, default="")
    # When this item was attached to that PRD (GRPH-243). `created_at` cannot answer it:
    # an item raised months earlier and linked after approval is scope ADDED, and reading
    # its creation time would file it as original scope — under-reporting exactly the
    # growth this feature exists to surface. NULL means the link predates this column;
    # scope-drift falls back to `created_at` and reports how many it had to infer, rather
    # than backfilling a timestamp nobody recorded.
    prd_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The governing baseline version when work on this item STARTED — stamped at claim,
    # or at the move to `in_progress` for work that never took a lease (GRPH-242). If the
    # PRD has rebaselined since, this item is being built against superseded intent, and
    # that is a derived fact rather than a notification: it does not stop being true
    # because someone dismissed it. NULL means work started before this was recorded, and
    # no claim about which intent it targeted can honestly be made.
    baseline_at_claim: Mapped[str | None] = mapped_column(String, nullable=True)
    # Fidelity (AL-68): `low` = specifiable in words now; `high` = needs a prototype
    # to answer (the grill → prototype → grill handoff). Routes prototype-first work.
    fidelity: Mapped[str] = mapped_column(String, default="low")  # low | high
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # The backstop for the mint path (services/items.py): even if minting is bypassed
    # or two agents race, the database refuses a duplicate number within a project.
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_item_number"),)

    project: Mapped[Project] = relationship()

    @property
    def key(self) -> str:
        """The user-visible key, rendered from the project's CURRENT tag (PRD-13)."""
        return tagging.render(self.project.tag, "item", self.number)

    @property
    def prd_key(self) -> str | None:
        return _key_of(self, self.prd_id, "prd")


class ArtifactRecommendation(Base):
    """What a promoted lesson should BECOME (GRPH-307 / PRD-16).

    One row per (tier, scope) rather than per lesson. PRD-16's acceptance is explicit: two
    lessons landing on the same tier and scope produce ONE recommendation, the second
    superseding the first — not two competing creates that a reviewer has to reconcile by
    hand and that would install two files doing the same job.

    `supersedes_id` keeps the earlier one rather than overwriting it, so "this grew from
    three lessons over two weeks" stays readable. Same append-only reasoning as the
    baseline chain: the record of how a decision was reached is the part you cannot
    reconstruct later.
    """

    __tablename__ = "artifact_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # fact | rule | hook | skill | agent | allowlist | update | delete
    tier: Mapped[str] = mapped_column(String)
    # What this artifact would OWN — the keyword scope a later lesson matches against to
    # become an `update` rather than a duplicate `create`.
    scope: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    # The shard ids this recommendation rests on. Evidence, not decoration: the drafting
    # step re-renders from the current lesson set and needs to know what that set is.
    lesson_ids: Mapped[list] = mapped_column(JSON, default=list)
    # An `update` verdict names the artifact it would amend. NULL on a create.
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    # queued | approved | rejected | superseded. A reviewed row is never flipped back to
    # queued by a later run — that is why classification skips lessons already carrying one.
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    supersedes_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graded_by: Mapped[str] = mapped_column(String, default="", nullable=False)
    # ---- drafting (GRPH-308) ------------------------------------------------------------
    # The artifact ITSELF — the thing someone could install today, not a summary of one.
    draft: Mapped[str] = mapped_column(Text, default="", nullable=False)
    draft_path: Mapped[str] = mapped_column(String, default="", nullable=False)
    # `file_additive` may install on approval; `shared_surgery` never writes. A property of
    # the TARGET, not of the artifact's quality — a perfect edit to AGENTS.md is still an
    # edit to a file other things live in.
    install_class: Mapped[str] = mapped_column(String, default="", nullable=False)
    # sha256 of (statement + evidence): an unchanged lesson set costs zero model calls
    # across runs, which is what makes a scheduled pass affordable to leave switched on.
    draft_hash: Mapped[str] = mapped_column(String, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ArtifactUsage(Base):
    """That a generated artifact was used, and when (GRPH-309 / PRD-16).

    Only ever written by something that OBSERVED a use — a skill invoked by name, an agent
    spawned, a generated hook reporting its own firing. Never inferred from silence.

    That constraint is the whole design. PRD-16: *"A fabricated signal here deletes working
    hooks."* An artifact that works produces no evidence it works — a rule everyone follows
    is mentioned least of all — so absence of a row here means "not observed", never "not
    used", and the staleness sweep must only ever run against artifacts whose absence of use
    is genuinely observable.
    """

    __tablename__ = "artifact_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(Integer, index=True)
    # skill invocation | agent spawn | hook self-report. Named so a later reader can tell
    # a first-party signal from a self-reported one without guessing.
    signal: Mapped[str] = mapped_column(String, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ArtifactTombstone(Base):
    """A retired artifact, and everything needed to bring it back (GRPH-309 / PRD-16).

    *"Retirement archives with a tombstone recording original path, use count, and a
    one-command restore. Never an unrecoverable delete."*

    The contents are kept in full. A retirement that discarded them would make the decision
    irreversible on the strength of a usage count — and a usage count is exactly the kind of
    evidence that turns out to have been measuring the wrong thing.
    """

    __tablename__ = "artifact_tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(Integer, index=True)
    path: Mapped[str] = mapped_column(String, default="")
    contents: Mapped[str] = mapped_column(Text, default="")
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    retired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactInventoryItem(Base):
    """An artifact that EXISTS on disk, whoever wrote it (GRPH-354 / PRD-16).

    `ArtifactRecommendation` holds only what this pipeline generated, so usage telemetry was
    blind to every skill, hook, agent and rule a human wrote by hand — reporting a population
    of zero on an install whose `.claude/` directory was full of them. Those are the artifacts
    actually spending context on every turn.

    Read-only by construction. A row here means "this was seen"; it never authorises writing,
    moving or deleting the file it describes.

    `state` carries the three findings that matter, and they are genuinely different claims:

      present  — the file is there. If it is machine-owned, it still matches what we rendered.
      forked   — machine-owned, and a human has since EDITED it. `install_plan` must refuse to
                 re-render this: updates are full re-renders, so re-rendering silently
                 discards their edit — the exact trust failure propose-only exists to prevent.
      orphaned — inventoried before, absent from the latest scan of the same root. Flagged,
                 never retired: a file missing from one scan may be an unmounted volume, and
                 retiring on that reading deletes working artifacts.
    """

    __tablename__ = "artifact_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # The root this file was found under. Kept so a scan of one root can only ever orphan
    # rows from THAT root — scanning `~/.claude` must not mark a `~/work/.cursor` artifact
    # missing merely because this pass never looked there.
    root: Mapped[str] = mapped_column(String, default="", index=True)
    path: Mapped[str] = mapped_column(String, index=True)
    # skill | agent | hook | rule — the same vocabulary artifacts.py classifies into, so a
    # discovered rule is unmeasurable for exactly the reason a generated one is.
    tier: Mapped[str] = mapped_column(String, default="")
    content_hash: Mapped[str] = mapped_column(String, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    # Set when this file matches a generated artifact's `draft_path`. NULL = human-authored,
    # which is the majority and the whole point of the table.
    recommendation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    state: Mapped[str] = mapped_column(String, default="present", index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    """A process that connects to this instance and does work (PRD-17 D1).

    **Nothing counted agents before.** `agent_id` was a self-declared string defaulting to
    the API key's name (`mcp_server.py`), so three terminals sharing a key were one agent to
    the server — nothing could assign roles between them, nothing could stop one reviewing
    its own work, and the roster's basic question (who is out there) had no answer.

    `state` is `idle|working|reviewing|offline`, and **`offline` is derived from
    `last_seen_at`, never stored as a transition.** An agent that dies does not say so, and
    a stored status would read healthy for a process killed an hour ago.

    Agent death needs no new mechanism: past its presence TTL the item leases lapse into the
    existing stale-reclaim path (`items._is_claimable`) and reservations expire. The lease
    timeout that already ships IS the death detector.
    """

    __tablename__ = "agents"

    # Frozen at issue time and rendered as `<TAG>-A<n>` (PRD-13). A per-project sequence
    # rather than a uuid because agents are NAMED in the Fleet view — "GRPH-A3 is stuck" —
    # and because simultaneous registrations on one key then get distinct ids by
    # construction, which is the property PRD-17 asks for.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    # The credential this agent authenticated with — hence the roles it is ELIGIBLE for.
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    # "claude-opus-5 @ macbook:wt-2". Duplicates are ALLOWED and that is deliberate: two
    # identical windows on one machine is a legitimate fleet shape, and de-duplicating by
    # label would merge two real agents into one — the exact bug this table exists to fix.
    label: Mapped[str] = mapped_column(String, default="")
    active_role: Mapped[str] = mapped_column(String, default="worker")
    role_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a poll response actually DELIVERED the directive. `assigned > acked` means
    # the next response must carry it — the comparison IS the outbox, so there is no queue
    # table to drift out of step with the roster.
    role_acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # {vendor, model, tier, readonly, host}. `vendor` is load-bearing: a Claude reviewer
    # approving Claude work is a different agent but not a different error distribution.
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    worktree: Mapped[str] = mapped_column(String, default="")
    branch: Mapped[str] = mapped_column(String, default="")
    # Presence lapsed while a branch was unmerged. The fleet releases the ITEM by itself;
    # the BRANCH is state only a human can resolve, so it is surfaced on the roster rather
    # than left as a footnote in a log nobody reads.
    branch_orphaned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(),
                                                  nullable=False)
    state: Mapped[str] = mapped_column(String, default="idle", index=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    project: Mapped[Project] = relationship()

    @property
    def key(self) -> str:
        """The user-visible key, rendered from the project's CURRENT tag (PRD-13)."""
        return tagging.render(self.project.tag, "agent", self.number)


class AreaReservation(Base):
    """A touch-area an agent holds while work is in flight (PRD-17 D-d).

    Collision clustering already partitions the backlog into sets that provably do not share
    files, but a partition is computed once and the fleet moves afterwards. A reservation is
    the running claim on top of it, so a second agent asking for work is not handed something
    that overlaps an edit already underway.

    `expires_at` rather than an explicit release: the agent holding it may die, and a
    reservation nothing can expire is a file nobody may touch again. It shares the lease
    clock, so one number governs claims, reservations, the bounce pin, and presence together.
    """

    __tablename__ = "area_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    # A path, glob or module — the same vocabulary as `Item.touchpoints`, so the reservation
    # and the collision map are comparable without a translation step.
    area: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestWatermark(Base):
    """How far ingest got in one source (GRPH-304 / PRD-16).

    The watermark is OPAQUE to everything but the adapter that wrote it: a line count here,
    a byte offset or a row cursor elsewhere. A core that knows which is a core that has to
    learn every harness, and that is exactly the coupling the adapter interface exists to
    avoid.

    Keyed by (adapter, source) so two harnesses reading the same path — an archive replayed
    through a second parser, say — do not overwrite each other's progress.
    """

    __tablename__ = "ingest_watermarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    adapter: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    watermark: Mapped[str] = mapped_column(String, default="")
    events_seen: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("adapter", "source", name="uq_ingest_source"),)


class MemoryShard(Base):
    __tablename__ = "memory_shards"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String, default="global")  # global/item
    source: Mapped[str] = mapped_column(String, default="")
    # Lifecycle (AL-49): agent self-reports enter as `candidate` and only reach the
    # default retrieval path once a human `publish`es them. `rejected` is kept for
    # provenance but never surfaces in search. `origin` records who/what wrote it.
    status: Mapped[str] = mapped_column(String, default="published", index=True)  # candidate|published|rejected
    origin: Mapped[str] = mapped_column(String, default="")  # user:<handle> | agent:<key> | agent:auto-extract
    embedding = mapped_column(EmbeddingType(settings.embed_dim), nullable=True)
    fresh: Mapped[bool] = mapped_column(Boolean, default=False)
    # Auto-triage provenance (AL-227): set when the scorer published/rejected this
    # shard without a human. `scoring_source` ("" = human-only) marks an auto-action
    # and names the signal ("similarity" | "llm"); `auto_confidence` is the score it
    # acted on. Powers the "recent auto-actions" lane and keeps every auto-action undoable.
    scoring_source: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    auto_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Whether this row went through the redactor (GRPH-305). False on anything written
    # before it existed — which is NOT the same as clean, and the whole reason this is
    # recorded rather than inferred from the text looking fine.
    scrubbed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(),
                                           nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def item_key(self) -> str | None:
        return _key_of(self, self.item_id, "item")


class Request(Base):
    __tablename__ = "requests"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # frozen; see Item.id
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    number: Mapped[int] = mapped_column(Integer)  # renders as <TAG>-R<number>
    type: Mapped[str] = mapped_column(String)  # bug/feature/enhancement/feedback
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text, default="")  # submitter's full description
    by: Mapped[str] = mapped_column(String, default="")
    votes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="new")  # new/triaging/linked
    linked_to: Mapped[str | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    ago: Mapped[str] = mapped_column(String, default="")  # display string from design
    source_url: Mapped[str] = mapped_column(String, default="")  # page the widget was on
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # user_agent, app_version, custom
    attachment_ids: Mapped[list] = mapped_column(JSON, default=list)  # screenshot ids
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_request_number"),)

    project: Mapped[Project] = relationship()

    @property
    def key(self) -> str:
        return tagging.render(self.project.tag, "request", self.number)

    @property
    def linked_to_key(self) -> str | None:
        return _key_of(self, self.linked_to, "item")


class Prd(Base):
    __tablename__ = "prds"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # frozen; see Item.id
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    number: Mapped[int] = mapped_column(Integer)  # renders as <TAG>-P<number>
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft/review/approved
    version: Mapped[str] = mapped_column(String, default="v0.1")
    body: Mapped[str] = mapped_column(Text, default="")
    linked: Mapped[list] = mapped_column(JSON, default=list)  # linked item ids
    # A rebaseline that has been REQUESTED but not yet earned (AL-241). Holds the
    # requester's words until the grill completes, then travels onto the new baseline and
    # is cleared. Non-null means "this PRD is being re-interrogated", which is exactly
    # the state a UI needs to distinguish from an ordinary first-time review.
    pending_rebaseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The terminal record (GRPH-244): when, against which baseline, in which mode, and
    # what was decided about every piece of intent that had nothing delivered. A SNAPSHOT
    # — it never recomputes. If it did, a closed PRD could silently acquire undelivered
    # sections nobody ever dispositioned, and the gate would be a thing that passed once
    # rather than a thing that holds. Non-null means closed, which is why there is no
    # separate flag to disagree with it.
    close_record: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Lineage (GRPH-246). A PRD created to carry intent DROPPED from an earlier one points
    # back at it, and names the baselined sections it inherited. Both are needed: the link
    # alone says a successor exists, and `promoted_sections` is what makes the chain from
    # original intent, through what was dropped, to what came next actually walkable.
    # Holds the frozen id, never a rendering — see GRPH-319.
    supersedes_prd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    promoted_sections: Mapped[list] = mapped_column(JSON, default=list)
    # Where the CURRENT interrogation starts in the transcript (GRPH-322). A rebaseline is
    # a new statement of intent and has to earn approval on its own answers; without this
    # the previous grill's answers grade it, and "we edited the spec to match what we
    # built" is approved by a conversation about the spec it replaced. The transcript
    # itself stays whole — history is append-only. Only the EVIDENCE WINDOW moves.
    grill_from_seq: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    updated: Mapped[str] = mapped_column(String, default="")  # display date from design
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    versions: Mapped[list[PrdVersion]] = relationship(
        back_populates="prd", cascade="all, delete-orphan", order_by="desc(PrdVersion.id)"
    )

    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_prd_number"),)

    project: Mapped[Project] = relationship()

    @property
    def key(self) -> str:
        return tagging.render(self.project.tag, "prd", self.number)

    @property
    def linked_keys(self) -> list[str]:
        return [_key_of(self, i, "item") for i in (self.linked or [])]


class GrillTurn(Base):
    """One turn of a PRD's grill, owned by the SERVER (AL-296 / PRD-15 D4).

    The grill used to live entirely in the client: it posted the whole transcript to
    `/grill/stream` and `/grill/apply`, and nothing was kept. That was fine while the
    grill was advisory. It stops being fine now that approval is derived from it — the
    server has to be able to answer "has this PRD been grilled, and is anything still
    open?" without trusting a caller to tell it.

    Distinct from the memory shards `capture_grill_decisions` writes, and deliberately
    so. Those hold the durable CONTENT of each decision and flow through Memory review;
    this holds the STRUCTURE of the conversation — what was asked, what came back, in
    what order. Merging them would make one of the two jobs worse.

    Append-only. A grill on a PRD is one continuing conversation, so re-opening it adds
    rounds rather than replacing them; the earlier rounds did happen.
    """

    __tablename__ = "grill_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prd_id: Mapped[str] = mapped_column(ForeignKey("prds.id"), index=True)
    # Position in the conversation. Unique per PRD so a double-submit can't interleave.
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)  # "agent" (question) | "user" (answer)
    text: Mapped[str] = mapped_column(Text, default="")
    # Where an ANSWER came from (AL-299): "human" — typed in an authenticated session —
    # or "agent" — relayed by an agent from what a person told it in chat. Both are
    # legitimate; the relayed path is what keeps the coding-agent loop frictionless, so
    # it is recorded rather than blocked. Empty for questions, which nobody supplies.
    via: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    actor: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("prd_id", "seq", name="uq_grill_turn_seq"),)


class GrillDimension(Base):
    """One dimension's outcome for a PRD's grill (AL-297 / PRD-15 D1).

    "The grill ran out of objections" has to mean the same thing everywhere, or
    `approved` denotes something different on every instance and PRD-12's baselines stop
    being comparable to each other. Four fixed dimensions, three outcomes each.

    `deferred` is the load-bearing one. Real specs leave things open, and "we are
    consciously not deciding X yet" is itself a decision — so it completes rather than
    blocks, and rides onto the baseline where later drift on that point reads as expected
    instead of as a surprise. What must never pass is an IMPLICIT non-answer counted as an
    answer, which is exactly what naming `deferred` separately makes visible.

    A dimension with no row here is `unanswered`. Absence is the honest default: it means
    nobody put the question, which is not the same as an author declining to answer it.
    """

    __tablename__ = "grill_dimensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prd_id: Mapped[str] = mapped_column(ForeignKey("prds.id"), index=True)
    dimension: Mapped[str] = mapped_column(String)  # see prds.DIMENSIONS
    outcome: Mapped[str] = mapped_column(String)  # resolved | deferred | unanswered
    note: Mapped[str] = mapped_column(Text, default="")  # why — the deferral reason, etc.
    # Which provider set the bar (AL-299): a real model id, "stub" for the offline
    # mechanical rule, or "author" for an explicit deferral. A stub-graded dimension
    # means "an answer was recorded", not "an answer was good" — without this on the
    # record the two are indistinguishable to anyone reading a baseline later.
    graded_by: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    # Which turn settled it, for traceability back into the conversation — the ANSWER the
    # verdict cites, resolved to its global `GrillTurn.seq`. NULL when the citation could
    # not be mapped: a wrong pointer is worse than none, because it reads as provenance.
    turn_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("prd_id", "dimension", name="uq_grill_dimension"),)


class WorkClassification(Base):
    """The platform judge's read on one completed item against its PRD's goal (GRPH-249).

    One row per item — the LATEST judgement, not a history. A classification is a derived
    read of "does this work serve the goal", and the thing worth keeping is what that
    answer is now; the audit trail of who claimed what lives on `Verdict`, which is
    append-only precisely because it holds claims rather than derivations.

    **Its ceiling is bounded and stated rather than implied.** This judge sees item text,
    evidence and the code graph. It never sees a diff. It assesses subject-matter alignment
    well and delivery correctness not at all, so `serves` means "this is about the right
    thing", never "this works".
    """

    __tablename__ = "work_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    prd_id: Mapped[str] = mapped_column(String, index=True)
    # serves | enables | unrelated | undecidable. `enables` is DERIVED from the link graph,
    # never asked of the model — typed links plus blocked_by/unblocks already encode it,
    # and the LLM is spent only on the semantic call.
    outcome: Mapped[str] = mapped_column(String)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Which bar was applied: a real model id, or "stub" when nothing is configured. A
    # stub-graded row means "not assessed", and without this on the record that is
    # indistinguishable from "assessed and found fine" (the AL-299 rule).
    graded_by: Mapped[str] = mapped_column(String, default="", nullable=False)
    # The intent this was judged against. A baseline change invalidates prior judgements,
    # so a classification with no baseline stamped could never be known to be current.
    baseline_version: Mapped[str] = mapped_column(String, default="")
    # THE single source of truth for whether this needs recomputing. Eager recompute is a
    # warm-up on the lazy path rather than a second design, so both routes agree by
    # construction instead of by care.
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # A low-confidence `unrelated` is not acted on — it defers to sign-off. Only a
    # high-confidence one self-flags (the AL-227 auto-triage pattern).
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("item_id", name="uq_classification_item"),)


class Verdict(Base):
    """A sign-off judgement about a PRD, with the provenance that makes it arguable.

    PRD-12 is blunt that an agent-side signer *moves* the self-attestation problem rather
    than solving it, so the mitigation is falsifiability rather than trust: a verdict
    cites, the citations are validated against things that exist, and who signed it is on
    the record. **A verdict is a claim with provenance, never truth**, and every field here
    exists to keep that readable rather than to make it more believable.

    `outcome` is stored as given. The sign-off taxonomy belongs to the agent judge
    (GRPH-252); this row owns whether a verdict is admissible and who stands behind it,
    and inventing the vocabulary here would fix it before the component that uses it
    exists.
    """

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prd_id: Mapped[str] = mapped_column(ForeignKey("prds.id"), index=True)
    # The baseline this was judged against. A verdict outlives the intent it was made
    # about, and without this it silently reads as a judgement of the current spec.
    baseline_version: Mapped[str] = mapped_column(String, default="")
    # Which piece of intent this verdict is ABOUT — a baselined section, the intent atom
    # (GRPH-313). NULL is a PRD-level verdict. PRD-12 asks for "a structured verdict per
    # intent element", and one verdict for a whole PRD cannot say which part it read: an
    # auditor that covered three sections of fourteen would be indistinguishable from one
    # that covered all of them.
    section: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    # [{kind, ref}] — validated at submission (prds.validate_verdict) against the code
    # graph, the baseline's sections, or an item's evidence, per the citation form.
    citations: Mapped[list] = mapped_column(JSON, default=list)
    signed_by: Mapped[str] = mapped_column(String, default="")
    # Which credential submitted it. Two agents can share a display name; the key cannot
    # be borrowed by accident, so it is the identity that survives a dispute.
    api_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The signer also held a claim on work under audit — grading their own exam through a
    # second door. Flagged rather than refused: on a solo project it is unavoidable and
    # refusing would just stop anyone signing off, but it must never be invisible.
    self_signed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # WHY `self_signed` says what it says (GRPH-327): `self-signed` (overlap found),
    # `independent` (someone else's fingerprints are on the work), or `unverifiable`
    # (nothing records who built it). `self_signed: false` alone conflated the last two,
    # so a verdict on work with no recorded author read as an independent review.
    separation: Mapped[str] = mapped_column(String, default="", nullable=False,
                                            server_default="")
    # The item keys that triggered the flag, so "self-signed" is checkable rather than an
    # accusation with nothing behind it.
    self_signed_items: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PrdVersion(Base):
    __tablename__ = "prd_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prd_id: Mapped[str] = mapped_column(ForeignKey("prds.id"))
    version: Mapped[str] = mapped_column(String)
    date: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    # THE agreed spec, not just another snapshot (AL-239). Set when a PRD is approved —
    # which since PRD-15 means its grill concluded, so the baseline freezes at the moment
    # the spec was demonstrably interrogated rather than when someone clicked something.
    #
    # Ordinary snapshots pile up freely; a baseline is the fixed point every later
    # judgement measures against. Post-approval edits stay legal and deliberately do NOT
    # move it — that is what makes drift measurable. If an edit moved the baseline, drift
    # would be definitionally zero and PRD-12 would report health while measuring nothing.
    is_baseline: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # The grill outcomes as they stood at approval, so a deferral is visible ON the
    # baseline and later drift on that point reads as expected rather than as a surprise.
    grill_outcomes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Rebaselining (AL-241). Baselines form an APPEND-ONLY chain: N+1 never destroys N,
    # because learning, scope change and laundering are content-identical from the end
    # state. Only sequencing and a stated reason separate them, which is why both are
    # mandatory rather than nice to have.
    #
    # NULL on the first baseline — it supersedes nothing.
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("prd_versions.id"), nullable=True
    )
    # learning | scope-change | correction. Typed so a chain can be read at a glance:
    # a run of `correction` is a spec that was wrong, a run of `scope-change` is a
    # project that moved, and the difference matters to whoever reads it later.
    rebaseline_reason_type: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    # The requester's OWN WORDS, not a paraphrase — the `capture_grill_decisions`
    # preservation principle. A reason written by the agent that wanted the rebaseline
    # is the thing most likely to be lost when a context window clears.
    rebaseline_reason: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    requested_by: Mapped[str] = mapped_column(String, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    prd: Mapped[Prd] = relationship(back_populates="versions")


class ProjectTagHistory(Base):
    """A tag a project used to hold, so keys rendered under it keep resolving (PRD-13).

    One row per rename — not one per entity — so this never grows with the corpus. The
    tag is the primary key because reuse is forbidden per deployment (AL-258): letting
    two projects hold `AL` at different times would make `AL-12` ambiguous forever, and
    no ordering by date can fix that once both have an item numbered 12.
    """

    __tablename__ = "project_tag_history"

    tag: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    held_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    held_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LegacyEntityKey(Base):
    """An id issued before project tags existed, kept resolvable forever (PRD-13).

    Seeded once by the tag backfill migration and never appended to again. It exists
    because the old ids used `R-` and `PRD-` as *entity-kind* markers rather than
    project tags, so tag history cannot express them — `PRD-12` was never a project
    tagged `PRD`. Everything issued after the backfill resolves by grammar or by tag
    history instead, so this table's size is fixed at whatever the deployment had.

    `entity_id` points at a FROZEN id, so no chain can ever form here: a second retag
    doesn't invalidate these rows, because the thing they point at cannot move.
    """

    __tablename__ = "legacy_entity_keys"

    old_key: Mapped[str] = mapped_column(String, primary_key=True)  # e.g. AL-12, PRD-4
    entity_type: Mapped[str] = mapped_column(String)  # item | request | prd
    entity_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)


class Link(Base):
    """Typed relationship between two items/requests (dependency/code/semantic/tag)."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    a: Mapped[str] = mapped_column(String)
    b: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, default="dependency")
    confidence: Mapped[float] = mapped_column(default=1.0)
    reason: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeNode(Base):
    """A described unit of the codebase — module / file / symbol — with an agent- or
    LLM-written summary, embedded for semantic search over structure.

    The *producer* is normally the external coding agent (it has the repo in context);
    Graphban's connected LLM is the *consumer* that reasons over what's stored. Keyed
    by (project_id, path) so a re-describe upserts. `content_hash` + `fresh` are the
    staleness handle: when a file's hash changes, the agent re-describes and the node is
    marked fresh again; a `prune` pass marks nodes it no longer saw as stale (fresh=False).
    """

    __tablename__ = "code_nodes"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # cn_...
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    path: Mapped[str] = mapped_column(String)  # app/services/items.py  or  ...items.py::create_item
    kind: Mapped[str] = mapped_column(String, default="file")  # module | file | symbol
    name: Mapped[str] = mapped_column(String, default="")  # short label
    lang: Mapped[str] = mapped_column(String, default="")  # python | ts | ...
    summary: Mapped[str] = mapped_column(Text, default="")  # what it is / does / owns
    content_hash: Mapped[str] = mapped_column(String, default="")  # source hash for staleness
    fresh: Mapped[bool] = mapped_column(Boolean, default=True)  # verified this describe pass
    embedding = mapped_column(EmbeddingType(settings.embed_dim), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_code_node_path"),)


class CodeEdge(Base):
    """A directed, typed relation between two code paths — imports / calls / owns /
    tested_by / references. Stored by *path* (not node id) so an edge may point at a node
    that hasn't been described yet (a dangling target is still information)."""

    __tablename__ = "code_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    src: Mapped[str] = mapped_column(String)  # path
    dst: Mapped[str] = mapped_column(String)  # path
    type: Mapped[str] = mapped_column(String, default="imports")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "src", "dst", "type", name="uq_code_edge"),
    )


class CodeRef(Base):
    """A directed link from a tracker item OR request to a code path — the bridge between
    the *work* (ideas/bugs/features) and the *code graph*. Distinct from an item's free-text
    `touchpoints` (fuzzy, glob-matched live): a CodeRef is an explicit, typed, curated edge to
    a specific path. Stored by path so it can point at a not-yet-described node."""

    __tablename__ = "code_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    ref_type: Mapped[str] = mapped_column(String)  # item | request
    ref_id: Mapped[str] = mapped_column(String)  # AL-12 / R-31
    path: Mapped[str] = mapped_column(String)  # code node path (may be undescribed)
    relation: Mapped[str] = mapped_column(String, default="affects")  # affects|implements|fixes|tests|references
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "ref_type", "ref_id", "path", "relation", name="uq_code_ref"),
    )


class Milestone(Base):
    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    phase: Mapped[str] = mapped_column(String)  # mvp | post | later
    title: Mapped[str] = mapped_column(String)
    tag: Mapped[str] = mapped_column(String, default="")
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class McpToolStat(Base):
    __tablename__ = "mcp_tool_stats"

    tool: Mapped[str] = mapped_column(String, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class PlatformConfig(Base):
    """Per-project platform + integration settings (Phase 5). One row per project."""

    __tablename__ = "platform_config"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    llm_mode: Mapped[str] = mapped_column(String, default="stub")  # legacy: stub | local | cloud
    local_base_url: Mapped[str] = mapped_column(String, default="http://localhost:11434")
    local_model: Mapped[str] = mapped_column(String, default="llama3.1:8b")
    cloud_provider: Mapped[str] = mapped_column(String, default="anthropic")
    cloud_model: Mapped[str] = mapped_column(String, default="claude-opus-4-8")

    # Provider registry (F1 redesign): the active chat provider id + per-provider config
    # (dict keyed by provider id → {api_key, base_url, chat_model, embed_model}). API keys
    # are stored here write-only — never returned raw (see provider_config).
    active_chat_provider: Mapped[str] = mapped_column(String, default="")
    providers: Mapped[dict] = mapped_column(JSON, default=dict)

    # Public sharing (AL-73): a project is publicly readable ONLY when it opts in.
    # The unguessable share_token is how public links address the project, so the
    # raw project_id is never needed (and, in hosted mode, never accepted) — that
    # closes the "name any project_id unauthenticated → cross-tenant read" hole.
    public_share_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    share_token: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)

    github_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    github_account: Mapped[str] = mapped_column(String, default="")
    github_repo: Mapped[str] = mapped_column(String, default="")
    github_scope: Mapped[str] = mapped_column(String, default="")

    gdrive_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    gdrive_account: Mapped[str] = mapped_column(String, default="")
    gdrive_folder: Mapped[str] = mapped_column(String, default="")

    # Local↔cloud hybrid privacy (AL-137, D8): when False, a linked local instance never
    # pushes THIS project's code graph — the summaries describe proprietary source and some
    # teams won't send them off-network. Cloud-side triage/collision-clustering is then weaker.
    sync_graph: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)

    # Spam protection for the public feedback endpoints.
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=20)
    turnstile_sitekey: Mapped[str] = mapped_column(String, default="")  # public; rendered in widget
    turnstile_secret: Mapped[str] = mapped_column(String, default="")  # server-side verify only

    @property
    def turnstile_secret_set(self) -> bool:
        """Whether a secret is configured — surfaced to the UI without leaking it."""
        return bool(self.turnstile_secret)

    @property
    def provider_config(self) -> dict:
        """Per-provider config for the UI — api keys reduced to a `key_set` bool, never raw."""
        out: dict = {}
        for pid, c in (self.providers or {}).items():
            out[pid] = {
                "base_url": c.get("base_url", ""),
                "chat_model": c.get("chat_model", ""),
                "embed_model": c.get("embed_model", ""),
                "key_set": bool(c.get("api_key")),
            }
        return out


class Attachment(Base):
    """A public-uploaded image (bug screenshot) referenced by a feedback request.
    Bytes live in the DB; served public-read by unguessable id."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    content_type: Mapped[str] = mapped_column(String, default="image/png")
    size: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # The project this key's agent writes to by default. NULL = global key (the agent
    # must pass project_id per call, or it falls back to the default project).
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    name: Mapped[str] = mapped_column(String, default="agent key")
    prefix: Mapped[str] = mapped_column(String)  # e.g. al_sk_ab12 for display
    hashed_key: Mapped[str] = mapped_column(String)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # Which roles an agent authenticating with this key may hold (PRD-17 D2). The CEILING,
    # not the assignment: `assign_role` may move an agent within this list and never past it,
    # so a compromised or over-eager client cannot promote itself into `reviewer`.
    # Existing keys are backfilled to all three, so nothing in flight breaks.
    roles: Mapped[list] = mapped_column(JSON, default=list)
    # Tags keys the Fleet view minted for one wave, so "End wave" revokes exactly those and
    # never a key a human made by hand (PRD-17 D-g). NULL = hand-minted, never swept.
    fleet_wave: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Lifecycle (AL-72): NULL expires_at = non-expiring; revoked is a soft kill switch.
    # verify_api_key rejects a key that is past expiry or revoked.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)

    user: Mapped[User] = relationship(back_populates="api_keys")


class SyncState(Base):
    """Per-PRD last-synced snapshot for the Drive/filesystem sync — powers conflict
    detection (flag when both sides changed since last sync)."""

    __tablename__ = "sync_state"

    prd_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_name: Mapped[str] = mapped_column(String, default="")
    last_hash: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeSyncState(Base):
    """Per-project manifest of the code graph last pushed to the linked cloud tenant (AL-139):
    `manifest` is {path: content_hash} of what the cloud confirmed — the diff base for an
    incremental, resumable push. Updated per confirmed batch so an interrupted push resumes."""

    __tablename__ = "code_sync_state"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncLink(Base):
    """Instance-wide link to a cloud tenant (AL-141) — the web-managed counterpart of the
    `graphban link` CLI. A self-hosted box pushes its code graph to `cloud_url`,
    authenticating with a `sync`-scoped org key held encrypted at rest (`api_key_enc`, the
    same Fernet-at-rest as provider BYOK keys). Singleton (`id="instance"`); a blank
    `cloud_url` means not linked — a pure local-only tool that never reaches out (the D2
    default). When present it OVERRIDES the env `SYNC_CLOUD_URL`/`SYNC_API_KEY`, so linking
    from the UI wins over a baked-in env link."""

    __tablename__ = "sync_link"

    id: Mapped[str] = mapped_column(String, primary_key=True, default="instance")
    cloud_url: Mapped[str] = mapped_column(String, default="")
    api_key_enc: Mapped[str] = mapped_column(String, default="")  # Fernet token; never returned raw
    org: Mapped[str] = mapped_column(String, default="")  # optional label shown in the UI
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IdempotencyKey(Base):
    """Maps an agent-supplied idempotency key to the resource a create tool produced,
    so a retried call returns the original resource instead of a duplicate."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    tool: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Append-only audit log: who did what, when (AL-43). One row per accepted
    mutation, written at the boundary (MCP dispatcher + REST) so the actor's
    identity is captured — the ledger in Graphban."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_type: Mapped[str] = mapped_column(String)  # user | apikey | system
    actor_id: Mapped[str] = mapped_column(String, default="")
    actor_label: Mapped[str] = mapped_column(String, default="")  # display name/handle
    surface: Mapped[str] = mapped_column(String)  # mcp | rest | public
    action: Mapped[str] = mapped_column(String)  # e.g. create_item, revoke_api_key
    target_type: Mapped[str] = mapped_column(String, default="")
    target_id: Mapped[str] = mapped_column(String, default="")
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AssistantThread(Base):
    """A conversation with the in-app AI assistant, scoped to one item or PRD (AL-174).

    Threads give brainstorming continuity across sessions and a durable record of what
    the assistant proposed and whether it was applied. `provider`/`model` pin which model
    drives the thread (AL-176 model picker)."""
    __tablename__ = "assistant_threads"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # th_...
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String)  # item | prd
    entity_id: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    # Token metering per conversation (AL-179), accumulated across turns.
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="AssistantMessage.seq")


class AssistantMessage(Base):
    """One ordered turn in an AssistantThread. Carries plain content plus the structured
    tool-calling record (calls the model made, results fed back) and any staged
    proposed-actions with their approval status (AL-177 fills in the approval flow)."""
    __tablename__ = "assistant_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # msg_...
    thread_id: Mapped[str] = mapped_column(ForeignKey("assistant_threads.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # ordering within the thread
    role: Mapped[str] = mapped_column(String)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)       # [{id, name, input}]
    tool_results: Mapped[list] = mapped_column(JSON, default=list)     # [{id, content, is_error}]
    proposed_actions: Mapped[list] = mapped_column(JSON, default=list) # staged writes + status (AL-177)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    thread: Mapped[AssistantThread] = relationship(back_populates="messages")


class AssistantProposedAction(Base):
    """A write the assistant proposed but has NOT executed (AL-177). Propose-then-approve:
    a write tool call is staged here as `pending`; the human `apply`s (executes + audits,
    capturing `prior_value` for reversibility) or `reject`s. Nothing mutates until approval,
    so prompt-injected content can at most propose a rejected action."""
    __tablename__ = "assistant_proposed_actions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # pa_...
    thread_id: Mapped[str] = mapped_column(ForeignKey("assistant_threads.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    tool: Mapped[str] = mapped_column(String)          # the write tool name
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String, default="")  # human-readable diff/summary
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending|applied|rejected|reverted
    prior_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # captured on apply for revert
    provider: Mapped[str] = mapped_column(String, default="")  # origin attribution: assistant:<provider>
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
