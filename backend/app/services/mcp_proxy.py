"""Local MCP proxy for the local-first hybrid (AL-138).

When a local instance is LINKED to a cloud tenant, agents talk to the LOCAL `/api/mcp`, but
only the code-graph tools run locally — the graph is built and authoritative here. Everything
else (items, claims, memory, PRDs, backlog…) is forwarded to the cloud, which stays
authoritative for mutable, contended state (D2). Unlinked, nothing is forwarded — a pure
local tool.

The local endpoint authenticates the incoming LOCAL key as usual; a proxied call is made with
the org-minted link credential (`SYNC_API_KEY`), so the CLOUD applies its own authz, metering,
and audit — the local side never double-counts.

The cloud sees only the sync key, so it cannot apply the CALLER's key limits (scopes, a PRD
bound, a role ceiling). Those are checked locally before anything is forwarded (GRPH-1981):
the dispatcher runs `_call_tool`'s prelude, then `require_forwardable` below refuses what can
only fail closed once it is upstream.
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.errors import Refused

_TIMEOUT = 30.0

# Served LOCALLY even when linked: the code graph (built + authoritative here), the code↔item
# bridge (graph-adjacent), key introspection, and the separate upstream issue reporter.
LOCAL_TOOLS = {
    "describe_code", "get_code_map", "code_neighbors", "search_code",
    "link_code", "unlink_code", "get_context",
    # MUST stay local (AL-284). Proxying happens BEFORE dispatch, so a linked instance
    # would forward this to the cloud and create the project in the org's tenant space —
    # exactly the authority action the gate refuses. Kept local, the refusal fires.
    "create_project",
    # Both names, because the proxy decides local-vs-remote BEFORE the dispatcher
    # normalizes aliases — a retired name must not start proxying to the cloud (AL-262).
    "report_graphban_issue", "report_agentledger_issue",
}


def enabled() -> bool:
    """True when this instance is linked to a cloud tenant (so non-graph tools proxy)."""
    return bool(settings.sync_cloud_url and settings.sync_api_key)


def should_proxy(name: str) -> bool:
    return enabled() and name not in LOCAL_TOOLS


def forward(name: str, args: dict) -> dict:
    """Forward a `tools/call` to the cloud tenant's MCP endpoint; return the raw JSON-RPC
    response (`{result}` or `{error}`)."""
    url = settings.sync_cloud_url.rstrip("/")
    resp = httpx.post(
        f"{url}/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers={"X-API-Key": settings.sync_api_key},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def require_forwardable(db, key, name: str, args: dict) -> None:
    """Refuse a call whose caller's key limits would not survive the trip upstream.

    The local prelude has already run. What remains are limits it judges from local rows that
    a linked box may not hold, and which the sync key upstream would not apply in their place:

    - A PRD-limited key (GRPH-1973) writing an item this box does not hold. The local check
      passes an unknown item; upstream that item could sit in any PRD.
    - A PRD-limited key registering an agent. The agent would be registered on the sync key,
      which carries no PRD bound, so every claim it made upstream would be unbounded.
    - A role-restricted key registering an agent with an enrolment code (the seat's role is
      upstream and unreadable here) or without a `role_hint` inside its ceiling (upstream the
      sync key's ceiling would decide the role).
    """
    from app.security import authz
    from app.services import fleet as fleet_svc
    from app.services import keys as keys_svc
    from app.models import Item

    if key.prd_id:
        if name == "register_agent":
            raise Refused(
                403, f"api key {key.name!r} is limited to PRD {key.prd_id}, and this box "
                "forwards register_agent upstream under its sync key, which carries no PRD "
                "bound", rule_id=authz.PRD_KEY_RULE,
                hint="use a PRD-limited key minted on the cloud tenant itself")
        for arg in authz.PRD_KEY_ITEM_WRITES.get(name, ()):
            ref = args.get(arg)
            if not isinstance(ref, str) or not ref:
                continue
            if db.get(Item, keys_svc.resolve_item(db, ref) or ref) is None:
                raise Refused(
                    403, f"api key {key.name!r} is limited to PRD {key.prd_id}; item {ref!r} "
                    f"is not held on this box, so it cannot be shown to be inside that PRD "
                    f"and {name} is not forwarded", rule_id=authz.PRD_KEY_RULE,
                    hint="this key writes only its own PRD's items")
    if name == "register_agent":
        allowed = fleet_svc.eligible_roles(key)
        if set(allowed) >= set(fleet_svc.ROLES):
            return
        if args.get("enrolment_code") or args.get("role_hint") not in allowed:
            raise Refused(
                403, f"api key {key.name!r} may hold only {', '.join(allowed)}; this box "
                "forwards register_agent upstream under its sync key, which would decide the "
                "role without that ceiling", rule_id="review.role_gated_tools",
                hint=f"pass role_hint as one of {', '.join(allowed)} and no enrolment_code")
