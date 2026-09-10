"""An item declares what its work reaches, and a seat may not take the ones that leave the
repository (GRPH-832).

**The incident.** On 2026-09-08 a cheap-tier worker was handed an item whose touchpoints were
four ordinary repository files — a markdown page, a TypeScript source, a deploy script and
`vercel.json` — and whose description implied production. It rotated the production encryption
key, deleted rows, set production environment variables on a hosted platform and redeployed.
Its own evidence log records each step. It modified none of the four declared files. Nothing
was lost because the encrypted tables held zero rows, which is luck.

**The second path, which is the one that matters for the divvy.** A later wave's ops item was
never delegated at all — it was the highest-scored row in the project, so it sat at the top of
every worker's queue and a child SELF-CLAIMED it. That worker declined on its own judgment and
wrote a blocker. Judgment is not a control, and a fix that guarded only `delegate` would have
missed the path the incident actually used.

**What is being claimed here, and what is not.** Three layers, of decreasing strength:

1. a DECLARATION (`reach=deploy`) that no agent credential can set or clear, refused to every
   seat on every path — real, and dependent on somebody setting it;
2. prose SIGNALS that cost a delegator one deliberate, recorded argument — a heuristic, and
   deliberately not a boundary;
3. a sentence in the child's instruction — not a control at all, and tested in the fleet suite.

The tests below are grouped that way, and the last group is the calibration: a heuristic that
fires on everything is worse than none, because the acknowledgement becomes reflex.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Delegation, Enrolment, Item
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import reach as reach_svc


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Reach"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _item(client, key, title="ordinary work", description="", touchpoints=None) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "description": description,
        "touchpoints": touchpoints or ["backend/app/services/x.py"]}))["id"]


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


@pytest.fixture()
def planner(client, auth, key, proj):
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _agent(client, key, "planner", enrolment_code=code)


def _seated_worker(client, auth, key, proj) -> str:
    """A SPAWNED CHILD: registered on a minted seat, which is what makes it one."""
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["worker"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _agent(client, key, "child", enrolment_code=code)


def _mark_deploy(client, auth, item_key) -> None:
    r = client.patch(f"/api/items/{item_key}", json={"reach": "deploy"}, headers=auth)
    assert r.status_code == 200, r.text


# ---- 1. the declaration, which is the only real boundary here ---------------------------

def test_a_deploy_item_is_refused_to_a_delegator(client, auth, key, db, proj, planner):
    """THE ASK, verbatim from the report: say so at delegate time rather than relying on the
    model to notice."""
    item = _item(client, key, "configure Twitch EventSub env in production")
    _mark_deploy(client, auth, item)

    e = _err(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                            "agent_id": planner, "seat": True}))

    assert "reach=deploy" in e["message"]
    assert "yourself" in e["message"]
    db.expire_all()
    assert db.scalars(select(Delegation)).all() == [], "wrote a delegation it then refused"


def test_no_argument_gets_a_deploy_item_past_the_refusal(client, auth, key, db, proj, planner):
    """The acknowledgement exists for the HEURISTIC. Letting it override a declaration a person
    made would be this code second-guessing the one input it can actually trust."""
    item = _item(client, key, "rotate the prod key")
    _mark_deploy(client, auth, item)

    e = _err(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                            "agent_id": planner, "seat": True,
                                            "acknowledge_reach": True}))

    assert "reach=deploy" in e["message"]


def test_a_spawned_child_cannot_self_claim_a_deploy_item(client, auth, key, db, proj):
    """THE ONE THAT MATTERS, and the path the incident actually used. The ops item was never
    delegated — it was top of the queue on score and a child took it on its own.

    Sabotage: guard only `delegate` and this passes the item straight back."""
    item = _item(client, key, "set env vars on prod and redeploy")
    _mark_deploy(client, auth, item)
    child = _seated_worker(client, auth, key, proj)

    with pytest.raises(items_svc.ReachesOutsideTheRepo) as exc:
        items_svc.claim_item(db, items_svc.keys.resolve_item(db, item) or item, child)

    assert "a person does this one" in str(exc.value)


def test_the_divvy_skips_a_deploy_item_rather_than_stopping_at_it(
        client, auth, key, db, proj):
    """`claim_next` walks a SCORED queue and the reported item was at the top of it. If a
    `deploy` item stopped the sweep instead of being skipped, one ops row would idle the whole
    fleet — which is the GRPH-429 failure, re-created by a new filter."""
    top = _item(client, key, "ops: redeploy prod", touchpoints=["ops/deploy.sh"])
    _mark_deploy(client, auth, top)
    ordinary = _item(client, key, "ordinary", touchpoints=["svc/thing.py"])
    child = _seated_worker(client, auth, key, proj)

    got = items_svc.claim_next(db, child, project_id=proj)

    assert got is not None and got.key == ordinary


def test_a_persons_own_agent_may_still_take_it(client, auth, key, db, proj):
    """THE CONTROL. A person doing ops work through their own tools is the one thing this must
    leave possible — the item is `deploy` precisely because a human has to do it."""
    item = _item(client, key, "ops work", touchpoints=["ops/deploy.sh"])
    _mark_deploy(client, auth, item)
    mine = _agent(client, key, "my own window")  # no enrolment: not a spawned child

    got = items_svc.claim_item(db, items_svc.keys.resolve_item(db, item) or item, mine)

    assert got is not None and got.key == item


def test_a_persons_own_agent_is_allowed_even_when_its_role_is_worker(
        client, auth, db, proj):
    """WHY THIS IS KEYED ON THE SEAT AND NOT THE ROLE, pinned rather than argued.

    The obvious alternative — refuse `active_role == "worker"` — passes every other test in
    this file, because a key with all scopes registers as `all-in-one`. It is still wrong: a
    project key minted for one role registers its owner's own session as a `worker`, and that
    session belongs to a person sitting in front of it. Keying on the role would lock them out
    of the work the declaration exists to reserve FOR them.

    Sabotage that proves this test is not vacuous: swap `agent.enrolment_id` for
    `agent.active_role == "worker"` in `held_by_a_seat` — every other test here stays green
    and this one fails."""
    own_key = client.post("/api/api-keys",
                          json={"name": "my laptop", "project_id": proj,
                                "scopes": ["read", "write"]},
                          headers=auth).json()["plaintext"]
    item = _item(client, own_key, "ops work", touchpoints=["ops/deploy.sh"])
    _mark_deploy(client, auth, item)
    # A person's own session that says what it is doing. `gban setup` writes exactly this
    # shape, and a human running one window as a worker is an ordinary posture.
    mine = _agent(client, own_key, "my own window", role_hint="worker")
    db.expire_all()
    from app.models import Agent

    assert db.get(Agent, mine).active_role == "worker", "fixture no longer makes a worker"
    assert db.get(Agent, mine).enrolment_id is None, "fixture registered on a seat"

    got = items_svc.claim_item(db, items_svc.keys.resolve_item(db, item) or item, mine)

    assert got is not None and got.key == item


def test_only_a_signed_in_person_can_declare_it(client, auth, key, db, proj):
    """The boundary is which ROUTE carries the field, not a check that could be forgotten:
    `PATCH /api/items/{id}` takes a bearer JWT and no agent credential reaches it."""
    item = _item(client, key, "ops work")

    e = _err(_mcp(client, key, "update_item", {"id": item, "reach": "deploy"}))

    assert e["code"] == "validation"
    assert "signed-in person" in e["message"]


def test_the_mcp_refusal_is_a_refusal_and_not_a_silent_drop(client, auth, key, db, proj):
    """`reach` is not in `update_item`'s field whitelist, so passing it would have written
    nothing and returned 200. A caller that believes it marked an item as ops when it did not
    is worse off than one that was told no — the absence-reads-as-clean failure, in the tool
    that would produce it most quietly."""
    item = _item(client, key, "ops work")
    _err(_mcp(client, key, "update_item", {"id": item, "reach": "deploy"}))

    db.expire_all()
    row = db.get(Item, items_svc.keys.resolve_item(db, item) or item)
    assert row.reach == "repo", "the refusal was cosmetic and the write happened anyway"


def test_an_unknown_reach_is_refused_rather_than_kept_as_repo(client, auth, db, proj, key):
    """Silently keeping `repo` is the worst outcome available: the caller believes the item is
    marked, the divvy hands it to a child, and the refusal that should have fired reads as an
    absence."""
    item = _item(client, key, "ops work")

    r = client.patch(f"/api/items/{item}", json={"reach": "nowhere"}, headers=auth)

    assert r.status_code == 422
    assert "invalid reach" in r.text


# ---- 2. the signals, which ask rather than decide ----------------------------------------

def test_prose_that_reads_like_deployment_asks_once(client, auth, key, db, proj, planner):
    """Refused, with the item quoted back — the reader is deciding whether a process with a
    shell should act on this text, and the text is the only thing that settles it."""
    item = _item(client, key, "wire up EventSub", description=(
        "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET on Vercel production and redeploy."))

    e = _err(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                            "agent_id": planner, "seat": True}))

    assert "TWITCH_CLIENT_ID" in e["message"], "described the item instead of quoting it"
    assert "acknowledge_reach=true" in e["message"]


def test_the_acknowledgement_lets_it_through_and_is_recorded(
        client, auth, key, db, proj, planner):
    """One deliberate argument, on the record with an author and a timestamp. An acknowledgement
    nobody can look up afterwards is a dialog box."""
    item = _item(client, key, "docs", description="Document how we redeploy production.")

    _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                       "agent_id": planner, "seat": True,
                                       "acknowledge_reach": True}))
    db.expire_all()
    row = db.scalars(select(Delegation)).all()[0]

    assert row.reach_acknowledged is True


def test_an_ordinary_item_needs_no_acknowledgement(client, auth, key, db, proj, planner):
    """THE CONTROL that decides whether this can ship. Almost every item is ordinary, and a
    gate that made every delegation take an extra argument would be routed around by the end
    of the first wave."""
    item = _item(client, key, "one renderer", description=(
        "get_item_details dropped shared fields. Build it on items_svc.item_dict and add a "
        "ratchet test that fails when a field is added to one and not the other."))

    _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                       "agent_id": planner, "seat": True}))


def test_the_brief_carries_both_halves(client, auth, key, db, proj, planner):
    """The interesting case is the DISAGREEMENT: an item declaring `repo` whose text reads like
    a deployment is the shape of the incident, and a block reporting only the declaration would
    render it as `repo` and say nothing."""
    item = _item(client, key, "eventsub", description="Run `railway variables set X=1`.")

    out = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                             "agent_id": planner, "seat": True,
                                             "acknowledge_reach": True}))
    got = out["brief"]["reach"]

    assert got["value"] == "repo"
    assert got["signals"] and got["basis"]
    assert got["signals"][0]["rule"] == "platform-command"


# ---- 3. calibration: a heuristic that fires on everything is worse than none -------------

MUST_FIRE = {
    "the incident's own checklist": (
        "Go live checklist.\n"
        "- Rotate the production ENCRYPTION_KEY and re-encrypt existing rows\n"
        "- Run scripts/db-deploy.mjs against prod"),
    "the self-claimed ops item": (
        "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET on Vercel production and redeploy."),
    "a platform command": "Run `railway variables set SENTRY_DSN=...`, then `railway up`.",
    "a cluster rollout": "kubectl apply -f k8s/api.yaml and scale the deployment to 3 replicas.",
}

MUST_NOT_FIRE = {
    "ordinary backend work": (
        "get_item_details dropped shared fields. Build it on items_svc.item_dict so there is "
        "one renderer, and add a ratchet test."),
    "docs that discuss deployment": (
        "Document the deploy runbook in docs/releasing.md: which tag publishes which package, "
        "and why two workflow files are a PyPI constraint before they are a preference."),
    "reading a live system to debug": (
        "Fetch `vercel logs` and `kubectl get pods` for the failing request and work out "
        "which layer dropped the header."),
    "a migration": (
        "Add enrolments.prd_id (nullable) and set it at mint time. Prove the Alembic chain "
        "from empty on Postgres."),
}


@pytest.mark.parametrize("name", sorted(MUST_FIRE))
def test_it_fires_on_the_shapes_that_caused_this(name):
    """A detector with no false positives that also never fires is not a detector."""
    assert reach_svc.signals(MUST_FIRE[name]), name


@pytest.mark.parametrize("name", sorted(MUST_NOT_FIRE))
def test_it_stays_quiet_on_ordinary_work(name):
    """`vercel logs` and `kubectl get` are how a worker investigates. Refusing reads would make
    this fire on every diagnostic, and the acknowledgement would become reflex."""
    assert not reach_svc.signals(MUST_NOT_FIRE[name]), name


def test_a_match_is_scoped_to_one_line():
    """Line-scoped rather than whole-text, so a match cannot be assembled from two unrelated
    paragraphs. The first version fired on an item whose summary said "production" and whose
    acceptance criteria, forty lines later, said "delete"."""
    apart = "Summarise the production rollout.\n" + ("\n" * 5) + "Delete the temporary fixture."

    assert not reach_svc.signals(apart)


def test_each_rule_is_quoted_at_most_once():
    """Three quotes of one reading are one piece of evidence printed three times, and they
    crowd out the second rule — which is the one that would change a reader's mind."""
    repeated = "\n".join(["Run `railway up` now."] * 4 + ["Rotate the API token."])

    found = reach_svc.signals(repeated)

    assert len(found) == 2
    assert {s["rule"] for s in found} == {"platform-command", "secret-rotation"}
