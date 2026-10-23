"""Derived architecture topology for the Stack Build view (GRPH-1034 / PRD-P51 §2).

Every node and edge is read from the running system. Nothing is hand-drawn. Absences
are first-class (`not_configured`, `unknown`, empty-state messages) — never a healthy
default. Instance-wide facts (web/api/db/email) are labelled separately from
project-scoped ones (AI plane, Drive, Linear), so one project's model cannot read as
the whole system.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.mcp_server import LIVE_TOOL_COUNT
from app.providers import registry as provider_registry
from app.services import fleet as fleet_svc
from app.services import instance_update
from app.services import linear as linear_svc
from app.services import platform as platform_svc
from app.services.memory import judge_status
from app.version import __version__

logger = logging.getLogger("graphban.stack")

# Liveness vocabulary. Three primary states, plus decider/email extras named in the PRD.
LIVENESS_REACHABLE = "reachable"
LIVENESS_UNREACHABLE = "unreachable"
LIVENESS_UNKNOWN = "unknown"
LIVENESS_NOT_CONFIGURED = "not_configured"
LIVENESS_UNCALIBRATED = "uncalibrated"  # configured but not adjudicating

RING_CODE = 1
RING_INFRA = 2
RING_EXTERNAL = 3

HOST_COMPOSE = "compose"
HOST_AGENTS = "agent_hosts"
HOST_USERS = "user_machines"
HOST_EXTERNAL = "external"


def alembic_head(db: Session) -> str | None:
    """The revision the live database is stamped at, or None when unknown.

    Asks the `alembic_version` table — the same source `scripts/graphban_upgrade.py`
    trusts — never the on-disk migration tree. SQLite tests that never ran Alembic
    have no row; that is absence, not head `0000`.
    """
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
    except Exception:  # noqa: BLE001 — missing table is absence
        logger.debug("alembic_version unreadable", exc_info=True)
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _web_bundle_sha() -> str | None:
    """Sha stamped into the served SPA (`version.txt`), when the API can see it."""
    candidates = []
    spa = getattr(settings, "spa_dir", "") or ""
    if spa:
        candidates.append(Path(spa) / "version.txt")
    # Dev / native layouts next to the backend package.
    here = Path(__file__).resolve().parents[3]  # repo root when running from source
    candidates.append(here / "web" / "dist" / "version.txt")
    candidates.append(here / "web" / "version.txt")
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            continue
    return None


def _db_probe(db: Session) -> tuple[str, dict[str, Any]]:
    """`(liveness, facts)` for the datastore. pgvector is an extension inside Postgres,
    never a peer box; SQLite is an in-process library and says so."""
    facts: list[dict[str, str]] = []
    dialect = db.get_bind().dialect.name if db.get_bind() is not None else "unknown"
    try:
        db.execute(text("SELECT 1"))
        live = LIVENESS_REACHABLE
    except Exception:  # noqa: BLE001
        live = LIVENESS_UNREACHABLE
        facts.append({
            "key": "db", "value": "down",
            "provenance": "SELECT 1 (aggregator)",
        })
        return live, {"facts": facts, "engine": dialect, "extensions": []}

    facts.append({
        "key": "db", "value": "ok",
        "provenance": "SELECT 1 (aggregator)",
    })
    head = alembic_head(db)
    facts.append({
        "key": "schema_revision",
        "value": head if head is not None else "unknown",
        "provenance": "alembic_version.version_num",
    })

    extensions: list[str] = []
    if dialect.startswith("postgres"):
        try:
            rows = db.execute(text(
                "SELECT extname FROM pg_extension WHERE extname = 'vector'"
            )).all()
            if rows:
                extensions.append("pgvector")
                facts.append({
                    "key": "extension.pgvector", "value": "installed",
                    "provenance": "pg_extension",
                })
            else:
                facts.append({
                    "key": "extension.pgvector", "value": "absent",
                    "provenance": "pg_extension",
                })
        except Exception:  # noqa: BLE001
            facts.append({
                "key": "extension.pgvector", "value": "unknown",
                "provenance": "pg_extension (query failed)",
            })
    else:
        facts.append({
            "key": "engine", "value": dialect,
            "provenance": "sqlalchemy dialect",
        })
        facts.append({
            "key": "note",
            "value": "SQLite is an in-process library, not a peer deployable",
            "provenance": "model rule",
        })

    return live, {"facts": facts, "engine": dialect, "extensions": extensions}


def _cred_liveness(state: str | None) -> str:
    if state == "valid":
        return LIVENESS_REACHABLE
    if state == "unreachable":
        return LIVENESS_UNREACHABLE
    if state == "pending_validation":
        return LIVENESS_UNKNOWN
    return LIVENESS_UNKNOWN


# Graphban's own deployables, each with source paths unique to it. Ownership is relative
# to the project being viewed: a box is "code we write" only when THIS project's code map
# has described one of its markers. For every other project the same box is a tool it
# runs to build its code, not code it wrote — the map used to call all four "ours"
# everywhere. Markers are specific files/packages, never `backend/app` or `web/src`,
# which half the FastAPI/Vite repos in the world also have.
GRAPHBAN_SOURCES: dict[str, dict[str, Any]] = {
    "api": {"package": "graphban-api",
            "markers": ("backend/app/mcp_server.py", "backend/app/services/stack.py")},
    "web": {"package": "graphban-web",
            "markers": ("web/src/features/stack", "web/src/lib/graph")},
    "fleet": {"package": "graphban-fleet", "markers": ("fleet/src/gbfleet",)},
    "gbagent": {"package": "graphban-fleet", "markers": ("fleet/src/gbagent",)},
    "cli": {"package": "graphban-cli", "markers": ("cli/src/gban",)},
}

OWNED = "owned"
NOT_OWNED = "not_owned"
OWNERSHIP_UNKNOWN = "unknown"


def source_ownership(db: Session, project_id: str) -> dict[str, tuple[str, str]]:
    """`{node_id: (state, matched_marker)}` for each Graphban deployable.

    A project with no code map at all is `unknown`, not `not_owned`: nothing was looked
    at. It still draws outside ring 1, because one project in an instance holds
    Graphban's source and every other one runs it.
    """
    from app.models import CodeNode

    try:
        has_map = db.scalar(
            select(CodeNode.id).where(CodeNode.project_id == project_id).limit(1)
        ) is not None
    except Exception:  # noqa: BLE001 — an unreadable map is unknown, not "not ours"
        logger.debug("code map unreadable for ownership", exc_info=True)
        has_map = False
    out: dict[str, tuple[str, str]] = {}
    for node_id, src in GRAPHBAN_SOURCES.items():
        if not has_map:
            out[node_id] = (OWNERSHIP_UNKNOWN, "")
            continue
        hit = ""
        for marker in src["markers"]:
            found = db.scalar(
                select(CodeNode.path).where(
                    CodeNode.project_id == project_id,
                    or_(
                        CodeNode.path == marker,
                        CodeNode.path.startswith(marker + "/", autoescape=True),
                        CodeNode.path.startswith(marker + "::", autoescape=True),
                    ),
                ).limit(1)
            )
            if found is not None:
                hit = marker
                break
        out[node_id] = (OWNED, hit) if hit else (NOT_OWNED, "")
    return out


def _ownership_fact(node_id: str, owner: tuple[str, str]) -> dict[str, str]:
    state, marker = owner
    package = GRAPHBAN_SOURCES[node_id]["package"]
    if state == OWNED:
        value = f"{package} — this project writes it ({marker} is in its code map)"
    elif state == NOT_OWNED:
        value = f"{package} — a tool this project runs; its source is not in this project's code map"
    else:
        value = f"{package} — unknown: this project has no code map to check"
    return _fact("source", value, "code map (code_nodes) vs Graphban source markers")


def _fact(key: str, value: Any, provenance: str) -> dict[str, str]:
    if value is None or value == "":
        rendered = "unknown"
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    return {"key": key, "value": rendered, "provenance": provenance}


def topology(db: Session, project_id: str) -> dict[str, Any]:
    """Build the derived Stack map for one project the caller can already read."""
    running = instance_update.running()
    db_live, db_meta = _db_probe(db)
    web_sha = _web_bundle_sha()
    schema = alembic_head(db)

    cfg = platform_svc.get_config(db, project_id)
    creds = platform_svc.list_credentials(db, platform_svc.scope_for(db, project_id))
    catalog = list(provider_registry.PROVIDERS)
    try:
        from app.providers import llm_meter
        loads = llm_meter.load_summary(db, project_id=project_id)
    except Exception:  # noqa: BLE001 — loads are optional colour
        logger.debug("model-loads unavailable", exc_info=True)
        loads = None

    decider = platform_svc.decider_health(db, project_id)
    try:
        judge = judge_status(db, project_id)
    except Exception:  # noqa: BLE001
        logger.debug("judge_status unavailable", exc_info=True)
        judge = {"judge": "off", "decider_configured": False, "reason": "unavailable"}

    try:
        roster = fleet_svc.fleet_status(db, project_id)
    except Exception:  # noqa: BLE001
        logger.exception("fleet_status failed for stack topology")
        roster = None

    try:
        linear = linear_svc.integration_status(db)
    except Exception:  # noqa: BLE001
        logger.debug("linear status unavailable", exc_info=True)
        linear = {"linked": False}

    smtp_configured = bool(settings.smtp_host)

    owner = source_ownership(db, project_id)

    def ring_of(node_id: str) -> int:
        return RING_CODE if owner[node_id][0] == OWNED else RING_INFRA

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    empties: list[dict[str, str]] = []

    # ── Instance deployables (compose / Railway) ─────────────────────────────
    api_facts = [
        _fact("version", running.get("version") or __version__, "instance_update.running"),
        _fact("git_sha", running.get("git_sha") or settings.resolved_git_sha, "instance_update.running"),
        _fact("service", "graphban-api", "constant"),
        _fact("mcp_tools", LIVE_TOOL_COUNT, "mcp_server.LIVE_TOOL_COUNT"),
        _fact("embed_provider", settings.embed_provider, "settings.embed_provider"),
        _fact("embed_ok", settings.embed_provider != "stub", "settings.embed_provider != stub"),
        _ownership_fact("api", owner["api"]),
    ]
    nodes.append({
        "id": "api",
        "label": "api",
        "title": "graphban-api",
        "kind": "deployable",
        "ring": ring_of("api"),
        "scope": "instance",
        "host": HOST_COMPOSE,
        "liveness": LIVENESS_REACHABLE,  # this process answered
        "surfaces": ["REST", "MCP", "sync engine", "provider-resolution"],
        "extensions": [],
        "facts": api_facts,
    })

    web_facts = [
        _fact("bundle_sha", web_sha, "/version.txt"),
        _fact("role", "React SPA", "model"),
        _ownership_fact("web", owner["web"]),
    ]
    nodes.append({
        "id": "web",
        "label": "web",
        "title": "web (React SPA)",
        "kind": "deployable",
        "ring": ring_of("web"),
        "scope": "instance",
        "host": HOST_COMPOSE,
        # Serving the SPA is nginx/compose; the API can only report the stamped sha.
        "liveness": LIVENESS_REACHABLE if web_sha else LIVENESS_UNKNOWN,
        "surfaces": [],
        "extensions": [],
        "facts": web_facts,
    })

    nodes.append({
        "id": "db",
        "label": "db",
        "title": "Postgres" if str(db_meta.get("engine", "")).startswith("postgres") else "SQLite",
        "kind": "infra",
        "ring": RING_INFRA,
        "scope": "instance",
        "host": HOST_COMPOSE,
        "liveness": db_live,
        "surfaces": [],
        "extensions": list(db_meta.get("extensions") or []),
        "facts": list(db_meta.get("facts") or []),
    })

    edges.append({
        "from": "web", "to": "api", "protocol": "HTTP/JWT", "label": "HTTP/JWT",
    })
    edges.append({
        "from": "api", "to": "db", "protocol": "psycopg :5432",
        "label": "psycopg" if str(db_meta.get("engine", "")).startswith("postgres") else "sqlite",
    })

    # ── Fleet / agent hosts ──────────────────────────────────────────────────
    online = int((roster or {}).get("online") or 0)
    total = int((roster or {}).get("total") or 0)
    agents = list((roster or {}).get("agents") or [])
    # Outposts: group by capabilities.host the way OutpostsView does.
    hosts: dict[str, list[dict]] = {}
    for a in agents:
        caps = a.get("capabilities") or {}
        host = (caps.get("host") or "").strip()
        if not host:
            label = a.get("label") or ""
            if " @" in label:
                host = label.rsplit(" @", 1)[-1].strip()
        key = host or "(unspecified)"
        hosts.setdefault(key, []).append(a)

    fleet_facts = [
        _fact("online", online, "GET /api/fleet"),
        _fact("agents_total", total, "GET /api/fleet"),
        _fact("posture", (roster or {}).get("posture"), "GET /api/fleet"),
        _fact("outpost_hosts", len(hosts), "fleet agents grouped by capabilities.host"),
        _ownership_fact("fleet", owner["fleet"]),
    ]
    if roster is None:
        fleet_live = LIVENESS_UNKNOWN
        empties.append({"id": "fleet", "message": "fleet status unavailable"})
    elif online == 0 and total == 0:
        fleet_live = LIVENESS_NOT_CONFIGURED
        empties.append({"id": "fleet", "message": "no fleet registered"})
    elif online == 0:
        fleet_live = LIVENESS_UNREACHABLE
        empties.append({"id": "fleet", "message": "fleet registered but no agent online"})
    else:
        fleet_live = LIVENESS_REACHABLE

    nodes.append({
        "id": "fleet",
        "label": "gbfleet",
        "title": "gbfleet supervisor (graphban-fleet)",
        "kind": "deployable",
        "ring": ring_of("fleet"),
        "scope": "instance",
        "host": HOST_AGENTS,
        "liveness": fleet_live,
        "surfaces": [],
        "extensions": [],
        "facts": fleet_facts,
        "outposts": [
            {
                "host": h,
                "online": sum(1 for a in alist if a.get("state") != "offline"),
                "agents": len(alist),
            }
            for h, alist in sorted(hosts.items())
        ],
    })
    edges.append({
        "from": "fleet", "to": "api", "protocol": "HTTP + MCP JSON-RPC",
        "label": "HTTP + MCP",
    })

    # gbagent is Graphban's own coding agent: a separate binary in the graphban-fleet
    # package that gbfleet spawns like a vendor CLI. Idle is normal for a spawned
    # child, so none online is unknown, not unreachable.
    def _vendor(a: dict) -> str:
        return str((a.get("capabilities") or {}).get("vendor") or "")

    live_agents = [a for a in agents if a.get("state") != "offline"]
    gbagent_online = sum(1 for a in live_agents if _vendor(a) == "gbagent")
    vendor_online = len(live_agents) - gbagent_online
    nodes.append({
        "id": "gbagent",
        "label": "gbagent",
        "title": "gbagent (graphban-fleet)",
        "kind": "deployable",
        "ring": ring_of("gbagent"),
        "scope": "instance",
        "host": HOST_AGENTS,
        "liveness": LIVENESS_REACHABLE if gbagent_online else LIVENESS_UNKNOWN,
        "surfaces": [],
        "extensions": [],
        "facts": [
            _fact("online", gbagent_online, "GET /api/fleet (capabilities.vendor == gbagent)"),
            _fact("role", "first-party coding agent, spawned by gbfleet", "model"),
            _ownership_fact("gbagent", owner["gbagent"]),
        ],
    })
    edges.append({
        "from": "fleet", "to": "gbagent", "protocol": "spawn local process", "label": "spawn",
    })
    edges.append({
        "from": "gbagent", "to": "api", "protocol": "MCP JSON-RPC", "label": "MCP",
    })

    # Vendor CLIs spawned by fleet — only when one is online. Nobody's project writes
    # claude or grok, so they are never ring 1.
    if vendor_online > 0:
        nodes.append({
            "id": "vendor_agents",
            "label": "vendor agents",
            "title": "Spawned vendor CLIs",
            "kind": "agent",
            "ring": RING_INFRA,
            "scope": "instance",
            "host": HOST_AGENTS,
            "liveness": LIVENESS_REACHABLE,
            "surfaces": [],
            "extensions": [],
            "facts": [
                _fact("online", vendor_online, "GET /api/fleet (capabilities.vendor != gbagent)"),
            ],
        })
        edges.append({
            "from": "fleet", "to": "vendor_agents", "protocol": "spawn local process",
            "label": "spawn",
        })
        edges.append({
            "from": "vendor_agents", "to": "api", "protocol": "MCP JSON-RPC",
            "label": "MCP",
        })

    # ── gban (user machines) — always present as a deployable; liveness unknown
    #    unless we invent a phone-home (we do not).
    nodes.append({
        "id": "cli",
        "label": "gban",
        "title": "gban (graphban-cli)",
        "kind": "deployable",
        "ring": ring_of("cli"),
        "scope": "instance",
        "host": HOST_USERS,
        "liveness": LIVENESS_UNKNOWN,
        "surfaces": [],
        "extensions": [],
        "facts": [
            _fact("note", "runs on user machines; no phone-home in v1", "model"),
            _ownership_fact("cli", owner["cli"]),
        ],
    })
    edges.append({
        "from": "cli", "to": "api", "protocol": "HTTP/JWT", "label": "HTTP/JWT",
    })

    if not any(state == OWNED for state, _ in owner.values()):
        empties.append({
            "id": "own_code",
            "message": "this project's own deployables are not on this map — it draws the "
                       "Graphban instance and tools this project runs to build, none of "
                       "which this project's code map shows it writing",
        })

    # ── External: email (SMTP; item text says Resend) ────────────────────────
    email_facts = [
        _fact("smtp_host", settings.smtp_host or "(unset)", "settings.smtp_host"),
        _fact("smtp_from", settings.smtp_from, "settings.smtp_from"),
    ]
    if smtp_configured:
        email_live = LIVENESS_UNKNOWN  # configured; we do not probe the relay here
    else:
        email_live = LIVENESS_NOT_CONFIGURED
        empties.append({"id": "email", "message": "no email (SMTP) configured"})
    nodes.append({
        "id": "email",
        "label": "email",
        "title": "Email (SMTP)",
        "kind": "external",
        "ring": RING_EXTERNAL,
        "scope": "instance",
        "host": HOST_EXTERNAL,
        "liveness": email_live,
        "surfaces": [],
        "extensions": [],
        "facts": email_facts,
    })
    if smtp_configured:
        edges.append({
            "from": "api", "to": "email", "protocol": "SMTP/HTTPS", "label": "SMTP",
        })

    # ── External: Drive / Linear (project-scoped sync) ───────────────────────
    if cfg.gdrive_connected:
        nodes.append({
            "id": "gdrive",
            "label": "Google Drive",
            "title": "Google Drive sync",
            "kind": "external",
            "ring": RING_EXTERNAL,
            "scope": "project",
            "host": HOST_EXTERNAL,
            "liveness": LIVENESS_UNKNOWN,
            "surfaces": [],
            "extensions": [],
            "facts": [
                _fact("account", cfg.gdrive_account, "GET /api/platform"),
                _fact("folder", cfg.gdrive_folder, "GET /api/platform"),
                _fact("connected", True, "GET /api/platform"),
            ],
        })
        edges.append({
            "from": "api", "to": "gdrive", "protocol": "HTTPS", "label": "HTTPS",
        })
    else:
        empties.append({"id": "gdrive", "message": "no Google Drive sync configured"})

    if linear.get("linked"):
        nodes.append({
            "id": "linear",
            "label": "Linear",
            "title": linear.get("workspace_name") or "Linear",
            "kind": "external",
            "ring": RING_EXTERNAL,
            "scope": "project",
            "host": HOST_EXTERNAL,
            "liveness": LIVENESS_REACHABLE if linear.get("token_set") else LIVENESS_UNKNOWN,
            "surfaces": [],
            "extensions": [],
            "facts": [
                _fact("workspace", linear.get("workspace_name"), "GET /api/linear/status"),
                _fact("token_set", linear.get("token_set"), "GET /api/linear/status"),
                _fact("last_sync_at", linear.get("last_sync_at"), "GET /api/linear/status"),
            ],
        })
        edges.append({
            "from": "api", "to": "linear", "protocol": "HTTPS", "label": "HTTPS",
        })
    else:
        empties.append({"id": "linear", "message": "no Linear sync configured"})

    # ── AI plane (project-scoped, WIRED credentials only) ────────────────────
    wired_kinds = {c["kind"] for c in creds}
    unwired = [p for p in catalog if p["id"] not in wired_kinds]
    # Also treat legacy active_chat_provider / effective as wired even without a Credential row.
    effective = ""
    if cfg.active_chat_provider:
        effective = cfg.active_chat_provider
    elif cfg.llm_mode == "local":
        effective = "ollama"
    elif cfg.llm_mode == "cloud":
        effective = "anthropic"
    else:
        effective = "stub"

    ai_nodes_added = 0
    for cred in creds:
        cid = f"ai:{cred['id']}"
        serves = cred.get("serves") or ""
        roles = []
        if "chat" in serves or serves in ("", "chat", "chat+embed", "all"):
            roles.append("chat")
        if "embed" in serves or "embed" in str(serves):
            roles.append("embed")
        if "decide" in serves or cred.get("is_decider"):
            roles.append("decide")
        if not roles:
            roles = [serves or "provider"]

        live = _cred_liveness(cred.get("state"))
        # Decider special: configured but not adjudicating → uncalibrated.
        if cred.get("is_decider") or "decide" in roles:
            if cred.get("state") == "valid" and judge.get("judge") != "decider":
                live = LIVENESS_UNCALIBRATED
            elif not cred.get("state"):
                live = LIVENESS_UNKNOWN

        label = cred.get("label") or cred.get("kind") or cred["id"]
        facts = [
            _fact("kind", cred.get("kind"), "GET /api/platform/credentials"),
            _fact("model", cred.get("model"), "GET /api/platform/credentials"),
            _fact("base_url", cred.get("base_url") or "(default)", "GET /api/platform/credentials"),
            _fact("state", cred.get("state"), "GET /api/platform/credentials"),
            _fact("serves", serves, "GET /api/platform/credentials"),
            _fact("is_default", cred.get("is_default"), "GET /api/platform/credentials"),
            _fact("is_embed", cred.get("is_embed"), "GET /api/platform/credentials"),
            _fact("is_decider", cred.get("is_decider"), "GET /api/platform/credentials"),
            _fact("falling_back", ",".join(cred.get("falling_back") or []) or "none",
                  "GET /api/platform/credentials"),
        ]
        if loads and isinstance(loads, dict):
            facts.append(_fact(
                "model_loads_window",
                loads.get("window"),
                "GET /api/platform/model-loads",
            ))

        protocol = "HTTP"
        kind = (cred.get("kind") or "").lower()
        if kind in ("ollama",):
            protocol = "HTTP /api/chat"
        elif kind in ("systemone", "typesafe"):
            protocol = "HTTP /v1/systemone"
        elif kind in ("openai", "xai", "anthropic", "groq", "mistral", "deepseek"):
            protocol = "HTTPS"

        nodes.append({
            "id": cid,
            "label": label,
            "title": f"{label} ({', '.join(roles)})",
            "kind": "external",
            "ring": RING_EXTERNAL,
            "scope": "project",
            "host": HOST_EXTERNAL,
            "liveness": live,
            "surfaces": roles,
            "extensions": [],
            "facts": facts,
            "provider_kind": cred.get("kind"),
        })
        edges.append({
            "from": "api", "to": cid, "protocol": protocol, "label": protocol,
        })
        ai_nodes_added += 1

    # Legacy path: project points at ollama/anthropic via llm_mode with no Credential row.
    if ai_nodes_added == 0 and effective and effective != "stub":
        cid = f"ai:legacy:{effective}"
        nodes.append({
            "id": cid,
            "label": effective,
            "title": f"{effective} (legacy llm_mode)",
            "kind": "external",
            "ring": RING_EXTERNAL,
            "scope": "project",
            "host": HOST_EXTERNAL,
            "liveness": LIVENESS_UNKNOWN,
            "surfaces": ["chat"],
            "extensions": [],
            "facts": [
                _fact("effective_chat_provider", effective, "PlatformConfigOut.effective_chat_provider"),
                _fact("llm_mode", cfg.llm_mode, "GET /api/platform"),
                _fact("local_base_url", cfg.local_base_url, "GET /api/platform"),
                _fact("local_model", cfg.local_model, "GET /api/platform"),
            ],
            "provider_kind": effective,
        })
        edges.append({
            "from": "api", "to": cid,
            "protocol": "HTTP /api/chat" if effective == "ollama" else "HTTPS",
            "label": "chat",
        })
        ai_nodes_added += 1

    if ai_nodes_added == 0:
        empties.append({"id": "ai", "message": "no cloud provider wired"})

    # Decider summary fact on the api node when configured.
    nodes[0]["facts"].append(_fact(
        "decider",
        (
            "live" if judge.get("judge") == "decider"
            else "uncalibrated" if decider.get("configured") and not decider.get("usable")
            else "configured" if decider.get("configured")
            else "not_configured"
        ),
        "platform.decider_health + memory.judge_status",
    ))
    nodes[0]["facts"].append(_fact(
        "effective_chat_provider", effective,
        "PlatformConfigOut.effective_chat_provider (project-scoped)",
    ))

    host_groups = [
        {
            "id": HOST_COMPOSE,
            "label": "Compose / Railway stack",
            "nodes": [n["id"] for n in nodes if n["host"] == HOST_COMPOSE],
        },
        {
            "id": HOST_AGENTS,
            "label": "Agent hosts / outposts",
            "nodes": [n["id"] for n in nodes if n["host"] == HOST_AGENTS],
        },
        {
            "id": HOST_USERS,
            "label": "User machines",
            "nodes": [n["id"] for n in nodes if n["host"] == HOST_USERS],
        },
        {
            "id": HOST_EXTERNAL,
            "label": "External services",
            "nodes": [n["id"] for n in nodes if n["host"] == HOST_EXTERNAL],
        },
    ]

    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hosts": host_groups,
        "nodes": nodes,
        "edges": edges,
        "empties": empties,
        "catalog": {
            "providers_total": len(catalog),
            "providers_wired": len(wired_kinds) or (1 if effective != "stub" and ai_nodes_added else 0),
            "unwired_count": len(unwired),
        },
        "identity": {
            "api_version": running.get("version") or __version__,
            "api_git_sha": running.get("git_sha") or settings.resolved_git_sha,
            "web_bundle_sha": web_sha,
            "schema_revision": schema,
            "db_engine": db_meta.get("engine"),
            "mcp_tools": LIVE_TOOL_COUNT,
        },
    }
