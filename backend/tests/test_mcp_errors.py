"""AL-47: the MCP dispatcher validates args and speaks a typed error taxonomy
with repair hints — the platform's core agent-facing UX (review finding F6)."""


def _key(client, auth):
    return client.post("/api/api-keys", json={"name": "e"}, headers=auth).json()["plaintext"]


def _call(client, key, tool, arguments):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}},
        headers={"X-API-Key": key},
    ).json()


def _err(client, key, tool, arguments):
    result = _call(client, key, tool, arguments)["result"]
    assert result.get("isError") is True, result
    return result["structuredContent"]["error"]


# ---- schema validation before dispatch ----

def test_missing_required_arg_is_validation_with_hint(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "create_item", {})  # title is required
    assert err["code"] == "validation"
    assert "title" in err["message"]
    assert "required" in err["hint"]  # names the required set


def test_bad_enum_lists_allowed_values(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "create_item", {"title": "x", "status": "shipped"})
    assert err["code"] == "validation"
    assert "backlog" in err["hint"] and "done" in err["hint"]


def test_wrong_type_is_validation(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "create_item", {"title": "x", "effort": "lots"})
    assert err["code"] == "validation"
    assert "integer" in err["message"]


# ---- taxonomy ----

def test_not_found_carries_repair_hint(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "update_item", {"id": "AL-9999", "status": "done"})
    assert err["code"] == "not_found"
    assert "search_items" in err["hint"]


def test_lease_conflict_is_conflict(client, auth):
    key = _key(client, auth)
    # AL-12 exists but this agent holds no lease on it.
    err = _err(client, key, "heartbeat", {"id": "AL-12"})
    assert err["code"] == "conflict"


def test_unknown_tool_is_validation(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "teleport", {})
    assert err["code"] == "validation"
    assert "tools/list" in err["hint"]


# ---- body parsing no longer escapes the envelope ----

def test_malformed_body_is_parse_error_not_500(client, auth):
    key = _key(client, auth)
    r = client.post("/api/mcp", content=b"{not json", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32700


# ---- idempotency scoping ----

def test_idempotency_key_reuse_across_tools_conflicts(client, auth):
    key = _key(client, auth)
    _call(client, key, "create_item", {"title": "first", "idempotency_key": "tok-1"})
    # Same token, different tool → conflict, not a silent wrong-resource return.
    err = _err(client, key, "add_memory", {"text": "note", "idempotency_key": "tok-1"})
    assert err["code"] == "conflict"
    assert "create_item" in err["message"]


def test_idempotency_same_tool_returns_original(client, auth):
    key = _key(client, auth)
    a = _call(client, key, "create_item", {"title": "once", "idempotency_key": "tok-2"})["result"]
    b = _call(client, key, "create_item", {"title": "once", "idempotency_key": "tok-2"})["result"]
    assert a["structuredContent"]["id"] == b["structuredContent"]["id"]


# ---- stable shapes / receipts ----

def test_suggest_next_wraps_item(client, auth):
    key = _key(client, auth)
    res = _call(client, key, "suggest_next", {})["result"]["structuredContent"]
    assert "item" in res  # never a bare null


def test_describe_code_echoes_paths(client, auth):
    key = _key(client, auth)
    res = _call(client, key, "describe_code", {
        "nodes": [{"path": "backend/app/x.py", "kind": "file", "summary": "x"}],
    })["result"]["structuredContent"]
    assert res["upserted_paths"] == ["backend/app/x.py"]
    assert res["stale_paths"] == []


# ---- metering only on success (dashboard not inflated by errors) ----

def test_failed_calls_do_not_meter(client, auth):
    before = {t["name"]: t["calls"] for t in client.get("/api/mcp/tools", headers=auth).json()["tools"]}
    key = _key(client, auth)
    _err(client, key, "update_item", {"id": "AL-9999"})  # not_found
    after = {t["name"]: t["calls"] for t in client.get("/api/mcp/tools", headers=auth).json()["tools"]}
    assert after.get("update_item", 0) == before.get("update_item", 0)


# ---- a dropped filter must not read as the whole set (GRPH-1799) ----

# The reads that refuse an unknown argument. Duplicated here on purpose: the test
# names them, so deleting one from `_FILTER_READS` fails this assertion rather than
# shrinking the set the other test walks.
_FILTERS = {
    "get_context", "list_projects", "setup_project", "search_items", "search_memory",
    "get_lessons", "get_backlog", "suggest_next", "generate_digest", "prd_coverage",
    "unscoped_fixes", "get_rule_toggles", "list_entitlements", "propose_allocation",
    "collision_clusters",
    "fleet_status", "get_code_map", "graph_query", "search_code", "scan_status",
    "scans_due", "prd_acceptance", "learning_loop", "search_docs", "list_threads",
}
# One subject, named by a required id. A dropped extra cannot widen a page.
_IDENTITY = {
    "get_item_details", "get_prd", "grill_prd", "related_work",
    "code_neighbors", "get_document", "get_design_set",
}
# Writes. An unknown field is not a filter of a page. Named so a new tool cannot
# land here by being forgotten.
_WRITES = {
    "add_memory", "answer_grill", "answer_wait", "assign_role", "bench", "bounce",
    "claim_cluster", "claim_next", "claim_review", "close_prd", "compile_prompts",
    "confirm_tier", "create_item", "create_line", "create_prd", "create_project",
    "decompose_prd", "delegate", "describe_code", "extract_lessons", "file_finding",
    "heartbeat", "link_code", "link_items", "mint_enrolment", "next_cluster",
    "open_design", "propose_rule", "publish_memory", "put_canvas", "put_document",
    "put_preamble", "put_set_constraints", "record_scan", "register_agent",
    "reject_memory", "release_item", "report_entitlement_event", "report_graphban_issue",
    "report_utilization", "request_rebaseline", "resolve_finding", "retire_wave",
    "review_recommendation", "set_entitlement", "sign_off", "submit_verdict",
    "unlink_code", "unlink_items", "update_item", "update_prd",
    "request_prd_link", "decide_prd_link", "post_comment", "file_suggested_item",
}


def test_every_tool_is_a_filter_read_or_a_named_exemption():
    """A new tool is one or the other. Landing in neither is how a filter gets dropped."""
    from app.mcp_server import TOOLS, _FILTER_READS

    assert _FILTER_READS == _FILTERS
    names = {t["name"] for t in TOOLS}
    assert names == _FILTER_READS | _IDENTITY | _WRITES


def test_a_filter_read_refuses_an_unknown_argument_and_an_identity_read_does_not():
    """Sabotage the branch, not the set: `search_items` and `get_backlog` both go
    through it, and `get_item_details` must not."""
    import pytest

    from app import errors
    from app.mcp_server import _validate_args

    for name in ("get_backlog", "search_items"):
        with pytest.raises(errors.Validation, match="not_a_filter"):
            _validate_args(name, {"not_a_filter": "x", "agent_id": "GRPH-A1"})
    # The caller, and a fleet-only filter, are known even when the base schema omits them.
    _validate_args("get_backlog", {"agent_id": "GRPH-A1"})
    _validate_args("collision_clusters", {"holds": True, "prd_id": "GRPH-P1"})
    _validate_args("search_items", {"prd_id": "GRPH-P1", "status": "done"})
    _validate_args("fleet_status", {"view": "lean"})
    _validate_args("get_item_details", {"id": "GRPH-1", "line_id": "GRPH-L1"})


def test_get_backlog_line_id_returns_only_that_line(client, auth):
    """Drop the `line_id` comparison in `prioritized` and the off-line item comes back."""
    from app.db import SessionLocal
    from app.services import items as items_svc
    from app.services import lines as lines_svc

    proj = client.post("/api/projects", json={"name": "BacklogLine"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "bl", "project_id": proj},
                      headers=auth).json()["plaintext"]
    with SessionLocal() as db:
        line = lines_svc.create_line(db, proj)
        on = items_svc.create_item(db, title="on the line", project_id=proj, status="next",
                                   kind="fix", line_id=line.id)
        off = items_svc.create_item(db, title="not on the line", project_id=proj, status="next",
                                    kind="feature")
        line_key, on_key, off_key = line.key, on.key, off.key

    page = _call(client, key, "get_backlog", {"line_id": line_key})["result"]["structuredContent"]
    ids = [row["id"] for row in page["results"]]
    assert ids == [on_key]
    assert off_key not in ids
    assert page["total"] == 1

    whole = _call(client, key, "get_backlog", {})["result"]["structuredContent"]
    whole_ids = [row["id"] for row in whole["results"]]
    assert on_key in whole_ids and off_key in whole_ids
    assert whole["total"] == 2


def test_get_backlog_unknown_argument_is_named(client, auth):
    key = _key(client, auth)
    err = _err(client, key, "get_backlog", {"not_a_filter": "x"})
    assert err["code"] == "validation"
    assert "not_a_filter" in err["message"]
    # Naming the caller is not a filter. The call still returns a page.
    res = _call(client, key, "get_backlog", {"agent_id": "not-a-real-agent"})
    assert res["result"].get("isError") is not True
    assert "total" in res["result"]["structuredContent"]


def test_a_blank_or_unknown_line_id_is_not_the_whole_backlog(client, auth):
    key = _key(client, auth)
    blank = _err(client, key, "get_backlog", {"line_id": "  "})
    assert blank["code"] == "validation" and "line_id" in blank["message"]

    proj = client.post("/api/projects", json={"name": "NoLine"}, headers=auth).json()["id"]
    scoped = client.post("/api/api-keys", json={"name": "nl", "project_id": proj},
                         headers=auth).json()["plaintext"]
    missing = _err(client, scoped, "get_backlog", {"line_id": "NOPE-L1"})
    assert missing["code"] == "validation"
    assert "NOPE-L1" in missing["message"]
    assert "results" not in missing


# ---- a delegation verb on an unregistered connection names the exact call (GRPH-1039) ----

def _non_default_project_with_an_item(client, auth, name="zzz NonDefault"):
    """A project that is NOT the key's default, plus one item in it.

    `_key` mints a project-less key, so the dispatcher picks the default by project NAME
    (`authz._member_project_ids` orders by it, and calls that "deterministic default-project
    resolution"). The seeded projects are "Core Platform", "Infra" and "Web App", so a name
    sorting after all three is never the default — which is what makes the "not the default"
    assertion below able to fail. Callers assert that precondition anyway.
    """
    from app.db import SessionLocal
    from app.services import items as items_svc

    proj = client.post("/api/projects", json={"name": name}, headers=auth).json()["id"]
    with SessionLocal() as db:
        item = items_svc.create_item(db, title="delegate me", project_id=proj, status="next")
        return proj, item.key


def _default_project(client, key):
    """The project this key resolves to when the call names none — the server's own answer,
    so the test never re-derives the ordering it is asserting against."""
    return _call(client, key, "get_context", {})["result"]["structuredContent"]["project_id"]


def test_delegate_on_an_unregistered_connection_names_the_call_and_the_items_project(
        client, auth):
    """The reported trap. A fresh session's connection is not an agent, and the old one-line
    refusal named neither the call to make nor the project to make it in — so it read as
    "delegation isn't enabled", and a blind retry registered in the key's DEFAULT project,
    which on a multi-project key is not where the item lives."""
    proj, item_key = _non_default_project_with_an_item(client, auth)
    key = _key(client, auth)
    default = _default_project(client, key)
    assert default != proj, "the item's project IS the default; the test cannot tell them apart"

    err = _err(client, key, "delegate", {"id": item_key, "lane": "backend", "tier": "cheap"})
    assert err["code"] == "validation"
    assert "register_agent" in err["message"]
    assert 'role_hint="planner"' in err["message"]
    assert proj in err["message"]
    # The exact call, quotable as written — not three facts the caller has to assemble.
    assert f'register_agent(role_hint="planner", project_id="{proj}")' in err["message"]
    # Naming the default instead would send the retry somewhere the item is not readable.
    assert f'project_id="{default}"' not in err["message"]


def test_mint_enrolment_for_an_unregistered_agent_names_the_target_project(client, auth):
    """Same shape as `delegate`, and a real hole rather than only an opaque message:
    `mint_enrolment_as` records `minted_by` without resolving it, so an id naming no agent
    used to mint a live seat attributed to nothing."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Enrolment

    proj, _item_key = _non_default_project_with_an_item(client, auth)
    key = _key(client, auth)
    default = _default_project(client, key)
    assert default != proj, "the target project IS the default; the test cannot tell them apart"

    err = _err(client, key, "mint_enrolment",
               {"agent_id": "no-such-agent", "role": "worker", "project_id": proj})
    assert err["code"] == "validation"
    assert "register_agent" in err["message"]
    assert 'role_hint="planner"' in err["message"]
    assert proj in err["message"]
    assert f'project_id="{default}"' not in err["message"]

    # A refusal that still minted would be the old behaviour wearing a better message.
    with SessionLocal() as db:
        assert db.scalars(select(Enrolment)).all() == []


def test_mint_enrolment_for_a_bound_seat_names_the_items_project(client, auth):
    """The bound path already refused, but named no project — and its target is the ITEM's,
    which is the one a planner minting for a wave actually needs."""
    proj, item_key = _non_default_project_with_an_item(client, auth)
    key = _key(client, auth)
    assert _default_project(client, key) != proj

    err = _err(client, key, "mint_enrolment",
               {"agent_id": "no-such-agent", "role": "worker",
                "item": item_key, "bound_only": True})
    assert err["code"] == "validation"
    assert "register_agent" in err["message"]
    assert 'role_hint="planner"' in err["message"]
    assert proj in err["message"]


def test_delegate_still_works_once_the_connection_has_registered(client, auth):
    """The control. Without it the three tests above could pass on a refusal that fires for
    every caller, registered or not — which would be a worse trap than the one being fixed."""
    proj, item_key = _non_default_project_with_an_item(client, auth)
    key = _key(client, auth)

    me = _call(client, key, "register_agent",
               {"role_hint": "planner", "project_id": proj})["result"]["structuredContent"]
    assert me.get("agent_id"), me

    res = _call(client, key, "delegate",
                {"id": item_key, "lane": "backend", "tier": "cheap",
                 "agent_id": me["agent_id"]})["result"]
    assert res.get("isError") is not True, res
    assert res["structuredContent"]["brief"]
