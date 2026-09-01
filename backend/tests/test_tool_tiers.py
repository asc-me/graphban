"""Tiered tool exposure (GRPH-571).

Three things have to hold, and only one of them is the saving.

1. **A tool absent from the manifest still dispatches.** This is the guard the item names,
   and it is the reason to be careful rather than pleased: a manifest optimisation that
   quietly became an authorisation change would be a much worse defect than the token cost it
   set out to fix. Tested by calling a tool that is NOT in the manifest and checking the work
   actually happened, not merely that no error came back.

2. **An agent can find out what it is missing.** GRPH-580 was a scope that existed, worked,
   and could not be created, so the gate depending on it ran permanently on its weak path
   while looking correct. A `prd` tier nobody is told about is the same defect wearing
   different clothes.

3. The manifest is smaller, per tier, pinned.

The completeness guard is the one that keeps this true in a year: every tool must be
classified core or tiered, so tool 56 forces the question instead of landing in core by being
unlisted. `fleet.OPEN_TOOLS` exists because `TOOL_ROLES` had no such guard and "forty tools
reached that default without anyone arguing for it".
"""
import json

import pytest

from app.config import settings
from app.mcp_server import TOOLS, _READ_ONLY
from app.services import fleet as fleet_svc
from app.services import tool_tiers as tt

ALL_TIERS = list(tt.TIERS)


def _mint(client, auth, scopes=("read", "write"), tiers=None):
    body = {"name": "tiers", "scopes": list(scopes)}
    if tiers is not None:
        body["tool_tiers"] = list(tiers)
    return client.post("/api/api-keys", json=body, headers=auth).json()


def _rpc(client, key, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/api/mcp", json=body, headers={"X-API-Key": key}).json()


def _list(client, key):
    return [t["name"] for t in _rpc(client, key, "tools/list")["result"]["tools"]]


def _call(client, key, tool, arguments):
    return _rpc(client, key, "tools/call", {"name": tool, "arguments": arguments})["result"]


def _tokens(names):
    return len(json.dumps([t for t in TOOLS if t["name"] in names])) // 4


# ---- the completeness guard ------------------------------------------------------------

def test_every_tool_is_classified_exactly_once():
    """A tool in neither map lands in core by accident; a tool in both is ambiguous.

    Directional on purpose: the failure mode this defends is core growing back to the full
    manifest one unclassified tool at a time, which is invisible — everything works, the
    number just creeps.
    """
    names = {t["name"] for t in TOOLS}
    classified = set(tt.TOOL_TIERS) | set(tt.CORE_TOOLS)

    assert not (names - classified), (
        f"unclassified: {sorted(names - classified)} — put each in CORE_TOOLS with a reason, "
        "or in TOOL_TIERS"
    )
    assert not (classified - names), f"classified but not a tool: {sorted(classified - names)}"
    both = set(tt.TOOL_TIERS) & set(tt.CORE_TOOLS)
    assert not both, f"in both maps: {sorted(both)}"


def test_every_tier_named_in_the_map_exists():
    assert set(tt.TOOL_TIERS.values()) <= set(tt.TIERS)
    assert set(tt.TIER_PURPOSE) == set(tt.TIERS), "a tier with no stated purpose cannot be advertised"


def test_every_core_tool_states_why():
    """The reasons are the point of the map. A blank one is an unargued default with a
    comment character in front of it.

    Catches EMPTY and catches a reason that only restates the tool's own name. It cannot
    catch a weak argument, and a longer threshold does not either — the first version of this
    test required 20 characters and flagged nine entries whose reasons are fine
    ("finding the work", "as search_code"), which is a length check pretending to be a
    quality one.
    """
    for name, why in tt.CORE_TOOLS.items():
        assert why.strip(), f"{name} is core with no reason"
        assert why.strip().strip(".").replace("_", " ") != name.replace("_", " "), (
            f"{name}'s reason only restates its name"
        )


# ---- the guard that matters most -------------------------------------------------------

def test_a_tool_absent_from_the_manifest_still_dispatches(client, auth):
    """THE ONE THAT MATTERS. Tiering is a token optimisation and must never become an
    authorisation change.

    Asserts the WORK HAPPENED, not merely that no error came back: a call that no-ops
    silently would satisfy a check for `isError is False` and leave the tool unusable.
    """
    key = _mint(client, auth)["plaintext"]
    assert "create_prd" not in _list(client, key), "premise wrong — create_prd is not tiered"

    res = _call(client, key, "create_prd", {"title": "made by an untiered key"})

    assert not res.get("isError"), f"a tiered-out tool was refused: {res}"
    prd_id = res["structuredContent"]["id"]
    got = client.get(f"/api/prds/{prd_id}", headers=auth).json()
    assert got["title"] == "made by an untiered key", "the call returned ok and did nothing"


def test_the_scope_gate_still_refuses_what_it_always_did(client, auth):
    """The control for the test above. If tiering-does-not-refuse were implemented as
    "nothing refuses", that test would pass on a server with no authorisation at all."""
    key = _mint(client, auth, scopes=["read"])["plaintext"]

    res = _call(client, key, "create_item", {"title": "x"})

    assert res["isError"] is True
    assert "write" in json.dumps(res["structuredContent"])


# ---- the affordance --------------------------------------------------------------------

def test_get_context_names_the_tiers_this_key_lacks(client, auth):
    key = _mint(client, auth)["plaintext"]

    ctx = _call(client, key, "get_context", {})["structuredContent"]

    assert {m["tier"] for m in ctx["missing_tiers"]} == set(tt.TIERS)
    for m in ctx["missing_tiers"]:
        assert m["purpose"], f"{m['tier']} is advertised with no purpose"
        assert m["tools"], f"{m['tier']} is advertised with no tools"


def test_the_hint_says_the_tools_are_absent_rather_than_forbidden(client, auth):
    """The distinction an agent has to be told, because guessing wrong is expensive in both
    directions: an agent that reads 'missing' as 'forbidden' will not call a tool it is
    perfectly entitled to call, and will report itself blocked instead."""
    key = _mint(client, auth)["plaintext"]

    hint = _call(client, key, "get_context", {})["structuredContent"]["widen_hint"]

    assert "not forbidden" in hint.lower()
    assert "still works" in hint.lower()


def test_a_fully_tiered_key_is_told_nothing_is_missing(client, auth):
    """Absent rather than empty: a key holding everything should not pay for a field whose
    only possible value is 'nothing'."""
    key = _mint(client, auth, tiers=ALL_TIERS)["plaintext"]

    ctx = _call(client, key, "get_context", {})["structuredContent"]

    assert "missing_tiers" not in ctx
    assert "widen_hint" not in ctx


def test_the_advertised_tools_are_the_ones_the_tier_actually_grants(client, auth):
    """An advertisement that names the wrong tools is worse than none — the agent asks for a
    tier and does not get what it was promised."""
    plain = _mint(client, auth)["plaintext"]
    advertised = {m["tier"]: set(m["tools"])
                  for m in _call(client, plain, "get_context", {})["structuredContent"]["missing_tiers"]}
    base = set(_list(client, plain))

    for tier in tt.TIERS:
        widened = set(_list(client, _mint(client, auth, tiers=[tier])["plaintext"]))
        assert widened - base == advertised[tier], (
            f"{tier} advertises {sorted(advertised[tier])} and grants {sorted(widened - base)}"
        )


# ---- what each key actually receives ---------------------------------------------------

def test_the_default_manifest_is_core_only(client, auth):
    names = set(_list(client, _mint(client, auth)["plaintext"]))
    assert names == set(tt.CORE_TOOLS)


def test_a_tier_adds_exactly_its_own_tools(client, auth):
    base = set(_list(client, _mint(client, auth)["plaintext"]))
    names = set(_list(client, _mint(client, auth, tiers=["prd"])["plaintext"]))

    assert names - base == {n for n, tier in tt.TOOL_TIERS.items() if tier == "prd"}
    assert not base - names, "granting a tier removed something"


def test_all_tiers_is_the_untiered_manifest(client, auth):
    """The worst case is unchanged, which is what makes this safe to deploy: nobody who asks
    for everything is worse off than before."""
    names = set(_list(client, _mint(client, auth, tiers=ALL_TIERS)["plaintext"]))
    assert names == {t["name"] for t in TOOLS}


def test_the_scope_gate_still_runs_first(client, auth):
    """Tiering composes with scope rather than replacing it — a read-only key with every tier
    still sees no write tools."""
    names = set(_list(client, _mint(client, auth, scopes=["read"], tiers=ALL_TIERS)["plaintext"]))
    assert names <= set(_READ_ONLY)
    assert "create_item" not in names


def test_an_unknown_tier_is_refused_at_mint(client, auth):
    """For the reason an unknown scope is: a key that mints fine and never widens fails much
    later, as a tool the agent cannot see and cannot explain."""
    res = client.post("/api/api-keys",
                      json={"name": "bad", "tool_tiers": ["prds"]}, headers=auth)
    assert res.status_code == 422
    assert "prds" in res.text and "allowed" in res.text


# ---- the operator's undo ----------------------------------------------------------------

def test_the_default_setting_restores_the_old_manifest(client, auth, monkeypatch):
    """An existing key's manifest shrinks the moment 0093 deploys. This is how an operator
    puts it back without a code change while they work out which keys need what."""
    monkeypatch.setattr(settings, "mcp_default_tool_tiers", ",".join(ALL_TIERS))

    names = set(_list(client, _mint(client, auth)["plaintext"]))

    assert names == {t["name"] for t in TOOLS}


def test_the_setting_only_ever_widens(client, auth, monkeypatch):
    """Additive, so an operator setting it cannot take a tier off a key that was minted with
    one — a config value that silently narrowed a credential would be the same class of
    surprise the posture field exists to prevent."""
    monkeypatch.setattr(settings, "mcp_default_tool_tiers", "misc")

    names = set(_list(client, _mint(client, auth, tiers=["prd"])["plaintext"]))

    assert {n for n, t in tt.TOOL_TIERS.items() if t == "prd"} <= names
    assert {n for n, t in tt.TOOL_TIERS.items() if t == "misc"} <= names
    assert not any(tt.TOOL_TIERS.get(n) == "fleet" for n in names)


# ---- fleet credentials get what they need ------------------------------------------------

@pytest.mark.parametrize("role", ["planner"])
def test_a_fleet_credential_is_minted_with_the_tier_it_needs(client, auth, role):
    """A planner without `assign_role` cannot allocate. It would not error — the tool would
    simply not be in the manifest, which reads as it not existing.

    The reviewer was here and was REMOVED, which is the more interesting half. Granting a
    reviewer its tools at fleet-mint works for a Fleet-view credential and silently does not
    for an ENROLLED one, because a seat is taken on a hand-minted key that has no tiers — the
    same agent, two ways in, two different manifests. `review_recommendation` is core instead;
    `test_an_enrolled_reviewer_is_shipped_the_read_its_job_needs` is the guard.
    """
    proj = client.post("/api/projects", json={"name": f"Tier{role}"}, headers=auth).json()["id"]
    key = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": role, "wave": "w1"},
                      headers=auth).json()

    # Asserted on the MANIFEST rather than on the response field. The field being set and the
    # filter not consulting it look identical from outside, and the field is not the claim —
    # what the credential is shipped is.
    names = set(_list(client, key["plaintext"]))
    granted = {n for n, t in tt.TOOL_TIERS.items() if t == "fleet"}
    # Intersected with the ROLE gate, which runs first and legitimately removes the other
    # roles' tools: a reviewer never sees `assign_role` however its tiers read.
    assert names & granted, f"a {role} was shipped none of the fleet tier: {sorted(granted)}"


#: The verbs `fleet.TOOL_ROLES` reserves to the planner — "PRD authorship is the planner's".
PLANNER_AUTHORSHIP = ("create_prd", "update_prd", "grill_prd", "answer_grill", "decompose_prd")


@pytest.mark.parametrize("role", ["planner", fleet_svc.ALL_IN_ONE])
def test_the_authority_the_role_grants_the_credential_must_show(client, auth, role):
    """The dead reservation GRPH-571 shipped with.

    Authorship is planner-ONLY by role and `prd`-gated by tier, and `fleet.mint` granted
    only `fleet` — so two ceilings that never mention each other composed into "no fleet
    credential may author", and the role gate's careful reservation became dead code the
    day it landed. The absence was invisible from either file alone; only the shipped
    manifest can see it.

    Asserted PER TOOL, not as a non-empty intersection: `names & granted` being truthy is
    exactly how the test above could pass while this capability was missing from every
    planner credential in the product.
    """
    proj = client.post("/api/projects", json={"name": f"Author{role}"}, headers=auth).json()["id"]
    key = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": role, "wave": "w1"},
                      headers=auth).json()

    names = set(_list(client, key["plaintext"]))
    for needed in PLANNER_AUTHORSHIP:
        assert needed in names, f"a {role} was shipped no {needed}"


def test_an_enrolled_reviewer_is_shipped_the_read_its_job_needs(client, auth):
    """The path that made `review_recommendation` core.

    PRD-19's recommended setup is one hand-minted credential per agent with the role granted
    per session — so anything a reviewer needs that lives in a tier is a tool it will never
    be shipped, however carefully `fleet.mint` grants tiers.
    """
    proj = client.post("/api/projects", json={"name": "TierEnrol"}, headers=auth).json()["id"]
    plaintext = client.post("/api/api-keys",
                            json={"name": "enrol", "project_id": proj},
                            headers=auth).json()["plaintext"]
    seat = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["reviewer"], "wave": "w1"},
                       headers=auth).json()["seats"][0]["code"]
    sid = client.post("/api/mcp",
                      json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                      headers={"X-API-Key": plaintext}).headers["mcp-session-id"]
    client.post("/api/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "register_agent",
                                 "arguments": {"label": "R", "enrolment_code": seat}}},
                headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid})
    names = {t["name"] for t in client.post(
        "/api/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={"X-API-Key": plaintext, "Mcp-Session-Id": sid},
    ).json()["result"]["tools"]}

    for needed in ("claim_review", "sign_off", "bounce", "review_recommendation"):
        assert needed in names, f"an enrolled reviewer was not shipped {needed}"


def test_a_worker_credential_runs_on_core(client, auth):
    """The control: if every fleet role got the tier, granting it would prove nothing. A
    worker claims, works and hands back — none of that is fleet administration."""
    proj = client.post("/api/projects", json={"name": "TierWorker"}, headers=auth).json()["id"]
    key = client.post("/api/fleet/keys",
                      json={"project_id": proj, "role": "worker", "wave": "w1"},
                      headers=auth).json()

    assert not (key.get("tool_tiers") or [])
    names = set(_list(client, key["plaintext"]))
    assert not (names & set(tt.TOOL_TIERS)), "a worker was shipped tiered tools"


# ---- the sizes, pinned ------------------------------------------------------------------

#: What core measures TODAY. A literal somebody typed, for the reason `MEASURED_TOKENS` is
#: one: a figure derived the same way on both sides agrees by construction and can never
#: fail. This one disagrees the moment core moves, which forces whoever moved it to re-run
#: the arithmetic — and moving core is the thing worth noticing, since core is what every
#: key pays.
CORE_TOKENS = 9066
# 8974 -> 9066 (GRPH-P31 / GRPH-616). `get_context` now carries gitops: unmeasured is not
# main, and control is present when linked including linked_unreachable. Compact
# outputSchema (`{"type": "object"}`); the sentences are the bill every key pays.

#: Per tier, so a tier cannot quietly grow back to the untiered weight — the item's
#: acceptance, in a form that fails when it stops being true.
TIER_TOKENS = {"prd": 2001, "codegraph": 1030, "fleet": 799, "misc": 1288}


def test_the_core_manifest_is_the_size_recorded():
    measured = _tokens(set(tt.CORE_TOOLS))
    assert measured == CORE_TOKENS, (
        f"core is {measured} tokens, recorded as {CORE_TOKENS}. Every key pays this. If a "
        "tool moved into core, argue for it in CORE_TOOLS and update the number; if one grew, "
        "that is the growth this exists to make visible."
    )


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_each_tier_is_the_size_recorded(tier):
    measured = _tokens({n for n, t in tt.TOOL_TIERS.items() if t == tier})
    assert measured == TIER_TOKENS[tier], (
        f"{tier} is {measured} tokens, recorded as {TIER_TOKENS[tier]}"
    )


def test_the_default_manifest_is_much_smaller_than_the_untiered_one():
    """The acceptance, as a floor rather than an exact ratio so it does not fail for the
    wrong reason every time a tool lands.

    **The threshold is 0.7 and it started at 0.6, which is the honest history.** The first
    split measured core at 52% off the untiered manifest. Four existing guards then proved
    four separate tools had to be core after all — `test_mcp_annotations` on `get_prd`
    ("absent from a manifest looks like a tool that does not exist"), `test_setup_project` on
    the bootstrap `get_context` points at, and the role-manifest suite on the worker's cluster
    verbs and the shared fleet reads. Each correction moved a tool from a tier into core and
    the saving fell to 38%.

    That is the number, and it is the one worth having: a 52% saving that costs a solo agent
    the ability to read the spec it is working to is not a saving. Raised deliberately rather
    than by reflex when it went red — the same discipline the ceiling in
    `test_mcp_footprint.py` demands of itself.
    """
    full = len(json.dumps({"tools": TOOLS})) // 4
    core = _tokens(set(tt.CORE_TOOLS))
    assert core < full * 0.7, f"core {core} vs full {full} — tiering is not paying for itself"
    # ~5,100 tokens per agent per turn, which is what this is actually worth.
    assert full - core > 4000, f"only {full - core} tokens saved"
