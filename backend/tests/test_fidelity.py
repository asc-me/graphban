"""AL-68: low- vs high-fidelity classification. High-fidelity = needs a prototype
to answer (the grill → prototype → grill handoff); tracked on items + surfaced in
prd_coverage and decompose_prd."""


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "f", **body}, headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]


# ---- item field ----

def test_create_item_default_low_fidelity(client, auth):
    key = _key(client, auth, project_id="core")
    item = _mcp(client, key, "create_item", {"title": "spec the API contract"})["structuredContent"]
    assert item["fidelity"] == "low"


def test_create_and_update_fidelity(client, auth):
    key = _key(client, auth, project_id="core")
    item = _mcp(client, key, "create_item", {"title": "design the onboarding", "fidelity": "high"})["structuredContent"]
    assert item["fidelity"] == "high"
    upd = _mcp(client, key, "update_item", {"id": item["id"], "fidelity": "low"})["structuredContent"]
    assert upd["fidelity"] == "low"


def test_invalid_fidelity_is_validation(client, auth):
    key = _key(client, auth, project_id="core")
    res = _mcp(client, key, "create_item", {"title": "x", "fidelity": "medium"})
    assert res["structuredContent"]["error"]["code"] == "validation"


# ---- decompose classifies fidelity + tags prototype work ----

def test_decompose_classifies_ui_section_as_high(client, auth):
    body = (
        "# Feature\n\n"
        "## API contract\n\nDefine the request/response shapes and error codes.\n\n"
        "## Visual design\n\nHow the dashboard should look and feel, the layout and interactions.\n"
    )
    prd = client.post("/api/prds", json={"title": "F", "body": body, "project_id": "core"}, headers=auth).json()
    from tests.prd_approve import approve_id
    approve_id(prd["id"])
    key = _key(client, auth, project_id="core")
    dec = _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})["structuredContent"]
    by_section = {p["section"]: p["fidelity"] for p in dec["proposals"]}
    assert by_section["API contract"] == "low"
    assert by_section["Visual design"] == "high"
    # The created high-fidelity item carries the fidelity + a `prototype` tag.
    details = _mcp(client, key, "get_item_details", {"id": dec["created"][1]})["structuredContent"]
    assert details["fidelity"] == "high"


# ---- coverage surfaces prototype-first work ----

def test_coverage_counts_open_high_fidelity(client, auth):
    body = "# G\n\n## Feel\n\nhow it should feel and animate.\n\n## Logic\n\nthe rules.\n"
    prd = client.post("/api/prds", json={"title": "G", "body": body, "project_id": "core"}, headers=auth).json()
    from tests.prd_approve import approve_id
    approve_id(prd["id"])
    key = _key(client, auth, project_id="core")
    _mcp(client, key, "decompose_prd", {"prd_id": prd["id"], "create": True})
    cov = _mcp(client, key, "prd_coverage", {"prd_id": prd["id"]})["structuredContent"]
    assert cov["open_high_fidelity"] == 1  # the "Feel" section
    feel = next(s for s in cov["sections"] if s["section"] == "Feel")
    assert feel["open_high_fidelity"] == 1
    logic = next(s for s in cov["sections"] if s["section"] == "Logic")
    assert logic["open_high_fidelity"] == 0
