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

import re

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
    # GRPH-1037. Command-shaped, including the forms people actually write. Ignoring
    # backticks or fences would make every one of these quiet, which is the guard
    # declining the case it exists for. `docker compose` is not a platform name plus a
    # word — `compose/Railway` is a legend — so it is matched as the two-word CLI.
    # Sabotage: drop the platform-command rule and these fail.
    "railway up with a flag": "railway up --service api",
    "docker compose down": "docker compose down",
    "a kubectl rollout restart": "kubectl rollout restart deployment/api",
    "railway up, backticked": "`railway up --service api`",
    "docker compose down, backticked": "`docker compose down`",
    "a kubectl rollout, backticked": "`kubectl rollout restart deployment/api`",
    "railway up, fenced": "```\nrailway up --service api\n```",
    "docker compose down, fenced": "```\ndocker compose down\n```",
    "a kubectl rollout, fenced": "```\nkubectl rollout restart deployment/api\n```",
    # Bounce of 4f38bde1. The command-shaped matcher went quiet on commands that
    # wipe a database. `reset` is a verb. `pg:reset` and `config:set` are one
    # colon token — `_WORD` cannot cross `:`. `maintenance:on` is the pin that
    # the tail need not be a listed verb (`on` is a function word when it stands
    # alone). `dump` is intentionally not a verb, so `db dump --linked` pins the
    # two-word flag window and nothing else.
    "supabase db reset": "supabase db reset",
    "supabase db reset --linked": "supabase db reset --linked",
    "heroku pg:reset": "heroku pg:reset",
    "heroku config:set": "heroku config:set FOO=1",
    "a colon tail that is not a verb": "heroku maintenance:on",
    "a flag within two words": "supabase db dump --linked",
    "helm install": "helm install rel chart",
    "helm uninstall": "helm uninstall rel",
    "kubectl edit": "kubectl edit deployment/api",
    "kubectl autoscale": "kubectl autoscale deployment/api --min=1 --max=5",
    "terraform force-unlock": "terraform force-unlock LOCKID",
    "aws terminate-instances": "aws ec2 terminate-instances --instance-ids i-1",
    "supabase migration repair": (
        "supabase migration repair 20240101000000 --status reverted"),
    "netlify env:set": "netlify env:set KEY 1",
    "docker compose with a flag before the verb": "docker compose --profile prod down",
    # `rb` is not a verb. The bucket uri sits between it and `--force`, so a flag
    # that has to be the next token misses a bucket delete.
    "aws s3 rb with the bucket before the flag": "aws s3 rb s3://bucket --force",
    # Group, subgroup, resource, verb — one past `gcloud compute instances delete`.
    "az network nsg rule delete": "az network nsg rule delete --name r",
    # Bounce of 1ee5735b. `remove` and `detach` were missing from the verb
    # allowlist, so these went quiet while the base matcher fired. The matcher
    # is the base again: the first word after the platform is enough.
    "vercel remove": "vercel remove my-app",
    "vercel remove, in a sentence": "Run vercel remove my-app on the prod team",
    "fly postgres detach": "fly postgres detach",
    "fly apps remove": "fly apps remove x",
    "railway service remove": "railway service remove",
    # Bounce of 3efc3177. Punctuation after determiner+noun is an imperative's
    # object, not a subject. A passive after the modal is the instruction.
    # A path-prefixed binary before "(" is not a legend.
    "delete the vercel project": "Delete the vercel project.",
    "wipe the supabase project": "Wipe the supabase project.",
    "drop the supabase projects": "Drop the supabase projects!",
    "remove the heroku teams": "Remove the heroku teams.",
    "tear down the kubectl projects": "Tear down the kubectl projects, now",
    "the vercel project will be removed": "THE VERCEL PROJECT WILL BE REMOVED",
    "path-prefixed railway projects": "Run ./bin/railway projects (prod)",
    # Bounce of 2f9a974b. A seven-word participle list goes quiet on every
    # other passive. The shape is the auxiliary, an adverb, `to be`, a
    # get-passive, a copula, or a destructive verb later in the clause.
    "will be purged": "The supabase project will be purged.",
    "will be decommissioned": "The railway project will be decommissioned.",
    "must be truncated": "The supabase project must be truncated.",
    "will be nuked": "The vercel project will be nuked.",
    "will be shut down": "The railway project will be shut down.",
    "adverb before be": "The vercel project should now be deleted.",
    "adverb before be, soon": "The heroku project will soon be deleted.",
    "has to be deleted": "The heroku teams has to be deleted.",
    "is to be removed": "The vercel project is to be removed.",
    "is going to be deleted": "The supabase project is going to be deleted.",
    "will get wiped": "The supabase project will get wiped tonight.",
    "copula plus participle": "This vercel project is removed on Friday.",
    "imperative later in the clause": (
        "The heroku project was deleted, so delete it again."),
    # The compound the later-verb check owns. No passive shape. Drop
    # `_destructive_later` and this goes quiet; `will deploy` stays quiet
    # because deploy is the modal's complement, not a later word.
    "destructive verb later in the clause": (
        "The railway project will ship next week, so drop the database."),
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
    # GRPH-989. `render` is a platform name AND the ordinary English verb for what a UI does,
    # and the rule matched it followed by any lowercase word. These three sentences refused a
    # Settings panel three times in one day: the item being built, a note explaining the trap,
    # and GRPH-989 itself, which had to quote the phrase in order to report it.
    "UI prose about rendering": (
        "A failed status read must not render as \"off\". Off and unknown are different "
        "states, and this repo ships the wrong one by default."),
    "a confident zero while pending": (
        "The counts render a confident 0 while the request is still pending, so a slow read "
        "and an empty project look the same."),
    "an em dash instead of a number": (
        "An unavailable counter should render an em dash rather than a zero."),
    # GRPH-1037. Naming a platform in a diagram is not invoking it. The base
    # matcher treats `Railway stack` as `railway` with the subcommand `stack`.
    # Two spans keep that quiet, and each one owns a fixture. Delete
    # `_LEGEND_SPAN` and the legend plus the symptom fail. Delete the subject
    # scrub and the future-deploy sentence fails. A possessive needs neither:
    # the matcher requires whitespace after the platform name, and `'` is not
    # whitespace. Widening either span (any slash, a punctuation tail, or a
    # passive shape, or a later `_MUTATIONS` verb) is caught by the parity
    # sweep, not by these fixtures. `will deploy` stays quiet: deploy is the
    # complement of the modal. `so drop the database` is later, and fires.
    "a topology legend": (
        "compose/Railway stack (web+api+db) · agent hosts/outposts "
        "(fleet + spawned vendor agents) · user machines (cli)"),
    "the symptom that quotes the legend": (
        "**Symptom.** The reach classifier's `platform-command` rule (\"names an "
        "infrastructure CLI with a subcommand\") fires on *descriptive* prose, not "
        "invoked commands. GRPH-1034 is declared `reach=repo` (it builds a read-only UI "
        "view) but was flagged on the quoted phrase `compose/Railway stack (web+api+db) "
        "· agent hosts/outposts (fleet + spawned vendor agents) · user machines (cli)` "
        "— which is a diagram legend, not a command to run."),
    "a view that renders a deploy topology": "the view renders the deploy topology",
    "a migration that adds a column": "a migration adds a column",
    "migrate, said as prose": "we migrate the rows before the page renders",
    # Determiner + descriptive noun. Not a global noun stop-list: `project` is
    # also a real subcommand (`vercel project rm`), and that one must still fire.
    "a future deploy, said as a sentence": (
        "The Railway project will deploy next week once the page renders"),
    # `be` plus an ordinary adjective is still a subject. `reset` is the one
    # unchanged participle the shape names; `ready` is not one.
    "a future state, said as a sentence": (
        "The Railway project will be ready next week"),
    # The same subject after sentence punctuation, with at most one
    # conjunction between the punctuation and the determiner. A verb in
    # that position is an imperative and is a parity row, not one of these.
    "a subject after a sentence": (
        "Ship the page. The Railway project will deploy next week"),
    "a subject after a conjunction": (
        "Done. But the Railway project will deploy next week"),
    "a subject that opens on a conjunction": (
        "And the Railway project will deploy next week"),
    "a possessive, not an invocation": "Railway's dashboard shows the stack",
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


def test_a_platform_name_used_as_english_is_not_a_command():
    """GRPH-989, stated as the discriminator rather than as three examples. An infrastructure
    subcommand is a verb or a noun — `deploy`, `up`, `apply`, `variables`. It is never `as`,
    `the` or `it`. A function word after the platform name means the sentence is using the word
    as English, which is the whole difference between "render as" and "render deploy"."""
    assert not reach_svc.signals("must not render as off")
    assert not reach_svc.signals("we render the page")
    assert not reach_svc.signals("it will render it twice")

    assert reach_svc.signals("render deploy"), "a real subcommand must still fire"


def test_the_guard_is_not_weakened_into_uselessness():
    """The other half, and the reason the fix is a stop-list of GRAMMAR rather than a longer
    exception list: every command shape that mattered before must still refuse. Sabotage: add
    `deploy` or `up` to `_NOT_SUBCOMMANDS` and these fail."""
    for command in ("render deploy", "fly launch", "fly deploy", "railway up",
                    "kubectl apply -f k8s/api.yaml", "vercel promote", "wrangler publish",
                    "Run `railway up` to ship it"):
        assert reach_svc.signals(command), command


def test_reading_subcommands_are_still_quiet():
    """The original half of the stop-list, pinned so extending it cannot drop it: `vercel logs`
    and `kubectl get` are how a worker investigates."""
    for reading in ("vercel logs", "kubectl get pods", "railway status", "fly version"):
        assert not reach_svc.signals(reading), reading


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


# ---- 3b. base parity: a later narrowing has to go red (GRPH-1037) ------------------------
#
# The oracle is the grammar-blind matcher on origin/main at 386b62de, the base this
# item started from. Head fires platform-command on every command that oracle fires
# on, except EXEMPT. One command per test, so dropping a single verb is a red row
# rather than a quieter suite. A sweep entry that the oracle does not fire on fails
# too: a command that was never a base hit cannot count as parity.

_BASE_PLATFORMS = (
    "railway|vercel|fly|flyctl|heroku|aws|gcloud|az|doctl|kubectl|helm|terraform|tofu"
    "|netlify|render|supabase|cloudflare|wrangler"
)
_BASE_NOT_SUBCOMMANDS = (
    "logs|get|status|version|--help"
    "|a|an|the|as|at|by|for|from|in|into|of|off|on|onto|out|over|to|up-to|with|within"
    "|and|or|but|if|so|than|then|that|this|these|those|which|while|when|whether"
    "|is|are|was|were|be|been|being|it|its|them|their|there|they"
    "|not|no|never|only|also|just|still|already|rather|instead"
)
_BASE_PLATFORM_ORACLE = re.compile(
    rf"\b(?:{_BASE_PLATFORMS})\s+(?!(?:{_BASE_NOT_SUBCOMMANDS})\b)[a-z][\w-]+",
    re.I,
)

#: Commands the base matcher fires on and head may stay quiet on. One line each,
#: and the reason is the decision. `kubectl port-forward` is the only one.
EXEMPT = {
    "kubectl port-forward": "a tunnel, not a mutation of the cluster",
    "kubectl port-forward svc/api 8080:80": "a tunnel, not a mutation of the cluster",
}

_SWEEP_PLATFORMS = (
    "railway", "vercel", "fly", "flyctl", "heroku", "aws", "gcloud", "az", "doctl",
    "kubectl", "helm", "terraform", "tofu", "netlify", "render", "supabase",
    "cloudflare", "wrangler",
)
# The second bounce's verbs, one per platform, bare and behind a noun subcommand.
_SWEEP_VERBS = (
    "remove", "detach", "disable", "stop", "suspend", "revert", "replace",
    "unpublish", "release",
)
# Real commands, including the shapes a widened noun exemption would swallow.
# `projects`, `project`, `stack` and `teams` are descriptive nouns ONLY after a
# determiner, and only when that determiner starts the clause and a modal
# follows. Drop the determiner from `_SUBJECT_SPAN` and the bare canaries go
# quiet: gcloud/supabase projects delete, vercel project rm, heroku stack:set,
# heroku teams:remove. Put the punctuation or end-of-line tail back and the
# imperative rows go quiet (`Delete the vercel project.`). Replace the passive
# shape with the seven-word participle list and the bounce-of-2f9a974b rows
# go quiet (`will be purged`, `should now be deleted`, `is removed`,
# `was deleted, so delete it again`). Drop `_destructive_later` and
# `The railway project will ship next week, so drop the database.` goes quiet.
# Match any slash as legend glue and `Run ./bin/railway projects (prod)` goes
# quiet. Put `(?<![/.])` back on the platform pattern and the path-prefixed
# lines go quiet.
_SWEEP_SPECIFIC = (
    "railway up",
    "railway up --service api",
    "railway down",
    "railway redeploy",
    "railway variables set X=1",
    "railway service remove",
    "railway volume delete",
    "Run railway up",
    "vercel deploy",
    "vercel remove my-app",
    "vercel rm my-app",
    "vercel promote",
    "vercel rollback",
    "vercel env rm NAME",
    "vercel project rm my-app",
    "Run vercel remove my-app on the prod team",
    "vercel remove my-app --yes",
    "fly deploy",
    "fly launch",
    "fly apps remove x",
    "fly postgres detach",
    "fly postgres destroy",
    "fly scale count 2",
    "fly machine destroy",
    "fly secrets set K=v",
    "flyctl deploy",
    "flyctl apps destroy x",
    "heroku pg:reset",
    "heroku config:set FOO=1",
    "heroku maintenance:on",
    "heroku ps:restart",
    "heroku apps:destroy",
    "heroku addons:destroy",
    "heroku restart",
    "heroku stack:set heroku-22",
    "heroku teams:remove user@example.com",
    "aws s3 rb s3://bucket --force",
    "aws s3 rm s3://bucket/key",
    "aws ec2 terminate-instances --instance-ids i-1",
    "aws iam delete-user --user-name x",
    "aws cloudformation delete-stack --stack-name s",
    "aws eks delete-cluster --name c",
    "gcloud projects delete my-proj",
    "gcloud compute instances delete vm",
    "gcloud run services delete svc",
    "gcloud container clusters delete c",
    "gcloud sql instances delete db",
    "az group delete --name g",
    "az network nsg rule delete --name r",
    "az vm delete --name v",
    "az webapp delete --name w",
    "doctl compute droplet delete 1",
    "doctl kubernetes cluster delete c",
    "doctl apps delete 1",
    "kubectl apply -f k8s/api.yaml",
    "kubectl delete pod x",
    "kubectl rollout restart deployment/api",
    "kubectl scale deployment/api --replicas=3",
    "kubectl drain node1",
    "kubectl cordon node1",
    "kubectl edit deployment/api",
    "kubectl autoscale deployment/api --min=1 --max=5",
    "kubectl create secret generic s",
    "kubectl replace -f k8s/api.yaml",
    "kubectl patch deployment/api -p {}",
    "kubectl rollout undo deployment/api",
    "helm install rel chart",
    "helm uninstall rel",
    "helm upgrade rel chart",
    "helm rollback rel 1",
    "helm delete rel",
    "terraform apply",
    "terraform destroy",
    "terraform force-unlock LOCKID",
    "terraform import addr id",
    "tofu apply",
    "tofu destroy",
    "netlify deploy --prod",
    "netlify env:set KEY 1",
    "netlify sites:delete",
    "netlify unlink",
    "render deploys create",
    "render services delete srv",
    "supabase db reset",
    "supabase db reset --linked",
    "supabase db dump --linked",
    "supabase link",
    "supabase migration repair 20240101000000 --status reverted",
    "supabase projects delete ref",
    "wrangler deploy",
    "wrangler publish",
    "wrangler delete",
    "wrangler kv:key delete",
    "cloudflare pages deploy",
    # Bounce of 2c852417. A slash or a dot is a legend only when what follows
    # the name is descriptive. A path-prefixed binary is still a command.
    "/usr/local/bin/kubectl delete pod web-1",
    "./bin/railway up",
    "~/bin/fly deploy",
    "infra/terraform destroy -auto-approve",
    # A determiner plus a noun is a subject only when it starts the clause
    # and a modal follows. `:` and the next word are the subcommand. A verb
    # before the determiner is an imperative, including when the noun is
    # followed by punctuation. A modal plus `be` plus a destructive
    # participle is the instruction.
    "Run the heroku stack:set heroku-22 command on prod",
    "Use the gcloud projects delete my-proj command",
    "Execute the supabase projects delete abc command",
    "Run the vercel project rm my-app command",
    "Run our heroku teams:remove x",
    "run that flyctl stack update",
    "Delete the vercel project.",
    "Wipe the supabase project.",
    "Drop the supabase projects!",
    "Remove the heroku teams.",
    "Tear down the kubectl projects, now",
    "THE VERCEL PROJECT WILL BE REMOVED",
    "Run ./bin/railway projects (prod)",
    # Adversarial pass over the same hole: a determiner, a platform, and a
    # destructive imperative or passive. Varied verb, case, punctuation, and
    # conjunction. Each one is a base hit; head has to fire on it too.
    "Destroy the aws account.",
    "Purge the gcloud project!",
    "Terminate the heroku apps, immediately",
    "Please erase the terraform stack.",
    "And then delete the fly apps.",
    "NOW REMOVE THE RAILWAY SERVICE.",
    "Kindly drop the supabase database;",
    "Go and wipe the vercel deployment.",
    "We should destroy the netlify site.",
    "Don't keep it: delete the render service.",
    "After the window, remove the cloudflare zone.",
    "Shut down the fly apps.",
    "Also, kill the wrangler namespace.",
    "Remove the heroku teams, and the vercel project.",
    "Please, Delete the doctl droplet.",
    "(delete the tofu state)",
    "Tear down the helm release!",
    "Once it is quiet. Destroy the az group.",
    "Yes! Remove the netlify site.",
    "Now delete the vercel project",
    "Still, remove the fly apps",
    "So drop the supabase projects",
    "But wipe the render account.",
    "~/bin/fly projects (prod)",
    "/usr/local/bin/kubectl projects (ns)",
    "The supabase project will be wiped tonight",
    "The railway stack will be destroyed.",
    "The gcloud project will be dropped",
    "The vercel project will be torn down",
    "The heroku teams will be reset",
    "The aws account will be deleted",
    "Ship it. And the netlify account will be wiped",
    "But the render account will be deleted",
    # Bounce of 2f9a974b. A participle list misses every passive it does not
    # name. The shape covers an unseen participle, an adverb before `be`,
    # `to be`, a get-passive, a copula, and a destructive verb later on.
    "The supabase project will be purged.",
    "The railway project will be decommissioned.",
    "The supabase project must be truncated.",
    "The vercel project will be nuked.",
    "The railway project will be shut down.",
    "The vercel project should now be deleted.",
    "The heroku project will soon be deleted.",
    "The heroku teams has to be deleted.",
    "The vercel project is to be removed.",
    "The supabase project is going to be deleted.",
    "The supabase project will get wiped tonight.",
    "This vercel project is removed on Friday.",
    "The heroku project was deleted, so delete it again.",
    # No passive shape. Only the later `_MUTATIONS` word (`drop`) keeps it.
    "The railway project will ship next week, so drop the database.",
    # Adversarial pass: more passives, modals, get-passives, and compounds.
    # Varied adverb, case, punctuation, and conjunction. Each is a base hit.
    "The aws account will be completely purged.",
    "The gcloud project must soon be truncated!",
    "THE FLY PROJECTS WILL BE NUKED",
    "The netlify account will be shut off.",
    "The render stack will get destroyed tonight.",
    "The terraform stack is to be wiped.",
    "Our cloudflare account is going to be purged.",
    "The doctl project will be decommissioned.",
    "The wrangler account should later be nuked.",
    "The az account was purged, so purge it again.",
    "The tofu stack will be taken down.",
    "The helm stack has to be shut down.",
    "The supabase project will be fully decommissioned.",
    "A railway project will get wiped.",
    "The vercel project is deleted.",
    "The heroku teams were removed yesterday.",
    "The gcloud project might be truncated at dawn.",
    "The fly projects could be nuked!",
    "The netlify account shall be decommissioned.",
    "That render account will be shut out.",
    "The kubectl projects will be torn down.",
    "The railway project will be very quickly purged.",
    "The railway project will be completely shut down.",
    "But the vercel project will be purged.",
    "Done. The railway project will be nuked.",
    "The supabase project is being decommissioned.",
    "The aws account will deploy next week, so wipe the volume",
)


def _sweep() -> list[str]:
    commands = list(_SWEEP_SPECIFIC)
    for platform in _SWEEP_PLATFORMS:
        for verb in _SWEEP_VERBS:
            commands.append(f"{platform} {verb}")
            commands.append(f"{platform} apps {verb} x")
    seen: set[str] = set()
    unique: list[str] = []
    for command in commands:
        if command in seen or command in EXEMPT:
            continue
        seen.add(command)
        unique.append(command)
    return unique


SWEEP = _sweep()


def _base_platform_fires(text: str) -> bool:
    return any(
        _BASE_PLATFORM_ORACLE.search(line) for line in text.splitlines() if line.strip()
    )


def _platform_command(text: str) -> bool:
    return any(s["rule"] == "platform-command" for s in reach_svc.signals(text))


@pytest.mark.parametrize("command", SWEEP)
def test_head_fires_wherever_the_base_matcher_fires(command):
    """Fail closed. A command the base matcher flags stays flagged, unless it is
    named in EXEMPT. Widening a descriptive exemption until it swallows one of
    these is a red row."""
    assert _base_platform_fires(command), (
        f"sweep entry is not a base hit, so a quiet head would pass: {command}"
    )
    assert _platform_command(command), command


@pytest.mark.parametrize("command", sorted(EXEMPT))
def test_exempt_commands_stay_quiet_and_say_why(command):
    """The exemption is a decision with a reason, not a hole. The base matcher
    fires here; head does not. Delete `port-forward` from the exempt token and
    this fails."""
    assert EXEMPT[command].strip(), command
    assert _base_platform_fires(command), command
    assert not reach_svc.signals(command), command


# ---- 4. the call: delegate consults the same reading the unit tests do (GRPH-1037) ------

_DESCRIPTIVE = (
    "a topology legend",
    "the symptom that quotes the legend",
    "a view that renders a deploy topology",
    "a migration that adds a column",
    "migrate, said as prose",
    "a future deploy, said as a sentence",
    "a future state, said as a sentence",
    "a possessive, not an invocation",
)
_COMMANDS = (
    "railway up with a flag",
    "docker compose down",
    "a kubectl rollout restart",
    "railway up, backticked",
    "docker compose down, backticked",
    "a kubectl rollout, backticked",
    "railway up, fenced",
    "docker compose down, fenced",
    "a kubectl rollout, fenced",
    # The four the first bounce named, and the five the second bounce named.
    # Delegate has to refuse them, not only signals().
    "supabase db reset",
    "supabase db reset --linked",
    "heroku pg:reset",
    "heroku config:set",
    "vercel remove",
    "vercel remove, in a sentence",
    "fly postgres detach",
    "fly apps remove",
    "railway service remove",
    "delete the vercel project",
    "wipe the supabase project",
    "drop the supabase projects",
    "remove the heroku teams",
    "tear down the kubectl projects",
    "the vercel project will be removed",
    "path-prefixed railway projects",
    "will be purged",
    "will be decommissioned",
    "must be truncated",
    "will be nuked",
    "will be shut down",
    "adverb before be",
    "adverb before be, soon",
    "has to be deleted",
    "is to be removed",
    "is going to be deleted",
    "will get wiped",
    "copula plus participle",
    "imperative later in the clause",
    "destructive verb later in the clause",
)


def test_descriptive_prose_delegates_without_an_acknowledgement(
        client, auth, key, db, proj, planner):
    """THE CALL. `signals()` can be right while `delegate` never asks it, and a suite that
    only calls the function stays green — the absence then reads as "this prose is fine to
    hand to a shell". Sabotage: delete the legend span or the subject scrub and
    the legend or the sentence is refused."""
    for name in _DESCRIPTIVE:
        text = MUST_NOT_FIRE[name]
        assert not reach_svc.signals(text), name
        item = _item(client, key, name, description=text)
        _ok(_mcp(client, key, "delegate", {
            "id": item, "lane": "backend", "tier": "cheap",
            "agent_id": planner, "seat": True}))


def test_command_shaped_prose_is_refused_without_an_acknowledgement(
        client, auth, key, db, proj, planner):
    """The other half of the call. A command in backticks or a fence is still a command —
    that is where the incident's own checklist was written. Sabotage: drop the
    platform-command rule and these delegate."""
    for name in _COMMANDS:
        text = MUST_FIRE[name]
        found = reach_svc.signals(text)
        assert found and any(s["rule"] == "platform-command" for s in found), name
        item = _item(client, key, name, description=text)
        e = _err(_mcp(client, key, "delegate", {
            "id": item, "lane": "backend", "tier": "cheap",
            "agent_id": planner, "seat": True}))
        assert "acknowledge_reach=true" in e["message"], name
        assert "platform-command" in e["message"], name
