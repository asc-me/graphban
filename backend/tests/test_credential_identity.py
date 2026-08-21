"""A row says whether an AGENT or a CREDENTIAL stamped it (GRPH-437).

Every fleet tool resolves its caller the same way: the `agent_id` it was sent, or — in the
single-agent posture, where nobody registered — the API key. That fallback is deliberate and
predates PRD-17. What it lacked was a mark, and the absence cost two distinct things:

1. THE RECORD. `items.reviewed_by` held either an agent id or a key's name with nothing
   separating them. On 2026-08-21 four items were signed off by `wave-refetch-2`, the label on
   a key minted for an unrelated probe, which reads in the ledger and the UI exactly like an
   agent that reviewed them.

2. THE GATE. `_independent_of_author` answered `me is None or …` — an unregistered caller was
   independent by fiat. The route needed no privilege: an agent builds an item, its heartbeat
   lapses (the ordinary end of a session), the credential stops counting as "running a fleet",
   and the same process signs its own work off unidentified. `built_by` held an agent id and
   `reviewed_by` a key name, so the strings differed and
   `test_every_done_item_was_signed_by_someone_else` read it as reviewed by somebody else.

The second one was found while fixing the first, and only because the first probe of it was
WRONG: it set `agent.last_seen`, but the column is `last_seen_at`, so the fleet never actually
went quiet and the refusal that came back was for an unrelated reason. A probe that fails to
reach the state it is aiming at reports the same "safe" as a system that is safe.
"""
from datetime import timedelta

import pytest

from app.models import Agent, ApiKey, Item, utcnow
from app.services import fleet
from app.services import keys as keys_svc
from tests.test_fleet_review import (  # noqa: F401
    _built_by, _new_item, _ok, _refused, _register, _rpc, agent_key, db, proj)


def _item(db, item_key: str) -> Item:
    """The row behind a rendered key. There is no `Item.key` COLUMN — the key a human sees is
    project tag + number, rendered (PRD-13), so filtering on it silently matches nothing."""
    return db.get(Item, keys_svc.resolve_item(db, item_key))


class _Key:
    def __init__(self, id_="k1", name="fleet"):
        self.id, self.name = id_, name


# ---- the mark -------------------------------------------------------------------------------

def test_a_registered_agent_is_stamped_with_its_own_id():
    """The common case must be untouched — a prefix on an agent id would break every
    comparison in the fleet, starting with the self-review ban."""
    assert fleet.caller_identity("GRPH-A1", _Key()) == "GRPH-A1"
    assert not fleet.is_credential("GRPH-A1")


def test_an_unidentified_caller_is_stamped_as_a_credential():
    assert fleet.caller_identity(None, _Key(name="wave-refetch-2")) == "key:wave-refetch-2"
    assert fleet.is_credential("key:wave-refetch-2")


def test_a_nameless_key_still_produces_a_marked_identity():
    """A key with no label falls back to its id, and the mark still has to be there — an
    unmarked uuid is just as indistinguishable from an agent id as an unmarked name."""
    got = fleet.caller_identity(None, _Key(id_="ak_7f2", name=""))
    assert got == "key:ak_7f2" and fleet.is_credential(got)


def test_the_mark_cannot_collide_with_an_agent_id(client, agent_key):
    """The property the whole scheme rests on: agent ids are minted `<TAG>-A<n>`, so no agent
    can ever be mistaken for a credential or the reverse."""
    me = _register(client, agent_key, "worker")
    assert not fleet.is_credential(me["agent_id"])


# ---- both sides of the comparison move together ---------------------------------------------

def test_the_solo_posture_still_catches_its_own_work(client, agent_key, db):
    """THE regression this change could plausibly introduce, so it is asserted directly.

    The self-review ban is `item.built_by == agent_id`, and in the solo posture BOTH sides come
    from the credential fallback. Prefixing where work is claimed but not where it is signed
    off — or migrating one column and not another — makes them stop matching, and the ban then
    passes silently. That is a worse bug than the one being fixed.
    """
    _new_item(client, agent_key, "solo work")
    c = _ok(client, agent_key, "claim_next", {})
    item_key = c["item"]["id"]
    assert fleet.is_credential(_item(db, item_key).built_by), \
        "the claim did not stamp a marked credential — the two sides cannot be compared"

    _ok(client, agent_key, "update_item", {"id": item_key, "status": "review"})
    err = _refused(client, agent_key, "sign_off", {"id": item_key})

    assert err["code"] == "unauthorized"
    assert "cannot sign it off" in err["message"]


# ---- the gate -------------------------------------------------------------------------------

def test_a_quiet_fleets_credential_cannot_sign_off_its_own_agents_work(client, agent_key, db):
    """The bypass. An agent builds, its heartbeat lapses, and the bare credential signs the
    work off — `db.get(Agent, "key:fleet")` is None, so independence was never evaluated.

    Note the fixture writes `last_seen_at`. An earlier probe wrote `last_seen`, which is not
    the column `_has_specialised_agents` reads, so the fleet stayed live and the call was
    refused for being a fleet call — a refusal that looked like proof of safety and was not.
    """
    me = _register(client, agent_key, "worker")
    item_key = _built_by(client, agent_key, me)

    agent = db.get(Agent, me["agent_id"])
    agent.last_seen_at = utcnow() - timedelta(hours=6)
    db.commit()
    assert not fleet._has_specialised_agents(db, agent.api_key_id and _Key(agent.api_key_id)), \
        "the fleet is still live, so this test is not exercising the unidentified path"

    err = _refused(client, agent_key, "sign_off", {"id": item_key})

    assert err["code"] == "unauthorized"
    assert "not identified as an agent" in err["message"], err["message"]
    assert _item(db, item_key).status == "review"


def test_a_credential_may_still_review_another_credentials_agent(client, agent_key, db, proj):
    """The refusal is scoped to the credential the author ran on, not to unidentified callers
    generally. Widening it would break the solo posture the fallback exists to serve."""
    me = _register(client, agent_key, "worker")
    item_key = _built_by(client, agent_key, me)
    item = _item(db, item_key)

    # An author on some OTHER credential: same rows, different key.
    other = ApiKey(id="other-key", name="other", hashed_key="x", prefix="gb_sk_x", user_id=_a_user(db), project_id=item.project_id,
                   scopes=["read", "write"])
    db.add(other)
    db.flush()   # the UPDATE below emits SQL immediately; a pending key is not yet a row
    db.query(Agent).filter(Agent.id == me["agent_id"]).update({"api_key_id": "other-key"})
    db.commit()

    assert fleet._independent_of_author(
        db, item, "key:fleet", api_key=_Key(id_="k-mine")), \
        "an unidentified caller on a different credential is independent and must stay so"


def test_independence_is_asked_once_and_reused(client, agent_key, db):
    """`danger` and the second gate must read the SAME answer. Two calls are two chances to
    diverge, and this is the shape of gate where a refactor makes one of them weaker — which
    is what the function's own comment says about the first gate."""
    import inspect

    src = inspect.getsource(fleet.sign_off)
    assert src.count("_independent_of_author(") == 1, (
        "sign_off asks about independence more than once; the two answers can drift")


# ---- the response ---------------------------------------------------------------------------

def test_the_caller_is_told_the_verdict_was_recorded_against_a_credential(
        client, agent_key, db):
    """A caller that never registered has no way to notice its verdict is attributed to a key.
    Four items were signed off that way before anybody did."""
    a = _register(client, agent_key, "worker")
    item_key = _built_by(client, agent_key, a)
    # The author is on this credential, so an anonymous sign-off is refused — put the author
    # somewhere else so the SUCCESS path is what gets exercised.
    db.add(ApiKey(id="k-other", name="other", hashed_key="y", prefix="gb_sk_y",
                  user_id=_a_user(db), project_id=_item(db, item_key).project_id,
                  scopes=["read", "write"]))
    db.flush()
    db.query(Agent).filter(Agent.id == a["agent_id"]).update({"api_key_id": "k-other"})
    db.query(Agent).filter(Agent.id == a["agent_id"]).update(
        {"last_seen_at": utcnow() - timedelta(hours=6)})
    db.commit()

    out = _ok(client, agent_key, "sign_off", {"id": item_key})

    assert out["status"] == "done"
    assert fleet.is_credential(out["signed_by"]), out.get("signed_by")
    assert "not an agent" in out["attribution"]


# ---- the backfill ---------------------------------------------------------------------------

def test_the_migration_marks_only_what_it_can_identify(client, db):
    """0084 prefixes a bare key NAME and leaves an agent id alone. Both halves matter: agent
    ids are the common case, and a string matching neither is left as it is because nothing
    can say what it was.

    Run against the migration's own SQL rather than a reimplementation of it — a test that
    re-derives the rule cannot catch the rule being wrong.

    Takes `client` for the seed: this file's `db` fixture depends only on the database reset,
    so without it there is no project, no key and no user, and the fixture fails before the
    assertion is reached.
    """

    from app.models import Project
    pid = db.query(Project).first().id
    # Seeded here rather than borrowed from the dataset: the migration keys off a key's NAME,
    # and the seed's keys have none, so a borrowed row would exercise the branch that matches
    # nothing and the test would pass without touching the rule.
    key = ApiKey(id="mig-key", name="a-credential-name", hashed_key="z", prefix="gb_sk_z",
                 user_id=_a_user(db), project_id=pid, scopes=["read"])
    db.add(key)
    db.flush()
    agent = db.query(Agent).first()

    rows = [
        Item(id="m1", project_id=pid, number=9001, title="by a key",
             reviewed_by=key.name),
        Item(id="m2", project_id=pid, number=9002, title="by nobody known",
             reviewed_by="something-else-entirely"),
    ]
    if agent is not None:
        rows.append(Item(id="m3", project_id=pid, number=9003, title="by an agent",
                         reviewed_by=agent.id))
    db.add_all(rows)
    db.commit()

    db.execute(_migration_sql("reviewed_by"))
    db.commit()
    for r in rows:
        db.refresh(r)

    assert rows[0].reviewed_by == f"key:{key.name}"
    assert rows[1].reviewed_by == "something-else-entirely", "guessed at an unknown string"
    if agent is not None:
        assert rows[2].reviewed_by == agent.id, "relabelled a real agent as a credential"


def _a_user(db) -> str:
    from app.models import User
    return db.query(User).first().id


def _migration_sql(col: str):
    """The UPGRADE statement, lifted from the migration module itself."""
    import pathlib
    import re

    from sqlalchemy import text

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "alembic" / "versions" / "0084_mark_credential_identities.py")
    body = path.read_text(encoding="utf-8")
    stmt = re.search(r'op\.execute\(f"""(.+?)"""\)', body, re.S)
    assert stmt, "0084's upgrade statement moved — this test is reading nothing"
    return text(stmt.group(1).replace("{col}", col))


@pytest.mark.parametrize("col", fleet_columns := [
    "claimed_by", "built_by", "assignee", "reviewed_by", "review_claimed_by"])
def test_the_migration_covers_every_column_the_fallback_can_reach(col):
    """The columns `caller_identity` can land in must all be migrated, or an item claimed
    before the change and signed off after it compares unequal on one of them."""
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "alembic" / "versions" / "0084_mark_credential_identities.py")
    assert col in path.read_text(encoding="utf-8"), f"{col} is not backfilled"


def test_no_tool_resolves_its_caller_the_old_way():
    """The mark is only as good as its coverage. Eight tools stamped a caller when this was
    written and all eight go through `caller_identity`; a ninth that hand-rolls the fallback
    puts an unmarked key name back into a column somebody reads as an agent.

    Asserted against the SOURCE because that is what a future edit changes. A behavioural test
    would have to know which tools exist to be complete, and the tool that has not been written
    yet is exactly the one that would reintroduce this."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "mcp_server.py").read_text(encoding="utf-8")

    hand_rolled = re.findall(r'^\s*agent\w* = .*key\.name.*$', src, re.M)

    assert not hand_rolled, (
        "these resolve a caller without marking a credential — call "
        f"fleet_svc.caller_identity instead: {hand_rolled}")
