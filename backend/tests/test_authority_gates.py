"""Authority gates stay human; each gate has one adjudication path (AL-285 / PRD-14).

PRD-14 draws the line the product had been crossing inconsistently:

  A QUALITY gate asks *is this good?* — memory publish/reject, PRD approval, evidence.
  That is judgement, and an agent may hold it (AL-282).

  An AUTHORITY gate asks *are you allowed?* — account creation, credential minting,
  project creation, retag, org/plan/tenant boundaries. That is permission, and an agent
  must never self-serve it. In hosted mode it is a tenant-isolation property, not a
  preference.

These are guard tests in the sense `test_infra_identity.py` is: they encode a decision
so a later change has to argue with a red suite instead of quietly widening a boundary.
A failure here is a security regression, not a naming bug.

Written BEFORE AL-284 adds the one deliberate exception (agent-side project creation on
an unlinked self-host instance), so they pass on arrival and then constrain it.
"""
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture()
def agent_key(client, auth):
    """A maximally-privileged agent credential: read + write, pinned to a real project."""
    r = client.post("/api/api-keys",
                    json={"name": "agent", "scopes": ["read", "write"]}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["plaintext"]


# ---- invariant 1: authority gates are never agent-operable --------------------------
# Each case is named explicitly rather than looped over a registry. A loop passes
# happily after an entry is deleted from the registry, and deletion is exactly the
# change that matters here — the same failure mode `test_no_prefix_is_ever_dropped`
# was rewritten to avoid.

def _both_headers(api_key: str) -> list[dict]:
    """An agent presents its credential either way; neither may authenticate these."""
    return [{"X-API-Key": api_key}, {"Authorization": f"Bearer {api_key}"}]


def test_an_agent_key_cannot_mint_a_credential(client, agent_key):
    """The keystone. A credential that can mint credentials is an authority escalation
    with no ceiling — it would let an agent widen its own scope indefinitely."""
    for headers in _both_headers(agent_key):
        r = client.post("/api/api-keys", json={"name": "self-minted", "scopes": ["read", "write"]},
                        headers=headers)
        assert r.status_code == 401, (headers, r.status_code, r.text)


def test_an_agent_key_cannot_create_a_project(client, agent_key):
    """AL-284 adds a NARROW exception over MCP, gated on the instance not being linked
    to a cloud org. The REST endpoint stays human-only regardless."""
    for headers in _both_headers(agent_key):
        r = client.post("/api/projects", json={"name": "Conjured", "tag": "CJ"}, headers=headers)
        assert r.status_code == 401, (headers, r.status_code, r.text)


def test_an_agent_key_cannot_retag_a_project(client, agent_key):
    """Retag rewrites how every key in the project renders. One row, wide blast radius."""
    for headers in _both_headers(agent_key):
        r = client.post("/api/projects/core/retag", json={"tag": "XX"}, headers=headers)
        assert r.status_code == 401, (headers, r.status_code, r.text)


def test_an_agent_key_cannot_reach_the_org_or_platform_surfaces(client, agent_key):
    """Membership, plans and tenant boundaries. In hosted mode these ARE the isolation."""
    for path in ("/api/orgs", "/api/platform/admin/me"):
        for headers in _both_headers(agent_key):
            r = client.get(path, headers=headers)
            assert r.status_code in (401, 404), (path, headers, r.status_code, r.text)


def test_rest_authority_endpoints_require_a_jwt_not_a_key(client):
    """The mechanism behind the cases above: `get_current_user` resolves a Bearer ACCESS
    JWT only. If it ever learned to accept an API key, every test above would still pass
    for the wrong reason, so assert the mechanism directly."""
    deps = (APP / "security" / "deps.py").read_text()
    body = deps.split("def get_current_user", 1)[1].split("\ndef ", 1)[0]
    assert 'expected_type="access"' in body
    assert "api_key" not in body.lower(), (
        "get_current_user must not learn to accept an API key — that would silently move "
        "every authority endpoint within reach of an agent credential"
    )


def test_the_mcp_tool_surface_is_an_explicit_allowlist(client):
    """A ratchet, not a count. `test_api.py` already asserts the NUMBER of tools, which a
    swap leaves untouched; this asserts the SET, so adding or renaming a tool is a
    deliberate act a reviewer sees. If a new tool performs an authority action, it does
    not belong on the list at all — it belongs behind a human."""
    from app.mcp_server import TOOLS

    assert {t["name"] for t in TOOLS} == {
        "get_context", "list_projects", "setup_project", "create_item",
        # The ONE deliberate authority exception (AL-284), and it is narrow: refused in
        # hosted mode and refused once the instance is linked to a cloud org, so a
        # project can only be conjured where it cannot reach anyone else's tenant.
        "create_project",
        "update_item", "search_items", "add_memory", "search_memory",
        # A read of whether a published lesson is still worth following. Not a write and
        # not an authority gate — promote-to-org is REST/JWT only.
        "get_lessons",
        # PRD-17 D1. Both are QUALITY, not authority: `register_agent` announces a process
        # and gets told its role — it cannot choose one its credential does not permit, and
        # the role ceiling lives on the key a human minted. `fleet_status` is a read.
        "register_agent", "fleet_status",
        # D3. `sign_off` is the one that looks like an authority gate and is not: it moves an
        # item to `done`, but ONLY for an agent that did not build it, and the ban is keyed on
        # authorship rather than role so a promotion cannot launder it.
        "claim_review", "sign_off", "bounce",
        # D4. Both quality: a read of the partition, and a claim bounded by the same role
        # gate and lease machinery as `claim_next`.
        "collision_clusters", "claim_cluster",
        # D6. `assign_role` looks like an authority gate and is bounded by one: it cannot
        # grant a role the agent's CREDENTIAL is not eligible for, and credentials are minted
        # by a human. A planner reshuffles within the ceiling; it cannot raise it.
        "propose_allocation", "assign_role",
        # E7. An AUTHORITY gate, and bounded twice: planner-only, so the role holding it
        # cannot build and has nothing to launder; and clamped by the minting credential's
        # own ceiling, so it reshuffles authority rather than manufacturing it.
        "mint_enrolment",
        # PRD-22 §6 (GRPH-460). Also AUTHORITY, and deliberately the SAME gate as minting,
        # because §6's whole argument is that mint, list and retire are one capability with
        # one scope. Bounded three ways: planner-only, so it inherits E7's containment
        # exactly; scoped to `minted_by` the caller, so it reaches only seats it issued; and
        # it revokes CREDENTIALS, never processes — `agents_still_running` names the children
        # still building against dead seats, because a result that read as "the wave is over"
        # is what would leave them there.
        #
        # It exists because the asymmetry was the bug: spin-up was agent-callable and
        # spin-down was not, which fails in the direction that costs money.
        "retire_wave",
        # Quality gates, added deliberately by AL-282. `publish_memory` SUBMITS for
        # independent adjudication rather than publishing, so it is not self-approval.
        "publish_memory", "reject_memory",
        # AL-299: relays an author's answer into the grill; recorded as agent-supplied.
        "answer_grill",
        "get_backlog", "get_item_details", "suggest_next", "link_items",
        "unlink_items", "extract_lessons", "generate_digest", "prd_coverage",
        "decompose_prd", "create_prd", "update_prd", "grill_prd",
        # GRPH-519. QUALITY, not authority: it reads a PRD already inside the key's project
        # scope and writes nothing. It grants no new reach — `prd_coverage` and `decompose_prd`
        # both already read the same row — and it is what makes `update_prd`'s whole-body
        # replace survivable, because you cannot safely replace a document you have not read.
        "get_prd",
        "related_work", "next_cluster", "claim_next", "heartbeat",
        "release_item", "describe_code", "get_code_map", "code_neighbors",
        "search_code", "link_code", "unlink_code", "report_graphban_issue",
        # PRD-20 D8. QUALITY, not authority: it computes hubs/components/path over edges that
        # already exist and writes nothing. It can tell an agent what depends on the thing it
        # is about to change; it cannot grant that agent permission to change anything.
        "graph_query",
        # PRD-12's acceptance surface (GRPH-254). All four are QUALITY, not authority.
        # `prd_acceptance` is read-only. `request_rebaseline` asks for new intent and
        # explicitly does not grant it — approval is still earned by answering the grill,
        # so the agent surfaces the need and the interrogation decides. `submit_verdict`
        # records a claim WITH its provenance rather than a truth, and flags the signer
        # when they also implemented the work. `close_prd` is the one worth pausing on:
        # it is terminal and irreversible, but it answers "is this accounted for?", not
        # "are you allowed?" — the gate it enforces is mechanical (every undelivered
        # section dispositioned), it grants no permission and crosses no tenant boundary,
        # and it is scope-gated like every other write.
        "prd_acceptance", "request_rebaseline", "submit_verdict", "close_prd",
        # PRD-16's learning loop (GRPH-310). Both QUALITY. `learning_loop` is read-only.
        # `review_recommendation` is the interesting one: it is the human boundary itself,
        # and an agent reaching it looks like self-approval until you notice that approving
        # WRITES NOTHING — a shared_surgery artifact is only ever proposed, and the file
        # additive class installs into directories the machine already owns. It decides
        # "is this worth keeping", never "am I allowed", and it is scope-gated like every
        # other write. If that ever stops being true this entry should be revisited first.
        "learning_loop", "review_recommendation",
    }, "the agent-reachable surface changed — is the new tool a quality gate or an authority one?"


# ---- invariant 2: one adjudication path per gate ------------------------------------
# A gate must have a single implementation parameterised by WHO adjudicates, never a
# human branch and an agent branch. Two paths drift — which is precisely how PRD
# approval (agent-operable via update_prd) and memory publish (JWT-only) ended up with
# opposite stances without anyone deciding that.

_STATUS_ASSIGN = re.compile(r"^\s*(?:\w+\.)*shard\.status\s*=", re.MULTILINE)


def test_only_the_memory_service_moves_a_shard_between_states():
    """Every transition — human publish, auto-triage, undo, and the agent adjudication
    AL-282 adds — must funnel through `services/memory.py`. A router that sets the
    status inline is a second path, and the second path is where the drift starts."""
    # Exclude by PATH, not by filename: `routers/memory.py` is also called memory.py, and
    # a name-based exclusion silently exempts the one router most likely to grow a second
    # transition. (Caught by sabotage — the name-based version passed while a router set
    # the status inline.)
    offenders = [
        rel
        for p in APP.rglob("*.py")
        if (rel := p.relative_to(APP).as_posix()) != "services/memory.py"
        and _STATUS_ASSIGN.search(p.read_text())
    ]
    assert offenders == [], (
        f"{offenders} mutate a shard's status directly; route it through "
        "services/memory.py so there is one adjudication path"
    )


def test_the_human_review_endpoints_delegate_to_the_service():
    """The same invariant from the other side: the JWT-authenticated review routes are a
    thin authorisation wrapper over the shared transition, not their own implementation."""
    router = (APP / "routers" / "memory.py").read_text()
    assert "mem_svc.set_status" in router or "set_status(" in router
    assert not _STATUS_ASSIGN.search(router)


# ---- every tool argues its role gate (GRPH-516) ---------------------------------------------

def _tool_names():
    from app.mcp_server import TOOLS

    return {t["name"] for t in TOOLS}


def test_every_tool_is_classified_by_role():
    """THE guard, and the reason this ticket exists.

    `TOOLS` has had a completeness check for a long time — the assertion above, which forces
    a new tool to be classified as a quality gate or an authority one. `TOOL_ROLES` had no
    equivalent, so "no role gate" was a DEFAULT rather than a decision, and forty tools
    reached it without anyone arguing for one of them.

    The two axes are independent — `create_prd` is both agent-callable and planner-only — so
    the existing guard never covered this one, not even by accident.
    """
    from app.services.fleet import OPEN_TOOLS, TOOL_ROLES

    names = _tool_names()
    classified = set(TOOL_ROLES) | set(OPEN_TOOLS)
    assert names - classified == set(), (
        f"unclassified: {sorted(names - classified)} — gate the tool in TOOL_ROLES, or put it "
        "in OPEN_TOOLS with the reason it is open to every role")
    assert classified - names == set(), (
        f"classified but gone: {sorted(classified - names)} — a stale entry argues about a "
        "tool nobody can call")


def test_a_tool_is_gated_or_open_and_never_both():
    """Two answers to one question is the state this map exists to remove."""
    from app.services.fleet import OPEN_TOOLS, TOOL_ROLES

    assert set(TOOL_ROLES) & set(OPEN_TOOLS) == set()


def test_every_open_tool_gives_a_reason():
    """An empty reason is the default wearing a disguise. It has to say something, and the
    threshold is deliberately low — the guard is that somebody wrote a sentence, not that a
    grader liked it."""
    from app.services.fleet import OPEN_TOOLS

    thin = {n: r for n, r in OPEN_TOOLS.items() if len(r.strip()) < 30}
    assert not thin, f"no real reason given: {sorted(thin)}"


def test_the_unargued_list_can_shrink_and_not_grow():
    """A RATCHET, because the honest map has debt in it and debt that can grow is not debt.

    Seven tools are open because nobody has argued them, not because someone decided they
    should be. Writing a plausible-sounding rationale for each would have closed those
    questions wrongly and looked identical to having answered them. So they are marked, and
    a new tool cannot join them: this number may only go down.
    """
    from app.services.fleet import OPEN_TOOLS, UNARGUED

    unargued = sorted(n for n, r in OPEN_TOOLS.items() if r.startswith(UNARGUED))
    assert len(unargued) <= 7, (
        f"{len(unargued)} tools are open without an argument: {unargued}. A new tool must be "
        "gated or argued — it may not join this list.")


def test_register_agent_stays_open_and_says_why():
    """The one entry whose ABSENCE from TOOL_ROLES is load-bearing: a caller cannot hold a
    role before it registers, so gating this deadlocks every agent at its first call. Left
    implicit, a later tidy-up would gate it and the failure would look like a broken client."""
    from app.services.fleet import OPEN_TOOLS, TOOL_ROLES

    assert "register_agent" not in TOOL_ROLES
    assert "deadlock" in OPEN_TOOLS["register_agent"]


def test_heartbeat_stays_open_and_says_why():
    """It was gated, that was the bug, and it took reviewers and planners off the roster 150s
    after they registered — found on the PRD-17 walk. The reason belongs where the next person
    to tidy this file will read it."""
    from app.services.fleet import OPEN_TOOLS, TOOL_ROLES

    assert "heartbeat" not in TOOL_ROLES
    assert "presence" in OPEN_TOOLS["heartbeat"].lower()


def test_the_gates_that_exist_are_unchanged_by_this():
    """This ticket adds a guard, NOT a sweep. Gating four more tools today would be four more
    chances to repeat the heartbeat mistake, so the classification records what is true and
    changes no refusal."""
    from app.services.fleet import TOOL_ROLES

    assert TOOL_ROLES["claim_next"] == ("worker",)
    assert TOOL_ROLES["sign_off"] == ("reviewer",)
    assert TOOL_ROLES["mint_enrolment"] == ("planner",)
    assert set(TOOL_ROLES) >= {"claim_next", "claim_cluster", "next_cluster", "claim_review",
                               "sign_off", "bounce", "release_item", "assign_role",
                               "mint_enrolment", "retire_wave", "create_prd", "update_prd",
                               "decompose_prd", "grill_prd", "answer_grill"}
