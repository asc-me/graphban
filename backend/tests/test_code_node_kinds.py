"""`CodeNode.kind` means something now (GRPH-382).

The enum shipped without definitions and the live graph drifted to 119 `module` / 4 `file` /
0 `symbol`, with every path a file path. Nothing read the field except a colour, which is how
it stayed wrong for so long — and PRD-20 D5 encodes kind in node fill and argues presence
clouds are safe because they use a different channel, which defends nothing on a graph that is
97% one value.
"""
import json

import pytest

from app.services import code_graph as cg


@pytest.fixture
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mcp(client, key, name, args):
    return json.loads(
        client.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": args}},
            headers={"X-API-Key": key},
        ).json()["result"]["content"][0]["text"]
    )


def _key(client, auth):
    return client.post(
        "/api/api-keys", json={"name": "kinds", "project_id": "core"}, headers=auth
    ).json()["plaintext"]


# ── the vocabulary ────────────────────────────────────────────────────────────

def test_a_source_file_is_a_file_not_a_module():
    # The exact mistake the live graph made 119 times.
    assert cg.kind_for_path("backend/app/services/items.py") == "file"
    assert cg.kind_for_path("web/src/lib/queries.ts") == "file"


def test_a_package_is_a_module():
    assert cg.kind_for_path("backend/app/services") == "module"
    assert cg.kind_for_path("web/src/lib") == "module"


def test_a_named_thing_inside_a_file_is_a_symbol():
    assert cg.kind_for_path("backend/app/services/items.py::claim_next") == "symbol"
    # `::` decides it even when the file half looks like a package.
    assert cg.kind_for_path("backend/app/services::helper") == "symbol"


def test_unknown_extensions_read_as_packages_rather_than_guessing():
    # A path with no recognised suffix is more likely a package than a file, and calling it a
    # file would put it in the same bucket as the thing it contains.
    assert cg.kind_for_path("infra/terraform") == "module"


def test_empty_and_whitespace_do_not_crash():
    assert cg.kind_for_path("") == "module"
    assert cg.kind_for_path("   ") == "module"


# ── writes ────────────────────────────────────────────────────────────────────

def test_an_unrecognised_kind_is_REFUSED_not_silently_rewritten(db):
    # Was: coerced to "file", so a caller passing junk got a plausible node and no signal.
    with pytest.raises(ValueError, match="unknown kind"):
        cg.upsert_node(db, project_id="core", path="a.py", kind="widget", name="a")


def test_a_kind_contradicting_its_path_is_corrected_to_the_path(db):
    node = cg.upsert_node(
        db, project_id="core", path="backend/app/services/items.py", kind="module", name="items"
    )
    db.commit()
    assert node.kind == "file"


def test_symbols_are_stored_as_symbols(db):
    node = cg.upsert_node(
        db, project_id="core", path="a.py::claim_next", kind="file", name="claim_next"
    )
    db.commit()
    assert node.kind == "symbol"


def test_describe_code_REPORTS_the_correction_rather_than_making_it_silently(client, auth):
    """The half that silent coercion never did.

    A caller that learns it mislabelled can stop; one that is never told cannot — which is
    exactly how the live graph reached 97% one value with nobody noticing.
    """
    key = _key(client, auth)
    res = _mcp(client, key, "describe_code", {
        "nodes": [
            {"path": "backend/app/services/fleet.py", "kind": "module", "name": "fleet"},
            {"path": "backend/app/services", "kind": "module", "name": "services"},
        ],
    })
    assert res["nodes_upserted"] == 2
    assert res["kind_corrections"] == [
        {"path": "backend/app/services/fleet.py", "asked": "module", "stored": "file"},
    ]


def test_no_corrections_reported_when_the_caller_gets_it_right(client, auth):
    key = _key(client, auth)
    res = _mcp(client, key, "describe_code", {
        "nodes": [
            {"path": "backend/app/services/fleet.py", "kind": "file", "name": "fleet"},
            {"path": "backend/app/services", "kind": "module", "name": "services"},
            {"path": "backend/app/services/fleet.py::claim_cluster", "kind": "symbol", "name": "cc"},
        ],
    })
    # An empty list is the clean result here, and it is only reachable by being right.
    assert res["kind_corrections"] == []


def test_a_describe_pass_produces_a_MIXED_population(client, auth, db):
    """The property the whole item exists for: the kind channel carries information again."""
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {
        "nodes": [
            {"path": "backend/app/services", "kind": "module", "name": "services"},
            {"path": "backend/app/services/items.py", "kind": "module", "name": "items"},
            {"path": "backend/app/services/fleet.py", "kind": "module", "name": "fleet"},
            {"path": "backend/app/services/items.py::claim_next", "kind": "module", "name": "cn"},
        ],
    })
    kinds = {n.path: n.kind for n in cg.list_nodes(db, "core")}
    assert kinds["backend/app/services"] == "module"
    assert kinds["backend/app/services/items.py"] == "file"
    assert kinds["backend/app/services/items.py::claim_next"] == "symbol"
    # Three distinct values from a caller that asked for one — no longer 97% anything.
    assert len({kinds[p] for p in kinds if p.startswith("backend/app/services")}) == 3


def test_reclassification_is_idempotent(db):
    """The migration recomputes from a path that has not moved, so re-running changes nothing."""
    cg.upsert_node(db, project_id="core", path="a/b.py", kind="file", name="b")
    db.commit()
    first = cg.list_nodes(db, "core")[0].kind
    cg.upsert_node(db, project_id="core", path="a/b.py", kind="file", name="b")
    db.commit()
    assert cg.list_nodes(db, "core")[0].kind == first == "file"
