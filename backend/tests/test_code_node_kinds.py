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


# ── doc and config (GRPH-381) ─────────────────────────────────────────────────

def test_prose_is_a_doc_not_a_file():
    """The files the graph could not represent at all.

    Measured on the live instance, 15 of 100 item touchpoints resolved to no node, and the set
    was dominated by exactly these: `docs/mcp.md` twice, `AGENTS.md`, `README.md`. They are
    load-bearing — the rules file every agent reads and the tool contract — so work touching
    them has a blast radius a human wants to see.
    """
    assert cg.kind_for_path("AGENTS.md") == "doc"
    assert cg.kind_for_path("docs/mcp.md") == "doc"
    assert cg.kind_for_path("README.md") == "doc"
    assert cg.kind_for_path("docs/design/notes.rst") == "doc"


def test_settings_files_are_config():
    assert cg.kind_for_path("docker-compose.yml") == "config"
    assert cg.kind_for_path("web/nginx.conf") == "config"
    assert cg.kind_for_path("backend/pyproject.toml") == "config"
    assert cg.kind_for_path("package.json") == "config"
    # Cursor rule files, which prime editor agents and were previously unrepresentable.
    assert cg.kind_for_path(".cursor/rules/graphban.mdc") == "config"


def test_extensionless_config_is_not_mistaken_for_a_package():
    # Without the basename table these read as directories — the same shape a package has.
    assert cg.kind_for_path("Dockerfile") == "config"
    assert cg.kind_for_path("backend/Dockerfile") == "config"
    assert cg.kind_for_path("Makefile") == "config"
    assert cg.kind_for_path(".gitignore") == "config"


def test_code_is_still_code():
    # The split must not drag source files out of `file` with it.
    assert cg.kind_for_path("backend/app/services/items.py") == "file"
    assert cg.kind_for_path("web/src/lib/queries.ts") == "file"
    assert cg.kind_for_path("web/src/index.css") == "file"


def test_doc_and_config_beat_the_code_table():
    """Order matters: `.md`, `.yml`, `.toml` and `.json` used to be IN the code table.

    If code were tested first they would keep resolving to `file` and this change would be a
    no-op that looked like a feature.
    """
    for path in ("a.md", "a.yml", "a.toml", "a.json"):
        assert cg.kind_for_path(path) != "file", path


def test_a_symbol_inside_a_doc_is_still_a_symbol():
    # `::` decides before any suffix does — a heading reference in a doc is not a config file.
    assert cg.kind_for_path("AGENTS.md::invariants") == "symbol"


def test_describe_code_accepts_and_corrects_doc_and_config(client, auth, db):
    key = _key(client, auth)
    res = _mcp(client, key, "describe_code", {
        "nodes": [
            {"path": "AGENTS.md", "kind": "file", "name": "agent guide"},
            {"path": "docker-compose.yml", "kind": "file", "name": "compose"},
            {"path": "README.md", "kind": "doc", "name": "readme"},
        ],
    })
    assert res["nodes_upserted"] == 3
    # The two that asked for `file` are told; the one that got it right is not.
    assert res["kind_corrections"] == [
        {"path": "AGENTS.md", "asked": "file", "stored": "doc"},
        {"path": "docker-compose.yml", "asked": "file", "stored": "config"},
    ]
    kinds = {n.path: n.kind for n in cg.list_nodes(db, "core")}
    assert kinds["AGENTS.md"] == "doc"
    assert kinds["docker-compose.yml"] == "config"
    assert kinds["README.md"] == "doc"


def test_the_map_can_be_filtered_to_docs(client, auth, db):
    """`get_code_map(kind=...)` is what makes the new kinds useful rather than decorative."""
    key = _key(client, auth)
    _mcp(client, key, "describe_code", {
        "nodes": [
            {"path": "AGENTS.md", "kind": "doc", "name": "guide"},
            {"path": "backend/app/services/items.py", "kind": "file", "name": "items"},
        ],
    })
    only_docs = _mcp(client, key, "get_code_map", {"kind": "doc"})
    assert [n["path"] for n in only_docs["nodes"]] == ["AGENTS.md"]


def test_an_area_that_is_not_a_path_still_resolves_to_nothing(db):
    """The honest limit, restated as a test.

    `doc` and `config` make repo FILES describable. They do nothing for areas that are not repo
    paths — `vercel env`, `twitch developer console`, `../ascme-labs/**` — which stay off-map by
    construction, exactly as PRD-20 D4 says. This item removes a blocker; it does not empty the
    tray, and claiming otherwise would be the reassuring reading.
    """
    for area in ("vercel env", "twitch developer console"):
        assert cg.kind_for_path(area) == "module"
        assert not cg.area_matches(area, "AGENTS.md")


# ── wrapper suffixes (GRPH-402) ───────────────────────────────────────────────

def test_a_templated_config_is_config_not_a_package():
    """Found by the PRD-20 acceptance walk's describe pass on the live instance.

    Matching on the FINAL suffix made `.template` win over `.conf`, so a templated config file
    read as `module` — the one kind that means "this contains other things", i.e. the shape a
    directory has.
    """
    assert cg.kind_for_path("web/nginx.conf.template") == "config"
    assert cg.kind_for_path("backend/settings.toml.example") == "config"
    assert cg.kind_for_path(".env.sample") == "config"


def test_a_templated_source_file_is_still_code():
    # Stripped and re-evaluated rather than added to the config table: a template of CODE
    # belongs with code, which a config-table entry could not express.
    assert cg.kind_for_path("deploy/settings.py.j2") == "file"
    assert cg.kind_for_path("web/index.html.tmpl") == "file"


def test_a_templated_doc_is_still_a_doc():
    assert cg.kind_for_path("docs/release-notes.md.template") == "doc"


def test_a_bare_wrapper_is_config_not_a_package():
    # Nothing underneath to classify. Calling it a package would repeat the same bug one level
    # down; a template of an unnamed thing is a config artifact.
    assert cg.kind_for_path("deploy.template") == "config"
    assert cg.kind_for_path("foo.example") == "config"


def test_stacked_wrappers_are_peeled():
    assert cg.kind_for_path(".env.example.template") == "config"


def test_an_interior_wrapper_needs_no_help():
    # `.json` already decides this correctly; the strip must not change the answer.
    assert cg.kind_for_path(".cursor/graphban-claim.example.json") == "config"


def test_a_wrapper_that_is_the_whole_name_is_not_stripped_to_nothing():
    # `len(low) > len(w)` guard: ".template" as a full basename must not become "".
    assert cg.kind_for_path(".template") in {"config", "module"}
    assert cg.kind_for_path("") == "module"
