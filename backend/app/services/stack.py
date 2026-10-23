"""Derived architecture topology for the Stack Build view (GRPH-1034 / PRD-P51 §2).

Every node and edge is read from the running system. Nothing is hand-drawn. Absences
are first-class (`not_configured`, `unknown`, empty-state messages) — never a healthy
default. Instance-wide facts (web/api/db/email) are labelled separately from
project-scoped ones (AI plane, Drive, Linear), so one project's model cannot read as
the whole system.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, text, update
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


# What makes a directory one of the project's OWN deployables, read from its code map.
# The server never holds the repo; the describing agent's config/file nodes are the only
# account of what the project builds. A container file is the strongest signal; a package
# manifest says "a unit that ships". Compose files are not deployables themselves.
_CONTAINER_NAMES = frozenset({"Dockerfile", "Containerfile"})
_MANIFEST_NAMES = frozenset({
    "package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "Gemfile",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "mix.exs", "deno.json",
    "Procfile", "fly.toml", "railway.json", "railway.toml", "vercel.json", "netlify.toml",
    "app.yaml",
})
# A manifest under any of these is a fixture, an example or tooling, not a deployable.
_NOT_DEPLOYABLE_SEGMENTS = frozenset({
    "node_modules", "test", "tests", "__tests__", "fixtures", "examples", "example",
    "docs", "vendor", "dist", "build", ".github", ".cursor", ".vscode", ".claude",
})
HOST_PROJECT = "project_app"
HOST_APP_INFRA = "app_infra"
_MAX_OWN_DEPLOYABLES = 12


def _is_deploy_file(name: str) -> str:
    if name in _CONTAINER_NAMES or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
        return "container"
    if name in _MANIFEST_NAMES:
        return "manifest"
    return ""


def own_deployables(db: Session, project_id: str,
                    owner: dict[str, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """The project's own deployables, one per directory whose Dockerfile or package
    manifest its code map describes. `[{dir, files: [(path, summary)], container}]`.

    Directories that are the source root of a Graphban box this project owns are left
    out (Graphban's own project would otherwise draw `backend/` beside `api`). A repo
    root with only a manifest is a workspace root when subdirectories also qualify, so it
    is drawn only when it is the sole candidate or carries a container file.
    """
    from app.models import CodeNode

    try:
        rows = db.execute(
            select(CodeNode.path, CodeNode.summary).where(CodeNode.project_id == project_id)
        ).all()
    except Exception:  # noqa: BLE001
        logger.debug("code map unreadable for own deployables", exc_info=True)
        return []
    owned_markers = [
        m for nid, (state, _) in (owner or {}).items() if state == OWNED
        for m in GRAPHBAN_SOURCES[nid]["markers"]
    ]
    dirs: dict[str, dict[str, Any]] = {}
    for path, summary in rows:
        if "::" in path:
            continue
        parts = path.strip("/").split("/")
        kind = _is_deploy_file(parts[-1])
        if not kind:
            continue
        parent = parts[:-1]
        if any(seg in _NOT_DEPLOYABLE_SEGMENTS for seg in parent):
            continue
        d = "/".join(parent)
        if d and any(m == d or m.startswith(d + "/") for m in owned_markers):
            continue
        entry = dirs.setdefault(d, {"dir": d, "files": [], "container": False})
        entry["files"].append((path, summary or ""))
        entry["container"] = entry["container"] or kind == "container"
    if "" in dirs and len(dirs) > 1 and not dirs[""]["container"]:
        del dirs[""]
    out = sorted(dirs.values(), key=lambda e: (not e["container"], e["dir"]))
    for e in out:
        e["files"].sort()
    return out[:_MAX_OWN_DEPLOYABLES]


def own_node_id(directory: str) -> str:
    return f"app:{directory or '.'}"


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

    # ── The project's own deployables (its code map's Dockerfiles / manifests) ──
    project_name = ""
    try:
        from app.models import Project
        proj = db.get(Project, project_id)
        project_name = (proj.name if proj else "") or project_id
    except Exception:  # noqa: BLE001
        project_name = project_id
    # A posted record is the app. Code-map deployables are what we draw only before
    # the first post (PRD-P55 §4); drawing both would show the app twice.
    record, record_unreadable = _load_app_record(db, project_id)
    own = [] if record is not None else own_deployables(db, project_id, owner)
    if record is not None:
        _fold_app_record(nodes, edges, record, project_name)
    for dep in own:
        d = dep["dir"]
        facts = [
            _fact(f"file:{p}", s or "(no summary)", "code map (code_nodes)") for p, s in dep["files"]
        ]
        facts.append(_fact(
            "liveness", "Graphban does not probe this app; where it runs is not in the code map",
            "model",
        ))
        nodes.append({
            "id": own_node_id(d),
            "label": d.rsplit("/", 1)[-1] if d else project_name,
            "title": f"{d}/" if d else f"{project_name} (repo root)",
            "kind": "deployable",
            "ring": RING_CODE,
            "scope": "project",
            "host": HOST_PROJECT,
            "liveness": LIVENESS_UNKNOWN,
            "surfaces": [],
            "extensions": [],
            "facts": [
                _fact("packaging", "container image" if dep["container"] else "package",
                      "Dockerfile in the code map" if dep["container"] else "manifest in the code map"),
                *facts,
            ],
        })

    if record is None and not own and not any(state == OWNED for state, _ in owner.values()):
        empties.append({
            "id": "own_code",
            "message": "this project's own deployables are not on this map — its code map "
                       "describes no Dockerfile or package manifest (package.json, "
                       "pyproject.toml, …) to draw them from",
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

    project_nodes = [n["id"] for n in nodes if n["host"] == HOST_PROJECT]
    app_infra_nodes = [n["id"] for n in nodes if n["host"] == HOST_APP_INFRA]
    app_source = "the app topology record" if record is not None else "the code map"
    host_groups = ([{
        "id": HOST_PROJECT,
        "label": f"{project_name} — its own app (from {app_source})",
        "nodes": project_nodes,
    }] if project_nodes else []) + ([{
        "id": HOST_APP_INFRA,
        "label": f"{project_name} — infrastructure the app runs on",
        "nodes": app_infra_nodes,
    }] if app_infra_nodes else []) + [
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
        # Always present. Omitting it when nothing was posted would read as "no services".
        "mapping": _mapping_block(record, record_unreadable),
        "app": None if record is None else _record_public(record),
    }


# ── Posted app topology (GRPH-1098 / PRD-P55 §2) ─────────────────────────────
#
# The server never holds the repo. A producer that does posts one current record
# per project. A refused post writes nothing, so a bad payload cannot blank the map.

class TopologyRefused(Exception):
    """The whole post was refused. `errors` is `{path, reason}`; nothing was written."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s)")


_NO_HEAD = frozenset({"", "unknown"})
_SECRET_REASON = "env values are never stored, only variable names"
_DANGLING = (
    "references a node the post does not define; declare it as an unresolved node"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# URLs, credential assignments, and known token prefixes. Matched on every string
# the post would store. A variable name (`DATABASE_URL`) has none of these.
_SECRET_RE = re.compile(
    r"://"
    r"|(?<![A-Za-z0-9_])(?:password|secret|api_key|token)\s*=\s*\S"
    r"|(?:^|[^A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9]|sk_live_|sk_test_|ghp_[A-Za-z0-9]|github_pat_|"
    r"xox[baprs]-|AKIA[A-Z0-9]"
    r")",
    re.IGNORECASE,
)
_TOP_KEYS = frozenset({
    "project_id", "producer_id", "producer_version", "mapped_sha",
    "components", "infrastructure", "external", "unresolved", "edges",
})
_NODE_KEYS = {
    "components": frozenset({"id", "dir", "label", "provenance", "env"}),
    "infrastructure": frozenset({"id", "label", "kind", "provenance", "env"}),
    "external": frozenset({"id", "label", "provenance", "env"}),
    "unresolved": frozenset({"id", "label", "provenance", "env"}),
}
_EDGE_KEYS = frozenset({"from", "to", "protocol", "label", "provenance", "env"})
_PROV_KEYS = frozenset({"file", "rule", "line"})
_FORBIDDEN_KEYS = frozenset({
    "value", "env_value", "secret", "password", "token", "api_key", "credential",
})
_GROUPS = ("components", "infrastructure", "external", "unresolved")


def project_head_sha() -> str | None:
    """The revision scans compare against, or None when this process has none.

    Same source as the Stack identity (`instance_update.running`). `unknown` is the
    sentinel that source returns when nothing was baked in (GRPH-426). A map compared
    to that sentinel would read as current. No head is `freshness_unknown`.
    """
    running = instance_update.running()
    raw = (running.get("git_sha") or settings.resolved_git_sha or "").strip()
    if raw in _NO_HEAD:
        return None
    return raw


def _looks_like_secret(value: str) -> bool:
    return bool(_SECRET_RE.search(value))


def _norm_dir(raw: str) -> str:
    d = raw.strip().replace("\\", "/")
    while d.startswith("./"):
        d = d[2:]
    d = d.strip("/")
    if d == ".":
        return ""
    return d


def _load_app_record(db: Session, project_id: str):
    """`(row, unreadable)`. A failed read is not "nobody posted"."""
    from app.models import AppTopology

    try:
        return db.get(AppTopology, project_id), False
    except Exception:  # noqa: BLE001 — a missing table or a broken row is absence of a read
        logger.debug("app topology unreadable", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("rollback after unreadable app topology failed", exc_info=True)
        return None, True


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _mapping_block(record, unreadable: bool) -> dict[str, Any]:
    """`mapped | stale | unmapped | freshness_unknown`.

    No record is `unmapped` even when the head is known — that is "nobody posted",
    and it is not "this app calls nothing". A record with no head is
    `freshness_unknown`, never `mapped`: staleness is the sha, and a missing sha
    is not a match. An unreadable record is the same state with empty fields, so
    a failed read cannot pass for a project that was never mapped.
    """
    head = project_head_sha()
    empty = {
        "state": "freshness_unknown" if unreadable else "unmapped",
        "mapped_sha": None,
        "head_sha": head,
        "previous_mapped_sha": None,
        "producer_id": None,
        "producer_version": None,
        "mapped_at": None,
        "posted_by": None,
    }
    if unreadable or record is None:
        return empty
    mapped = (record.mapped_sha or "").strip()
    if head is None:
        state = "freshness_unknown"
    elif head != mapped:
        state = "stale"
    else:
        state = "mapped"
    return {
        "state": state,
        "mapped_sha": mapped or None,
        "head_sha": head,
        "previous_mapped_sha": record.previous_mapped_sha or "",
        "producer_id": record.producer_id,
        "producer_version": record.producer_version,
        "mapped_at": _iso(record.mapped_at),
        "posted_by": record.posted_by or "",
    }


def _record_public(record) -> dict[str, Any]:
    """The stored post, in the shape the producer sent. `external` is the wire key."""
    return {
        "project_id": record.project_id,
        "producer_id": record.producer_id,
        "producer_version": record.producer_version,
        "mapped_sha": record.mapped_sha,
        "mapped_at": _iso(record.mapped_at),
        "previous_mapped_sha": record.previous_mapped_sha or "",
        "posted_by": record.posted_by or "",
        "components": list(record.components or []),
        "infrastructure": list(record.infrastructure or []),
        "external": list(record.external_services or []),
        "unresolved": list(record.unresolved or []),
        "edges": list(record.edges or []),
    }


def _prov_dict(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {
        "file": str(raw.get("file") or ""),
        "rule": str(raw.get("rule") or ""),
    }
    line = raw.get("line")
    if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
        out["line"] = line
    return out


def _fold_app_record(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                     record, project_name: str) -> None:
    """Draw the posted record in place of code-map deployables.

    Graph ids are prefixed (`app:`, `infra:`, `ext:`, `unresolved:`) so a producer
    id of `api` or `db` cannot collide with the instance boxes. Edges whose
    endpoints are not in this record are dropped: a corrupt row must not draw a
    dangling edge the post path would have refused.
    """
    produced: dict[str, str] = {}

    def add(graph_id: str, label: str, title: str, kind: str, ring: int, host: str,
            prov: dict[str, Any], extra: list[dict[str, str]]) -> None:
        facts = [
            _fact("file", prov.get("file"), "app topology record"),
            _fact("rule", prov.get("rule"), "app topology record"),
        ]
        if prov.get("line"):
            facts.append(_fact("line", prov["line"], "app topology record"))
        facts.append(_fact("producer", record.producer_id, "app topology record"))
        facts.append(_fact("producer_version", record.producer_version, "app topology record"))
        facts.append(_fact("mapped_sha", record.mapped_sha, "app topology record"))
        facts.extend(extra)
        facts.append(_fact("liveness", "Graphban does not probe this app", "model"))
        nodes.append({
            "id": graph_id,
            "label": label,
            "title": title,
            "kind": kind,
            "ring": ring,
            "scope": "project",
            "host": host,
            "liveness": LIVENESS_UNKNOWN,
            "surfaces": [],
            "extensions": [],
            "facts": facts,
            "provenance": prov,
        })

    for comp in record.components or []:
        if not isinstance(comp, dict):
            continue
        directory = comp.get("dir") if isinstance(comp.get("dir"), str) else ""
        directory = _norm_dir(directory) if directory else ""
        gid = own_node_id(directory)
        label = (comp.get("label") or "").strip() if isinstance(comp.get("label"), str) else ""
        if not label:
            label = directory.rsplit("/", 1)[-1] if directory else (project_name or "app")
        title = f"{directory}/" if directory else f"{project_name or 'app'} (repo root)"
        prov = _prov_dict(comp.get("provenance"))
        extra = [_fact("dir", directory or ".", "app topology record")]
        if isinstance(comp.get("env"), str) and comp["env"]:
            extra.append(_fact("env", comp["env"], "app topology record"))
        add(gid, label, title, "deployable", RING_CODE, HOST_PROJECT, prov, extra)
        produced[str(comp.get("id") or directory or ".")] = gid

    def plain(group: str, prefix: str, kind: str, ring: int, host: str) -> None:
        for item in getattr(record, group) or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            pid = str(item["id"])
            gid = f"{prefix}:{pid}"
            label = item.get("label") if isinstance(item.get("label"), str) else ""
            label = label.strip() or pid
            prov = _prov_dict(item.get("provenance"))
            extra: list[dict[str, str]] = []
            if group == "infrastructure" and isinstance(item.get("kind"), str) and item["kind"]:
                extra.append(_fact("kind", item["kind"], "app topology record"))
            if isinstance(item.get("env"), str) and item["env"]:
                extra.append(_fact("env", item["env"], "app topology record"))
            add(gid, label, label, kind, ring, host, prov, extra)
            produced[pid] = gid

    plain("infrastructure", "infra", "infra", RING_INFRA, HOST_APP_INFRA)
    plain("external_services", "ext", "external", RING_EXTERNAL, HOST_EXTERNAL)
    plain("unresolved", "unresolved", "unresolved", RING_EXTERNAL, HOST_EXTERNAL)

    for edge in record.edges or []:
        if not isinstance(edge, dict):
            continue
        src = produced.get(str(edge.get("from") or ""))
        dst = produced.get(str(edge.get("to") or ""))
        if not src or not dst:
            logger.debug("app topology edge dropped; endpoint not in the record")
            continue
        edges.append({
            "from": src,
            "to": dst,
            "protocol": str(edge.get("protocol") or ""),
            "label": str(edge.get("label") or ""),
            "provenance": _prov_dict(edge.get("provenance")),
        })


def _err(errors: list[dict[str, str]], path: str, reason: str) -> None:
    errors.append({"path": path, "reason": reason})


def _req_str(body: dict, key: str, errors: list[dict[str, str]]) -> str | None:
    if key not in body or body[key] is None:
        _err(errors, key, "missing")
        return None
    if not isinstance(body[key], str):
        _err(errors, key, "must be a string")
        return None
    value = body[key].strip()
    if not value:
        _err(errors, key, "missing")
        return None
    if _looks_like_secret(value):
        _err(errors, key, _SECRET_REASON)
    return value


def _field_str(item: dict, key: str, prefix: str, errors: list[dict[str, str]],
               *, allow_empty: bool = False) -> str | None:
    path = f"{prefix}.{key}"
    if key not in item or item[key] is None:
        _err(errors, path, "missing")
        return None
    if not isinstance(item[key], str):
        _err(errors, path, "must be a string")
        return None
    value = item[key] if allow_empty else item[key].strip()
    if not allow_empty and not value.strip():
        _err(errors, path, "missing")
        return None
    if _looks_like_secret(value):
        _err(errors, path, _SECRET_REASON)
    return value.strip() if not allow_empty else value


def _provenance(item: dict, prefix: str, errors: list[dict[str, str]]) -> dict | None:
    path = f"{prefix}.provenance"
    if "provenance" not in item or item["provenance"] is None or item["provenance"] == {}:
        _err(errors, path, "missing")
        return None
    prov = item["provenance"]
    if not isinstance(prov, dict):
        _err(errors, path, "must be an object")
        return None
    for key in prov:
        if key in _FORBIDDEN_KEYS:
            _err(errors, f"{path}.{key}", _SECRET_REASON)
        elif key not in _PROV_KEYS:
            _err(errors, f"{path}.{key}", "unknown field")
    ok = True
    file_ = prov.get("file")
    rule = prov.get("rule")
    if not isinstance(file_, str) or not file_.strip():
        _err(errors, f"{path}.file", "missing")
        ok = False
    elif _looks_like_secret(file_):
        _err(errors, f"{path}.file", _SECRET_REASON)
        ok = False
    if not isinstance(rule, str) or not rule.strip():
        _err(errors, f"{path}.rule", "missing")
        ok = False
    elif _looks_like_secret(rule):
        _err(errors, f"{path}.rule", _SECRET_REASON)
        ok = False
    line = None
    if "line" in prov and prov["line"] is not None:
        line_v = prov["line"]
        if isinstance(line_v, bool) or not isinstance(line_v, int) or line_v < 1:
            _err(errors, f"{path}.line", "must be a positive integer")
            ok = False
        else:
            line = line_v
    if not ok:
        return None
    out: dict[str, Any] = {"file": file_.strip(), "rule": rule.strip()}
    if line is not None:
        out["line"] = line
    return out


def _optional_env(item: dict, prefix: str, errors: list[dict[str, str]]) -> str:
    if "env" not in item or item["env"] is None or item["env"] == "":
        return ""
    env = item["env"]
    if not isinstance(env, str) or not _IDENT.fullmatch(env):
        _err(errors, f"{prefix}.env", "must be a variable name")
        return ""
    if _looks_like_secret(env):
        _err(errors, f"{prefix}.env", _SECRET_REASON)
        return ""
    return env


def _check_keys(item: dict, allowed: frozenset, prefix: str, errors: list[dict[str, str]]) -> None:
    for key in item:
        if key in _FORBIDDEN_KEYS:
            _err(errors, f"{prefix}.{key}", _SECRET_REASON)
        elif key not in allowed:
            _err(errors, f"{prefix}.{key}", "unknown field")


def _validate_app_post(body: Any) -> tuple[list[dict[str, str]], dict | None]:
    """Every problem, sorted by path. A non-empty error list means store nothing."""
    errors: list[dict[str, str]] = []
    if not isinstance(body, dict):
        return [{"path": "", "reason": "must be an object"}], None

    for key in body:
        if key in _FORBIDDEN_KEYS:
            _err(errors, key, _SECRET_REASON)
        elif key not in _TOP_KEYS:
            _err(errors, key, "unknown field")

    producer_id = _req_str(body, "producer_id", errors)
    producer_version = _req_str(body, "producer_version", errors)
    mapped_sha = _req_str(body, "mapped_sha", errors)
    if isinstance(body.get("mapped_sha"), str) and body["mapped_sha"].strip() == "unknown":
        _err(errors, "mapped_sha", "unknown is not a commit")
    if isinstance(body.get("project_id"), str) and _looks_like_secret(body["project_id"]):
        _err(errors, "project_id", _SECRET_REASON)

    groups: dict[str, list] = {g: [] for g in _GROUPS}
    id_owner: dict[str, str] = {}
    dir_owner: dict[str, str] = {}

    for group in _GROUPS:
        if group not in body:
            continue
        raw = body[group]
        if not isinstance(raw, list):
            _err(errors, group, "must be a list")
            continue
        allowed = _NODE_KEYS[group]
        for i, item in enumerate(raw):
            prefix = f"{group}[{i}]"
            if not isinstance(item, dict):
                _err(errors, prefix, "must be an object")
                continue
            _check_keys(item, allowed, prefix, errors)
            prov = _provenance(item, prefix, errors)
            env = _optional_env(item, prefix, errors)
            label = ""
            if "label" in item and item["label"] is not None:
                if not isinstance(item["label"], str):
                    _err(errors, f"{prefix}.label", "must be a string")
                else:
                    label = item["label"].strip()
                    if _looks_like_secret(label):
                        _err(errors, f"{prefix}.label", _SECRET_REASON)

            directory: str | None = None
            kind = ""
            if group == "components":
                if "dir" not in item or item["dir"] is None:
                    _err(errors, f"{prefix}.dir", "missing")
                elif not isinstance(item["dir"], str):
                    _err(errors, f"{prefix}.dir", "must be a string")
                else:
                    if _looks_like_secret(item["dir"]):
                        _err(errors, f"{prefix}.dir", _SECRET_REASON)
                    directory = _norm_dir(item["dir"])
                raw_id = item.get("id")
                if "id" in item and raw_id is not None and not isinstance(raw_id, str):
                    _err(errors, f"{prefix}.id", "must be a string")
                    pid = None
                elif isinstance(raw_id, str) and raw_id.strip():
                    pid = raw_id.strip()
                    if _looks_like_secret(pid):
                        _err(errors, f"{prefix}.id", _SECRET_REASON)
                elif directory is None:
                    pid = None
                else:
                    pid = directory or "."
            else:
                pid = None
                raw_id = item.get("id")
                if "id" not in item or raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
                    _err(errors, f"{prefix}.id", "missing")
                elif not isinstance(raw_id, str):
                    _err(errors, f"{prefix}.id", "must be a string")
                else:
                    pid = raw_id.strip()
                    if _looks_like_secret(pid):
                        _err(errors, f"{prefix}.id", _SECRET_REASON)
                if group == "infrastructure" and "kind" in item and item["kind"] is not None:
                    if not isinstance(item["kind"], str) or not item["kind"].strip():
                        _err(errors, f"{prefix}.kind", "must be a string")
                    else:
                        kind = item["kind"].strip()
                        if _looks_like_secret(kind):
                            _err(errors, f"{prefix}.kind", _SECRET_REASON)

            if pid:
                if pid in id_owner:
                    _err(errors, f"{prefix}.id", f"duplicate of {id_owner[pid]}")
                else:
                    id_owner[pid] = f"{prefix}.id"
            if group == "components" and directory is not None:
                if directory in dir_owner:
                    _err(errors, f"{prefix}.dir", f"duplicate of {dir_owner[directory]}")
                else:
                    dir_owner[directory] = f"{prefix}.dir"

            # Registered for edge checks even when provenance failed, so a posted
            # node is not also reported as a node the post does not define.
            node_broken = any(e["path"] == prefix or e["path"].startswith(prefix + ".") for e in errors)
            if pid and not node_broken and prov is not None and (group != "components" or directory is not None):
                norm: dict[str, Any] = {
                    "id": pid,
                    "label": label,
                    "provenance": prov,
                }
                if env:
                    norm["env"] = env
                if group == "components":
                    norm["dir"] = directory
                if group == "infrastructure" and kind:
                    norm["kind"] = kind
                groups[group].append(norm)

    node_paths = tuple(f"{g}[" for g in _GROUPS) + _GROUPS
    structural = any(e["path"] in _GROUPS or e["path"].startswith(node_paths) for e in errors)
    if sum(len(v) for v in groups.values()) == 0 and not structural:
        _err(errors, "nodes", "missing")

    edges_norm: list[dict[str, Any]] = []
    if "edges" in body:
        raw_edges = body["edges"]
        if not isinstance(raw_edges, list):
            _err(errors, "edges", "must be a list")
        else:
            for i, item in enumerate(raw_edges):
                prefix = f"edges[{i}]"
                if not isinstance(item, dict):
                    _err(errors, prefix, "must be an object")
                    continue
                _check_keys(item, _EDGE_KEYS, prefix, errors)
                src = _field_str(item, "from", prefix, errors)
                dst = _field_str(item, "to", prefix, errors)
                protocol = _field_str(item, "protocol", prefix, errors)
                if "label" not in item or item["label"] is None:
                    _err(errors, f"{prefix}.label", "missing")
                    label = None
                elif not isinstance(item["label"], str):
                    _err(errors, f"{prefix}.label", "must be a string")
                    label = None
                else:
                    label = item["label"]
                    if _looks_like_secret(label):
                        _err(errors, f"{prefix}.label", _SECRET_REASON)
                prov = _provenance(item, prefix, errors)
                env = _optional_env(item, prefix, errors)
                if src and src not in id_owner:
                    _err(errors, f"{prefix}.from", _DANGLING)
                if dst and dst not in id_owner:
                    _err(errors, f"{prefix}.to", _DANGLING)
                edge_broken = any(
                    e["path"] == prefix or e["path"].startswith(prefix + ".") for e in errors
                )
                if not edge_broken and src and dst and protocol is not None and label is not None and prov:
                    edge: dict[str, Any] = {
                        "from": src,
                        "to": dst,
                        "protocol": protocol,
                        "label": label.strip(),
                        "provenance": prov,
                    }
                    if env:
                        edge["env"] = env
                    edges_norm.append(edge)

    seen: set[tuple[str, str]] = set()
    uniq: list[dict[str, str]] = []
    for e in sorted(errors, key=lambda item: (item["path"], item["reason"])):
        key = (e["path"], e["reason"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    if uniq:
        return uniq, None
    return [], {
        "producer_id": producer_id,
        "producer_version": producer_version,
        "mapped_sha": mapped_sha,
        "components": groups["components"],
        "infrastructure": groups["infrastructure"],
        "external": groups["external"],
        "unresolved": groups["unresolved"],
        "edges": edges_norm,
    }


def replace_app_topology(db: Session, project_id: str, body: dict, *, posted_by: str) -> dict:
    """Validate, then replace the one current record. Last valid post wins.

    The check finishes before any write. A refusal leaves the previous row
    untouched — the map never goes blank because a post was wrong.
    """
    errors, norm = _validate_app_post(body)
    if errors or norm is None:
        raise TopologyRefused(errors)

    from sqlalchemy.exc import IntegrityError

    from app.models import AppTopology, Project

    if db.get(Project, project_id) is None:
        raise TopologyRefused([{"path": "project_id", "reason": "unknown project"}])

    # One UPDATE. `previous_mapped_sha = mapped_sha` is the old sha, evaluated
    # from the row being replaced, so two valid posts cannot blank it and the
    # last committed post is the record. No SELECT ... FOR UPDATE: an untested
    # lock is a round trip (test_lock_concurrency).
    payload = {
        "previous_mapped_sha": AppTopology.mapped_sha,
        "producer_id": norm["producer_id"],
        "producer_version": norm["producer_version"],
        "mapped_sha": norm["mapped_sha"],
        "mapped_at": datetime.now(timezone.utc),
        "posted_by": posted_by or "",
        "components": list(norm["components"]),
        "infrastructure": list(norm["infrastructure"]),
        "external_services": list(norm["external"]),
        "unresolved": list(norm["unresolved"]),
        "edges": list(norm["edges"]),
    }
    updated = db.execute(
        update(AppTopology).where(AppTopology.project_id == project_id).values(**payload)
    )
    if not updated.rowcount:
        db.add(AppTopology(
            project_id=project_id,
            previous_mapped_sha="",
            producer_id=norm["producer_id"],
            producer_version=norm["producer_version"],
            mapped_sha=norm["mapped_sha"],
            mapped_at=payload["mapped_at"],
            posted_by=payload["posted_by"],
            components=list(norm["components"]),
            infrastructure=list(norm["infrastructure"]),
            external_services=list(norm["external"]),
            unresolved=list(norm["unresolved"]),
            edges=list(norm["edges"]),
        ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        db.execute(
            update(AppTopology).where(AppTopology.project_id == project_id).values(**payload)
        )
        db.commit()
    row = db.get(AppTopology, project_id)
    db.refresh(row)
    return _record_public(row)
