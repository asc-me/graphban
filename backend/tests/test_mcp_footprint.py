"""AL-78: keep the MCP surface cheap for the agents that carry it.

Two costs an agent pays: the tool manifest shipped on every `tools/list`, and the
payload of every read. These tests pin the wins — a scope-gated manifest and lean
list rows — so they can't silently regress.
"""
import json

from app.mcp_server import TOOLS, _READ_ONLY


def _mint(client, auth, scopes):
    return client.post(
        "/api/api-keys", json={"name": "fp", "scopes": scopes}, headers=auth
    ).json()["plaintext"]


def _rpc(client, key, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()


def _list(client, key):
    return [t["name"] for t in _rpc(client, key, "tools/list")["result"]["tools"]]


def _call(client, key, tool, arguments):
    return _rpc(client, key, "tools/call",
               {"name": tool, "arguments": arguments})["result"]["structuredContent"]


# ---- Win #1: scope-gated manifest --------------------------------------------
def test_read_only_key_sees_only_read_tools(client, auth):
    """A key without the `write` scope can't call a mutation, so it shouldn't be
    shipped 16 write-tool schemas it would only get Forbidden on."""
    names = _list(client, _mint(client, auth, ["read"]))
    assert set(names) == set(_READ_ONLY)
    for write_tool in ("create_item", "update_item", "add_memory", "describe_code"):
        assert write_tool not in names


def test_write_key_sees_the_full_manifest(client, auth):
    names = _list(client, _mint(client, auth, ["read", "write"]))
    assert set(names) == {t["name"] for t in TOOLS}
    assert len(names) == len(TOOLS)


def test_get_context_tool_count_matches_the_scoped_manifest(client, auth):
    """The count get_context reports is what THIS key can call — not the server total —
    so it agrees with the manifest the agent actually received."""
    read_key = _mint(client, auth, ["read"])
    assert _call(client, read_key, "get_context", {})["tool_count"] == len(_READ_ONLY)
    write_key = _mint(client, auth, ["read", "write"])
    assert _call(client, write_key, "get_context", {})["tool_count"] == len(TOOLS)


# ---- Win #2: lean list rows, opt-in verbosity --------------------------------
def _seed_item(client, key):
    return _call(client, key, "create_item", {
        "title": "footprint fixture item",
        "touchpoints": ["backend/app/mcp_server.py"],
        "tags": ["footprint"],
        "effort": 3,
    })["id"]


def test_search_items_is_lean_by_default(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture"})
    assert page["results"], "fixture item should be found"
    row = page["results"][0]
    assert set(row) == {"id", "title", "status"}
    # the fat fields are absent by default
    for fat in ("touchpoints", "assignee", "claimed_by", "prd_id", "fidelity", "effort"):
        assert fat not in row


def test_search_items_full_opts_back_in(client, auth):
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "search_items", {"query": "footprint fixture", "fields": "full"})
    row = page["results"][0]
    assert row["touchpoints"] == ["backend/app/mcp_server.py"]
    assert "fidelity" in row and "effort" in row


def test_get_backlog_lean_keeps_the_ranking_signal(client, auth):
    """The prioritization fields are the reason to call get_backlog, so lean drops the
    fat item fields but never ready/score."""
    key = _mint(client, auth, ["read", "write"])
    _seed_item(client, key)
    page = _call(client, key, "get_backlog", {})
    assert page["results"], "backlog should not be empty after seeding"
    row = page["results"][0]
    assert {"id", "title", "status", "ready", "score"} <= set(row)
    assert "touchpoints" not in row  # fat field stays opt-in
    full = _call(client, key, "get_backlog", {"fields": "full"})["results"][0]
    assert "touchpoints" in full and "score" in full


def test_bad_fields_value_is_a_validation_error(client, auth):
    key = _mint(client, auth, ["read"])
    res = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "search_items", "arguments": {"fields": "verbose"}}},
        headers={"X-API-Key": key},
    ).json()["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["code"] == "validation"


# ---- regression guard: the manifest can't quietly bloat again ----------------
def test_manifest_stays_within_token_budget():
    """A ceiling, not an exact size — new tools are fine, unbounded prose is not.
    Measured ~7.4k tokens for the full manifest at the time of AL-78.

    Raised 9000 -> 9500 in AL-299, deliberately and with the numbers checked rather than
    as a reflex when it went red. PRD-14/15 added four tools (publish_memory,
    reject_memory, create_project, answer_grill) totalling ~3.7k chars; without them the
    manifest measures ~8.1k tokens, comfortably under the old ceiling. So the growth is
    tool COUNT, which this guard explicitly permits, not the verbosity it exists to
    catch.

    Prose was addressed first: ~740 chars came out of the four new descriptions and the
    shared effort/touchpoint strings before the ceiling moved at all. If this fires
    again, trim before raising — and check the same way, by subtracting whatever tools
    are new since the last bump.

    Raised 9500 -> 10500 in GRPH-254, by that same procedure. PRD-12's agent surface adds
    four tools (prd_acceptance, request_rebaseline, submit_verdict, close_prd) totalling
    ~3.9k chars; subtract them and the manifest measures ~9.0k tokens, comfortably under
    the old ceiling. They average 987 chars against 1001 for the existing 36, so this is
    tool COUNT again rather than the verbosity the guard exists to catch — and the count
    was already held down deliberately, since the eight acceptance READS share one tool
    behind a `view` argument instead of claiming eight names.

    ~1.0k chars came out of those four descriptions before the ceiling moved. The new
    headroom (~476 tokens) is the same slack the 9500 ceiling had before this change, so
    the guard stays as tight as it was rather than being loosened.

    Raised 10500 -> 11500 in GRPH-332, by the same procedure. PRD-17 D1 adds two tools
    (register_agent, fleet_status); subtract them and the manifest measures ~10474 tokens.
    Prose came out first and the numbers say it worked: the two started at 1240 chars each
    against a 995 average and were cut to 891, so they are now LEANER than the mean and the
    growth is tool COUNT, which this guard permits, rather than the verbosity it exists to
    catch.

    Worth recording what the measurement also showed: at the 10500 ceiling the surface had
    only ~26 tokens of headroom left, so tools had been added since GRPH-254 without anyone
    re-running this arithmetic. The new ~579 tokens restores roughly the slack the previous
    two bumps left, rather than buying room for another unchecked run of growth.

    Raised 11500 -> 12500 in GRPH-334, same procedure: D3 adds three tools (claim_review,
    sign_off, bounce); without them the manifest measures ~10921 tokens. Prose came out first
    and they now average 965 chars against 990 for the existing 44, so once again the growth
    is COUNT.

    **But this is the second raise for one PRD, and that is the interesting number.** PRD-17
    adds five tools in total and has moved this ceiling twice. The guard is doing its job by
    making that visible: the manifest is approaching the size where trimming it per ACTIVE
    ROLE stops being the "later nicety" PRD-17 D-b calls it. A worker never needs the three
    reviewer tools in its manifest, and a reviewer never needs `claim_next` — that is roughly
    a third of the fleet surface each agent is paying for and cannot use.

    So if this fires a third time, the answer is probably not a fourth raise. It is the
    role-gated manifest, which needs SSE on /api/mcp first (PRD-17 lists that as a non-goal
    precisely because it is a transport change, not a tweak).

    **Built in GRPH-337 follow-up, and the SSE caveat turned out to be half right.** Trimming
    per ACTIVE role does need a push channel — a role changes under a live connection and
    `tools/list` was fetched once at connect. But trimming per the KEY's eligible roles is
    static: a credential's ceiling is fixed at mint, so the manifest it is shipped can never
    go stale. That is also what D-b literally prescribes. A single-role fleet key now sees
    16-19% fewer tokens (see `test_mcp_role_manifest.py`), which is the whole fleet case since
    every key the Fleet view mints is single-role.

    This ceiling is unchanged and still measures the FULL manifest, deliberately: it is the
    worst case an unrestricted key pays, and role gating must not become an excuse to let that
    grow unwatched.

    Raised 12500 -> 12600 in GRPH-369, by the same procedure and for a smaller reason than any
    previous bump: PRD-19 adds NO tool, only `enrolment_code` on `register_agent` — 137 chars,
    which is ~34 tokens and exactly the overshoot. Prose was trimmed twice first (the tool
    description and the parameter text) before the ceiling moved, and the remainder cannot come
    out without losing what an agent needs to know. Minimal on purpose: E7 adds
    `mint_enrolment` and should have to justify its own raise rather than spend headroom voted
    for something else.

    **PRD-19 undermines the optimisation this guard was pointing at, and that is worth stating
    here rather than discovering later.** The role-gated manifest trims per the KEY's eligible
    roles — which works because the Fleet view mints single-role credentials. Under enrolment
    the recommended setup is ONE UNRESTRICTED credential for every agent, with the role granted
    per session; so the key's ceiling is `all three`, nothing trims, and every agent pays this
    full manifest again. Trimming per enrolment would work but the enrolment is not known until
    `register_agent` has run, which is the SSE problem D-b called a non-goal. So the next time
    this fires, the honest options are a real prose pass or that transport change — not another
    raise.

    Raised 12600 -> 12800 in GRPH-374, and this one IS the case the guard permits. PRD-19 E7
    adds `mint_enrolment` — one tool, ~211 tokens after trimming its description and dropping a
    redundant parameter doc. Subtract it and the manifest measures ~12534, under the previous
    ceiling: the growth is tool COUNT, which this test explicitly allows, not the verbosity it
    exists to catch. Checked by the same subtraction every earlier bump used.

    The note above still stands and is now the live problem rather than a warning: under
    enrolment every agent holds an UNRESTRICTED credential, so the role-gated manifest trims
    nothing and this full number is what everyone pays. The next raise should be argued
    against a real prose pass or per-enrolment trimming, not granted."""
    full_chars = len(json.dumps({"tools": TOOLS}))
    read_chars = len(json.dumps({"tools": [t for t in TOOLS if t["name"] in _READ_ONLY]}))
    assert full_chars // 4 < 12800, f"full manifest ~{full_chars // 4} tokens — trim descriptions"
    # scope-gating must keep buying its ~half-off for read keys
    assert read_chars < full_chars * 0.55


def test_a_session_scoped_manifest_actually_saves_the_tokens(client, auth):
    """The number O6 was decided on. An unrestricted credential is what enrolment recommends,
    so before E9 every agent carried the full manifest on every turn — the cost is per-request
    for the life of the session, not once at connect.

    Asserted as a floor rather than an exact figure: the manifest grows, and a test that
    pinned the saving to a percentage would fail for the wrong reason every time a tool
    lands."""
    proj = client.post("/api/projects", json={"name": "FootprintSession"},
                       headers=auth).json()["id"]
    plaintext = client.post("/api/api-keys", json={"name": "fleet", "project_id": proj},
                            headers=auth).json()["plaintext"]
    seat = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["reviewer"], "wave": "w1"},
                       headers=auth).json()["seats"][0]["code"]
    sid = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {}},
                      headers={"X-API-Key": plaintext}).headers["mcp-session-id"]
    full = _rpc(client, plaintext, "tools/list")["result"]["tools"]

    client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "register_agent",
                                             "arguments": {"label": "R",
                                                           "enrolment_code": seat}}},
                headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid})
    narrowed = client.post("/api/mcp",
                           json={"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                                 "params": {}},
                           headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid},
                           ).json()["result"]["tools"]

    saved = len(json.dumps(full)) - len(json.dumps(narrowed))
    assert saved > 0, "the whole point is that a registered reviewer carries less"
    assert saved / len(json.dumps(full)) > 0.10, (
        f"only {saved} chars saved — O6 was decided on 15-19%, and below ~10% the session "
        "machinery is not paying for itself")
