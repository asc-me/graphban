"""Seed the database with the Graphban design prototype's dataset.

Idempotent: only runs when the DB has no users. All seeded users share the
password below (dev convenience — change for any real deployment).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tagging
from app.embeddings import get_embedder
from app.models import (
    Item,
    Link,
    McpToolStat,
    Membership,
    MemoryShard,
    Milestone,
    PlatformConfig,
    Prd,
    PrdVersion,
    Project,
    Request,
    User,
)
from app.security.passwords import hash_password

# bandit B105: the local demo seed's documented login, never used outside `seed`.
SEED_PASSWORD = "graphban"  # nosec B105

PROJECTS = [
    dict(id="core", name="Core Platform", tag="CP", accent="#c6f24e", visibility="private",
         description="Graphban itself — the agent-native tracker + memory core.",
         share_global_memory=True, auto_extract=True, mcp_enabled=True, embed_model="text-embedding-3-small"),
    dict(id="web", name="Web App", tag="WA", accent="#7ca2ff", visibility="private",
         description="Marketing site + hosted dashboard for graphban.dev.",
         share_global_memory=True, auto_extract=True, mcp_enabled=False, embed_model="text-embedding-3-small"),
    dict(id="infra", name="Infra", tag="INFR", accent="#e0b34a", visibility="private",
         description="Deploy, CI, and self-host packaging.",
         share_global_memory=False, auto_extract=False, mcp_enabled=True, embed_model="nomic-embed-text (Ollama)"),
]

USERS = [
    dict(id="u1", name="Alex Cain", handle="ascme", email="alex@ascme-labs.com", avatar="#a78bfa",
         initials="AC", role="owner", access=dict(core="write", web="write", infra="write")),
    dict(id="u2", name="Dana Ruiz", handle="dev_ren", email="dana@ascme-labs.com", avatar="#7ca2ff",
         initials="DR", role="admin", access=dict(core="write", web="read", infra="none")),
    dict(id="u3", name="Ops Lee", handle="ops_lee", email="ops@ascme-labs.com", avatar="#e0b34a",
         initials="OL", role="member", access=dict(core="read", web="none", infra="write")),
    dict(id="u4", name="Kate Ito", handle="a11y_kate", email="kate@ascme-labs.com", avatar="#ff8f8f",
         initials="KI", role="member", access=dict(core="read", web="read", infra="none")),
]

R = lambda name, handle, avatar: {"name": name, "handle": handle, "avatar": avatar}  # noqa: E731
ALEX = R("Alex Cain", "ascme", "#a78bfa")

ITEMS = [
    dict(id="AL-12", status="in_progress", title="Wire AI chat sidebar to project + memory context",
         tags=["ai", "frontend"], effort=5, date="Jul 19", reporter=ALEX,
         description="Stream project state and top-k memory shards into the chat context window. Handle token budget + truncation of low-relevance shards.",
         pr=dict(number=142, title="feat: chat context assembler", branch="feat/chat-context", state="open", additions=418, deletions=22, checks="passing", ago="updated 3h ago")),
    dict(id="AL-08", status="in_progress", title="Set up pgvector memory store & embedding pipeline",
         tags=["backend", "memory"], effort=5, date="Jul 18", reporter=ALEX,
         description="Postgres + pgvector. Chunk item bodies, embed on write, expose search_memory over cosine similarity. Fallback to SQLite bm25 when pgvector absent.",
         pr=dict(number=138, title="feat: pgvector memory store", branch="feat/pgvector-store", state="open", additions=906, deletions=14, checks="pending", ago="updated 1h ago")),
    dict(id="AL-15", status="next", title="MCP: implement create_item, update_item, search_items",
         tags=["mcp", "api"], effort=3, date="Jul 21", reporter=ALEX,
         description="First three write/read tools over the MCP endpoint. Auth via API key. Return compact JSON the agent can chain.", pr=None),
    dict(id="AL-16", status="next", title="JWT + API keys for MCP auth",
         tags=["backend", "auth"], effort=3, date="Jul 22", reporter=R("Dana Ruiz", "dev_ren", "#7ca2ff"),
         description="Short-lived JWT for the web app, long-lived scoped API keys for agents. Per-key rate limits.", pr=None),
    dict(id="AL-11", status="review", title="Docker Compose one-command self-host",
         tags=["infra", "devx"], effort=3, date="Jul 17", reporter=R("Ops Lee", "ops_lee", "#e0b34a"),
         description="Single `docker compose up` brings up API, Postgres+pgvector, and web. Seed script optional. Health checks + volumes for persistence.",
         pr=dict(number=131, title="chore: compose + healthchecks", branch="infra/compose", state="review", additions=212, deletions=8, checks="passing", ago="updated 1d ago")),
    dict(id="AL-04", status="done", title="Build the linear list view",
         tags=["frontend", "ui"], effort=8, date="Jul 14", reporter=ALEX,
         description="Single scrolling stream ordered by priority + recency. Drag reorder, inline status, quick filters. Shipped.",
         pr=dict(number=119, title="feat: linear list view", branch="feat/linear-list", state="merged", additions=1240, deletions=96, checks="passing", ago="merged Jul 14")),
    dict(id="AL-19", status="backlog", title="Public feedback form (embeddable)",
         tags=["frontend", "feedback"], effort=2, date="—", reporter=R("Sam Okoro", "solo_sam", "#4fd6c4"),
         description="Standalone embeddable form that drops submissions into the triage queue with source metadata.", pr=None),
    dict(id="AL-21", status="backlog", title="Auto-duplicate detection for requests",
         tags=["ai", "feedback"], effort=5, date="—", reporter=ALEX,
         description="On submit, embed the request and surface likely duplicates / related items above a similarity threshold before it enters triage.", pr=None),
    dict(id="AL-22", status="blocked", title="Fix drag-reorder jitter on Safari",
         tags=["frontend", "bug"], effort=1, date="Jul 20", reporter=R("Kate Ito", "a11y_kate", "#ff8f8f"),
         blocker="waiting on upstream dnd patch",
         description="Rows flicker mid-drag under Safari due to dragenter/dragover ordering.",
         pr=dict(number=140, title="fix: safari dragover preventDefault", branch="fix/safari-drag", state="draft", additions=34, deletions=11, checks="failing", ago="updated 5h ago")),
]

REQUESTS = [
    dict(id="R-31", type="feature", title="Two-way GitHub issue sync", by="@m_arc", ago="2d ago", votes=12, status="triaging", linked_to=None),
    dict(id="R-29", type="bug", title="SQLite fallback crashes on first migrate", by="@ops_lee", ago="3d ago", votes=5, status="triaging", linked_to=None),
    dict(id="R-33", type="enhancement", title="Full keyboard-only navigation", by="@a11y_kate", ago="1d ago", votes=7, status="linked", linked_to="AL-04"),
    dict(id="R-27", type="bug", title="Memory search returns stale embeddings after edit", by="@dev_ren", ago="4d ago", votes=4, status="linked", linked_to="AL-08"),
    dict(id="R-35", type="feedback", title="Love the skinny flow — please add saved filters", by="@solo_sam", ago="6h ago", votes=3, status="new", linked_to=None),
]

LINKS = [
    dict(a="AL-12", b="AL-08", type="dependency", confidence=0.95, reason="AI chat depends on the search_memory tool"),
    dict(a="AL-08", b="R-27", type="code", confidence=0.91, reason="shared memory/store.py · re-index on edit"),
    dict(a="AL-08", b="AL-21", type="semantic", confidence=0.84, reason="embeddings.py · dup detection reuses vectors"),
    dict(a="AL-11", b="R-29", type="code", confidence=0.80, reason="migrate.py · SQLite fallback path"),
    dict(a="AL-15", b="R-31", type="semantic", confidence=0.78, reason="MCP api surface · GitHub sync rides tools"),
    dict(a="AL-22", b="R-33", type="code", confidence=0.72, reason="LinearList.tsx · drag + focus handling"),
    dict(a="AL-15", b="AL-12", type="dependency", confidence=0.69, reason="chat calls create_item / search_items"),
    dict(a="AL-21", b="R-35", type="tag", confidence=0.61, reason="tag:feedback · saved-filter reuse"),
]

MILESTONES = {
    "mvp": [
        ("Core linear tracker + drag reorder", "frontend", True),
        ("Agent memory store (pgvector)", "backend · in progress", False),
        ("AI chat sidebar with project context", "ai · in progress", False),
        ("First 8 MCP tools stable", "api", True),
        ("Feature / bug request queue", "frontend", True),
        ("Public embeddable feedback form", "frontend", True),
        ("Docker Compose one-command self-host", "infra · review", False),
        ("SQLite fallback for local-first", "backend", True),
    ],
    "post": [
        ("Two-way GitHub issue sync", "from R-31 · 12 votes", False),
        ("Public roadmap + community voting", "community", False),
        ("Advanced AI automations", "ai", False),
        ("Agent UI Forge + more templates", "extensibility", False),
    ],
    "later": [
        ("Multi-tenant hosted tiers (Pro / Team)", "business", False),
        ("Mobile-optimized responsive pass", "frontend", False),
        ("Local Ollama embedding option", "ai · privacy", False),
    ],
}

# Seed starting call counts from the design so the MCP Tools page looks lived-in;
# real MCP calls increment from here.
MCP_STATS = {
    "create_item": 2100, "update_item": 1400, "search_items": 3800, "search_memory": 5200,
    "add_memory": 980, "get_backlog": 640, "get_item_details": 1100, "suggest_next": 420,
    "link_items": 210, "extract_lessons": 0, "generate_digest": 0,
}

PRDS = [
    dict(id="PRD-1", title="Graphban Core MVP", status="approved", version="v1.0", updated="Jul 20",
         linked=["AL-04", "AL-08", "AL-12", "AL-15"],
         body="# Graphban Core MVP\n\n## Overview\nAgent-native tracker combining persistent memory with a skinny linear tracker and native MCP tools.\n\n## Goals\n- Single source of truth for dev items, decisions, and feedback\n- Let agents read/write context via MCP\n- Reduce context loss across projects\n\n## Key Features\n- Linear tracker with drag reorder\n- pgvector memory + semantic search\n- Feature/bug request queue\n\n## Success Metrics\n- Daily usage by creator on 1-2 projects\n- 8+ stable MCP tools\n\n## Risks\n- Embedding model choice (local vs API)\n- Self-host complexity",
         versions=[
             dict(version="v1.0", date="Jul 20", note="Approved for build. Locked MVP scope."),
             dict(version="v0.3", date="Jul 12", note="Added success metrics and MCP tool list."),
             dict(version="v0.1", date="Jul 05", note="Initial draft from kickoff notes."),
         ]),
    dict(id="PRD-2", title="Memory & Semantic Recall", status="review", version="v0.4", updated="Jul 21",
         linked=["AL-08", "AL-21"],
         body="# Memory & Semantic Recall\n\n## Overview\nPersistent memory shards, embedded on write, searchable via pgvector cosine similarity.\n\n## Goals\n- Auto-extract decisions on item completion\n- Sub-200ms recall over the project corpus\n\n## Open Questions\n- Chunk size and overlap\n- When to prune low-relevance shards",
         versions=[
             dict(version="v0.4", date="Jul 21", note="Added pruning open question."),
             dict(version="v0.2", date="Jul 16", note="Defined embedding pipeline."),
         ]),
    dict(id="PRD-3", title="Public Roadmap & Feedback SDK", status="draft", version="v0.2", updated="Jul 19",
         linked=["AL-19"],
         body="# Public Roadmap & Feedback SDK\n\n## Overview\nEmbeddable, themeable feedback components and a read-only public roadmap.\n\n## Non-Goals\n- Full analytics suite\n- Auth for public viewers",
         versions=[
             dict(version="v0.2", date="Jul 19", note="Switched from iframe to components."),
             dict(version="v0.1", date="Jul 15", note="First draft."),
         ]),
]

SHARDS = [
    dict(id="m1", scope="global", source="from AL-08", item_id=None,
         text="Decided: use pgvector over a separate vector DB to keep self-host to one Postgres container."),
    dict(id="m2", scope="global", source="global", item_id=None,
         text="Convention: all MCP tools are snake_case verb-first (create_item, search_memory)."),
    dict(id="m3", scope="item", source="from AL-22", item_id="AL-22",
         text="Learning: Safari drag events need explicit preventDefault on dragover or rows flicker."),
    dict(id="m4", scope="global", source="global", item_id=None,
         text="User preference: dark-only UI, spacious/minimal density, monospace for all metadata."),
    dict(id="m5", scope="item", source="from AL-11", item_id="AL-11",
         text="Deploy target locked: one command — `docker compose up` — must bring the full stack online."),
]


def seed(db: Session) -> bool:
    if db.scalar(select(User).limit(1)) is not None:
        return False

    pw = hash_password(SEED_PASSWORD)

    # Flush in FK-dependency order: parents before children. (A bare ForeignKey
    # column without a relationship() doesn't order the unit of work, and Postgres
    # enforces FKs immediately — unlike SQLite.)
    for p in PROJECTS:
        db.add(Project(**p))
    db.flush()

    for u in USERS:
        fields = {k: v for k, v in u.items() if k not in ("access", "role")}
        db.add(User(password_hash=pw, **fields))
    db.flush()
    for u in USERS:
        for pid, level in u["access"].items():
            db.add(Membership(user_id=u["id"], project_id=pid, role=u["role"], access=level))

    for order, it in enumerate(ITEMS):
        db.add(Item(project_id="core", sort_order=order,
                    number=tagging.legacy_number(it["id"]), **it))
    db.flush()

    for r in REQUESTS:
        db.add(Request(project_id="core", number=tagging.legacy_number(r["id"]), **r))

    embedder = get_embedder()
    for s in SHARDS:
        db.add(MemoryShard(project_id="core", fresh=False, embedding=embedder.embed(s["text"]), **s))

    for link in LINKS:
        db.add(Link(project_id="core", **link))
    order = 0
    for phase, rows in MILESTONES.items():
        for title, tag, done in rows:
            db.add(Milestone(project_id="core", phase=phase, title=title, tag=tag, done=done, sort_order=order))
            order += 1
    for tool, calls in MCP_STATS.items():
        db.add(McpToolStat(tool=tool, calls=calls))

    # Default to the offline stub so chat works with no external services; GitHub
    # shown connected per the design. Switch mode from the Platform Settings view.
    db.add(PlatformConfig(
        project_id="core", llm_mode="stub",
        local_base_url="http://localhost:11434", local_model="llama3.1:8b",
        cloud_provider="anthropic", cloud_model="claude-opus-4-8",
        github_connected=True, github_account="ascme-labs",
        github_repo="ascme-labs/graphban", github_scope="repo · read/write",
        gdrive_connected=False,
        # The demo roadmap is meant to be public; a stable token keeps the embed link
        # working. Freshly-created projects stay private until they opt in (AL-73).
        # bandit B106: a share token for the PUBLIC demo roadmap, public by design.
        public_share_enabled=True, share_token="demo-core-roadmap",  # nosec B106
    ))

    for p in PRDS:
        versions = p["versions"]
        fields = {k: v for k, v in p.items() if k != "versions"}
        db.add(Prd(project_id="core", number=tagging.legacy_number(p["id"]), **fields))
    db.flush()
    for p in PRDS:
        # Design lists versions newest-first; insert oldest-first so ids are chronological
        # (the relationship orders by desc(id)). Only the latest snapshot carries the body.
        latest = p["versions"][0]
        for ver in reversed(p["versions"]):
            db.add(PrdVersion(prd_id=p["id"], body=(p["body"] if ver is latest else ""), **ver))

    db.commit()
    return True
