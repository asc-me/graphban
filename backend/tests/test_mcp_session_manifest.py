"""PRD-19 E9 — the manifest narrows to the SESSION's role, after registration (GRPH-398).

`tools/list` is fetched at connect, before `register_agent` runs, so it could only ever be
trimmed by the CREDENTIAL. Enrolment made the recommended credential unrestricted — one key,
roles from seats — so nothing trimmed and every agent carried all 52 tools on every turn.

Everything here is ADVISORY. The call-time role gate is untouched and remains the only
enforcement; a client that ignores the session id pays tokens and nothing else. The tests that
matter most in this file are therefore the ones asserting that a trimmed manifest does not
change what a caller may DO.
"""
import json

import pytest

from app.models import Agent


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "SessionManifest"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    """UNRESTRICTED — the credential PRD-19 recommends, and the one that trimmed nothing."""
    return client.post("/api/api-keys", json={"name": "fleet", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _rpc(client, key, method, params=None, sid=None):
    headers = {"X-API-Key": key}
    if sid:
        headers["Mcp-Session-Id"] = sid
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/api/mcp", json=body, headers=headers)


def _tools(client, key, sid=None):
    return [t["name"] for t in _rpc(client, key, "tools/list", sid=sid).json()["result"]["tools"]]


def _register(client, key, sid=None, **args):
    r = _rpc(client, key, "tools/call",
             {"name": "register_agent", "arguments": args}, sid=sid).json()
    return json.loads(r["result"]["content"][0]["text"])


def _seat(client, auth, proj, role):
    out = client.post("/api/fleet/seats",
                      json={"project_id": proj, "roles": [role], "wave": "w1"},
                      headers=auth).json()["seats"]
    return out[0]["code"]


def test_initialize_issues_a_session_id(client, key):
    """E9a. Without one the server cannot tell two agents on one credential apart — which is
    the same weakness `role_for_call` works around by resolving an unnamed caller to
    `unidentified`."""
    r = _rpc(client, key, "initialize", {"protocolVersion": "2025-06-18"})

    assert r.headers.get("mcp-session-id")
    assert r.json()["result"]["serverInfo"]["name"] == "graphban"


def test_the_manifest_narrows_to_the_registered_role(client, auth, proj, key):
    """The point of the whole slice. Same credential, same connection — but after the agent
    says who it is, the list stops carrying the other roles' tools."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    before = _tools(client, key, sid=sid)
    assert "claim_cluster" in before and "sign_off" in before

    _register(client, key, sid=sid, label="R", enrolment_code=_seat(client, auth, proj, "reviewer"))

    after = _tools(client, key, sid=sid)
    assert "sign_off" in after, "a reviewer keeps its own tools"
    assert "claim_cluster" not in after, "and stops carrying the worker's"
    assert len(after) < len(before)


def test_a_worker_and_a_reviewer_on_one_credential_get_different_manifests(
        client, auth, proj, key):
    """The configuration enrolment exists for: ONE credential, roles from seats. Before this,
    both agents received an identical untrimmed list."""
    w_sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    r_sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    _register(client, key, sid=w_sid, label="W",
              enrolment_code=_seat(client, auth, proj, "worker"))
    _register(client, key, sid=r_sid, label="R",
              enrolment_code=_seat(client, auth, proj, "reviewer"))

    worker, reviewer = _tools(client, key, sid=w_sid), _tools(client, key, sid=r_sid)

    assert "claim_cluster" in worker and "claim_cluster" not in reviewer
    assert "sign_off" in reviewer and "sign_off" not in worker


def test_a_client_that_sends_no_session_id_sees_what_it_always_saw(client, auth, proj, key):
    """Nothing regresses for a client that predates this or drops the header — and that is
    most of them until they are probed."""
    _register(client, key, label="W", enrolment_code=_seat(client, auth, proj, "worker"))

    tools = _tools(client, key)

    assert "claim_cluster" in tools and "sign_off" in tools


def test_an_unregistered_session_sees_the_full_manifest(client, key):
    """The connect-time fetch, which happens before any agent exists. Trimming it would mean
    guessing, and a wrong guess hides a tool the caller may legitimately need."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]

    assert "sign_off" in _tools(client, key, sid=sid)


def test_two_agents_on_one_connection_are_not_guessed_between(client, auth, proj, key):
    """Real case: an orchestrator and the subagent it spawns can share a transport. Picking one
    would hand somebody a manifest trimmed for the other — so it declines instead.

    This is why the whole feature is safe: the answer to "cannot tell" is today's answer."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    parent = _register(client, key, sid=sid, label="P",
                       enrolment_code=_seat(client, auth, proj, "worker"))
    _register(client, key, sid=sid, label="S", parent_agent_id=parent["agent_id"],
              enrolment_code=_seat(client, auth, proj, "reviewer"))

    tools = _tools(client, key, sid=sid)

    assert "claim_cluster" in tools and "sign_off" in tools


def test_trimming_never_decides_what_may_be_called(client, auth, proj, key, db):
    """THE safety property. A manifest can only fail to mention a tool; the role gate refuses
    it. If this ever inverts — "not in the manifest, therefore not callable" — the gate has
    moved somewhere a client controls, which is GRPH-377 from the other direction.

    So: a reviewer whose manifest omits `claim_cluster` must still be refused BY THE GATE when
    it calls it anyway, with a role refusal rather than an unknown-tool error."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    me = _register(client, key, sid=sid, label="R",
                   enrolment_code=_seat(client, auth, proj, "reviewer"))
    assert "claim_cluster" not in _tools(client, key, sid=sid)

    r = _rpc(client, key, "tools/call",
             {"name": "claim_cluster", "arguments": {"agent_id": me["agent_id"]}},
             sid=sid).json()

    text = r["result"]["content"][0]["text"]
    assert "unauthorized" in text and "reviewer" in text


def test_an_expired_seat_does_not_narrow_the_manifest(client, auth, proj, key, db):
    """An expired session grants no role, and the agent needs `fleet_status` to collect the
    directive telling it to re-enrol. Narrowing to a role it no longer holds would trim the
    manifest toward a session that is already over."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    me = _register(client, key, sid=sid, label="R",
                   enrolment_code=_seat(client, auth, proj, "reviewer"))
    client.post("/api/fleet/end-wave", json={"project_id": proj, "wave": "w1"},
                headers=auth)

    tools = _tools(client, key, sid=sid)

    assert "claim_cluster" in tools and "sign_off" in tools
    assert db.get(Agent, me["agent_id"]).mcp_session_id == sid


def test_an_all_in_one_agent_keeps_the_whole_manifest(client, key):
    """The single-agent posture is unrestricted by definition, so there is nothing to trim —
    and trimming it would be the silent downgrade O3 exists to prevent, arriving as a tool
    list instead of a role badge."""
    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    _register(client, key, sid=sid, label="solo")

    tools = _tools(client, key, sid=sid)

    assert "claim_cluster" in tools and "sign_off" in tools


def test_a_registered_session_still_narrows_without_the_probe(client, auth, proj, key, db):
    """What the retired probe was watching, kept as a behavioural assertion.

    `tools_list_refetched` existed to answer one question — does a real client re-fetch its
    manifest unprompted? — and it did: Grok Build shell asked again within 20s of registering
    and took the narrowed list, which closed E9c. The instrument came out on 2026-08-20 rather
    than costing an event row per narrowed fetch forever.

    Deleting it must not quietly delete the coverage, so the case it exercised is asserted on
    the RESULT instead of on a side effect: a `tools/list` after registration narrows, and
    nothing needs recording for that to be true."""
    from app.models import Event

    sid = _rpc(client, key, "initialize").headers["mcp-session-id"]
    before = _tools(client, key, sid=sid)
    _register(client, key, sid=sid, label="R",
              enrolment_code=_seat(client, auth, proj, "reviewer"))

    after = _tools(client, key, sid=sid)

    assert "claim_cluster" in before and "claim_cluster" not in after
    assert not db.query(Event).filter(Event.action == "tools_list_refetched").all(), \
        "the probe is retired — a re-fetch must leave no event behind"


# ---- the boundary, stated at the moment the role is granted -----------------------------------

def test_register_names_the_tools_the_role_will_be_refused(client, auth, proj, key):
    """The manifest cannot say this: `tools/list` is fetched at connect, before any role
    exists, so a fleet agent holds all 52 tools all session and finds the edge by walking into
    it — and three refusals in a row is how `quarantine` decides an agent has stopped
    listening, so discovering the boundary by trial costs the agent its place in the fleet."""
    out = _register(client, key, label="R", enrolment_code=_seat(client, auth, proj, "reviewer"))

    assert "claim_cluster" in out["tools_off_limits"]
    assert "claim_next" in out["tools_off_limits"]
    assert "sign_off" not in out["tools_off_limits"], "its own tools are not off limits"


def test_a_worker_is_told_a_different_boundary(client, auth, proj, key):
    """Same call, opposite answer — otherwise it is a constant wearing a field's name."""
    out = _register(client, key, label="W", enrolment_code=_seat(client, auth, proj, "worker"))

    assert "sign_off" in out["tools_off_limits"] and "claim_review" in out["tools_off_limits"]
    assert "claim_cluster" not in out["tools_off_limits"]


def test_an_all_in_one_agent_is_told_nothing_is_off_limits(client, key):
    """An empty list rather than a reassuring sentence: the single-agent posture really is
    unrestricted, and a non-empty answer here would be the silent downgrade O3 warns about,
    arriving as advice."""
    out = _register(client, key, label="solo")

    assert out["tools_off_limits"] == []


def test_the_list_matches_what_the_gate_actually_refuses(client, auth, proj, key):
    """The failure this guards is drift: a list that says one thing while the gate does
    another is worse than no list, because an agent would trust it. Asserted against
    TOOL_ROLES itself so a new gated tool cannot be added without appearing here."""
    from app.services import fleet

    out = _register(client, key, label="R", enrolment_code=_seat(client, auth, proj, "reviewer"))

    for name in out["tools_off_limits"]:
        assert "reviewer" not in fleet.TOOL_ROLES[name], f"{name} is not actually refused"
    refused = {n for n, roles in fleet.TOOL_ROLES.items() if "reviewer" not in roles}
    assert set(out["tools_off_limits"]) == refused, "every refused tool is named, not a sample"
