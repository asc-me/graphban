"""GRPH-975 — a supervisor credential records the base without `update_item`.

The unit tests that post the cut-from note themselves, with a wave-grade key, stay in
`test_fleet_base_commit_gate`. They pass while every real wave logs `base not recorded`,
because the supervisor client refuses `update_item`. The test at the bottom drives the
real `gbfleet` client — its real `ALLOWED_TOOLS` and `ALLOWED_PATHS` — against this app.

GRPH-1121: the wave-grade key is not the key a wave runs on. `gban setup` mints a
project-scoped key with no `fleet_wave` and no stored roles (the ceiling resolves to
planner and worker). Requiring `fleet_wave` refused that caller, so the base and the
lease keep-alive never landed. The credential gate now admits that shape. `_wave_cut_item`
still decides the item: a setup key matches only because the agent that minted the seat
authenticated with this key, and only inside the key's project.

Sabotage (a): put the receipt back on `client.call("update_item", ...)` inside
`_record_cut_from`. The real client raises `NotPermitted`, the note is absent, and
`test_the_real_supervisor_client_records_the_base_and_sign_off_compares` fails.

Sabotage (b): `return True` at the top of `fleet._wave_cut_item`. The route then accepts
an item the wave did not cut, and `test_a_wave_that_did_not_cut_the_item_is_refused` fails.
Dropping only the project comparison fails
`test_the_same_wave_label_in_another_project_is_not_this_wave` and
`test_a_setup_key_from_another_project_cannot_plant_a_base`, and leaves (b)'s test green.

Sabotage (c): restore the `fleet_wave`-required gate (`return False` when the tag is
empty) and `test_a_setup_key_records_the_base_and_the_lease_and_sign_off_compares` fails.
Drop the minted-by requirement (any seat in the project counts) and
`test_a_setup_key_that_did_not_mint_the_seat_is_refused` fails.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "fleet" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gbfleet.client import ALLOWED_TOOLS, Graphban  # noqa: E402
from gbfleet.heartbeat import keep  # noqa: E402
from gbfleet.supervisor import Wave, _record_cut_from  # noqa: E402

from app.models import Agent, ApiKey, Item  # noqa: E402
from app.services import fleet as fleet_svc  # noqa: E402
from app.services.items import (  # noqa: E402
    COMMITTED_MARKER, CUT_FROM_MARKER, DEFAULT_LEASE_SECONDS, _aware, claimable,
)

BASE = "7ea90ebebe2b88d263dcae8ca77f84b69cb00300"
HEAD = "aaf6132e1d4c77b2c9a1e5f0b3d8a7c6e5f4d3c2"


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


class _Bridge(httpx.BaseTransport):
    """Forward the supervisor's httpx client onto the TestClient that owns the app.

    A second ASGI transport would skip the lifespan that built this database. The
    request is already outside a route, so calling back into the test client is the
    app the rest of the file is talking to.
    """

    def __init__(self, app, paths: list[str]):
        self._app = app
        self._paths = paths

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._paths.append(request.url.path)
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in {"host", "content-length"}
        }
        sent = self._app.request(
            request.method, request.url.path, content=request.content, headers=headers,
        )
        return httpx.Response(
            sent.status_code, content=sent.content,
            headers={"content-type": sent.headers.get("content-type", "application/json")},
        )


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _project(client, auth):
    proj = client.post("/api/projects", json={"name": "Observations"}, headers=auth).json()["id"]
    hand = client.post("/api/api-keys", json={"name": "hand", "project_id": proj},
                       headers=auth).json()["plaintext"]
    minted = client.post("/api/fleet/keys",
                         json={"project_id": proj, "role": "planner", "wave": "wave-1"},
                         headers=auth)
    assert minted.status_code == 201, minted.text
    return proj, hand, minted.json()["plaintext"]


def _setup_key(client, auth, proj) -> tuple[str, str]:
    """The POST `gban setup` actually makes (`cli/src/gban/setup.py` `mint`).

    Stored `roles` stay empty. `eligible_roles` resolves that to every role, which is
    planner and worker — the ceiling the item names. Writing `["planner", "worker"]`
    onto the row, or borrowing the wave-grade planner key, would leave this test green
    while the credential a wave runs on still got 403. That is the miss GRPH-1121 is.
    """
    minted = client.post(
        "/api/api-keys",
        json={"name": f"{proj} delegation", "project_id": proj,
              "expires_in_days": None, "tool_tiers": ["fleet", "prd"]},
        headers=auth,
    )
    assert minted.status_code == 201, minted.text
    body = minted.json()
    assert body["project_id"] == proj
    assert body["fleet_wave"] is None
    assert body["expires_at"] is None
    assert body["tool_tiers"] == ["fleet", "prd"]
    return body["plaintext"], body["id"]


def _assert_setup_shape(db, key_id: str) -> None:
    row = db.get(ApiKey, key_id)
    assert row is not None
    assert row.roles in (None, [])
    assert not (row.fleet_wave or "").strip()
    assert row.project_id
    ceiling = fleet_svc.eligible_roles(row)
    assert "planner" in ceiling and "worker" in ceiling


def _item(client, hand, *, title="a slice"):
    worker = _ok(client, hand, "register_agent",
                 {"label": title, "branch": "gb/obs", "capabilities": {"instance": title}})
    made = _ok(client, hand, "create_item",
               {"title": title, "status": "next", "effort": 1})
    return worker, made["id"]


def _bind(db, proj, item, *, wave, minted_by=None):
    seat, _code = fleet_svc.issue_enrolment(
        db, project_id=proj, role="worker", wave=wave, minted_by=minted_by, item_id=item,
    )
    return seat


def _post(client, key, **body):
    return client.post("/api/fleet/observations", json=body, headers={"X-API-Key": key})


def _notes(client, hand, item):
    got = _ok(client, hand, "get_item_details", {"id": item})
    return [e for e in got["evidence"] if e.get("kind") == "note"]


# ---- who may post, and about which item -------------------------------------------------------


def test_a_worker_fleet_key_and_a_hand_minted_key_are_refused(client, auth, db):
    """A worker-only key and an unscoped key are 403 at the credential gate.

    A project-scoped hand key is the shape `gban setup` mints, so the credential gate
    admits it (GRPH-1121). Posting about an item it did not cut is 404, the same answer
    as a missing item. Widening the credential gate must not become a write: there is
    no seat this key minted, and the note is absent.
    """
    proj, hand, _planner = _project(client, auth)
    worker_key = client.post(
        "/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "wave-1"},
        headers=auth).json()["plaintext"]
    unscoped = client.post(
        "/api/api-keys",
        json={"name": "global", "tool_tiers": ["fleet", "prd"]},
        headers=auth).json()
    assert unscoped["project_id"] is None
    assert unscoped["fleet_wave"] is None
    body = {"item_id": "GRPH-1", "kind": "cut_from", "branch": "gb/x", "base": BASE}
    assert _post(client, worker_key, **body).status_code == 403
    assert _post(client, unscoped["plaintext"], **body).status_code == 403
    # The hand key passes the credential gate and then fails the item gate.
    missed = _post(client, hand, **body)
    assert missed.status_code == 404, missed.text
    assert "no such item" in missed.text


def test_a_wave_that_did_not_cut_the_item_is_refused(client, auth, db):
    """404, the same answer as a missing item. A 403 would confirm the id exists.

    Sabotage (b): `return True` at the top of `_wave_cut_item` and this is 200 with a note.
    """
    _proj, hand, planner = _project(client, auth)
    _worker, item = _item(client, hand)
    refused = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert refused.status_code == 404, refused.text
    assert _notes(client, hand, item) == []
    missing = _post(client, planner, item_id="NO-SUCH", kind="cut_from",
                    branch="gb/obs", base=BASE)
    assert missing.status_code == 404


def test_the_same_wave_label_in_another_project_is_not_this_wave(client, auth, db):
    """`next_wave` restarts at `wave-1` in every project. `can_write` is the user's
    membership, so one operator owning both projects is not a scope check.

    Project A's planner key (`fleet_wave` `wave-1`) posting `cut_from` about an item
    in project B that has a `wave-1` seat must be 404. Drop the project comparison in
    `_wave_cut_item` and this is 200 with a note, which plants the base
    `commit_is_not_the_base` trusts on an item this wave did not cut.
    """
    _proj_a, _hand_a, planner_a = _project(client, auth)
    proj_b = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    hand_b = client.post("/api/api-keys", json={"name": "hand-b", "project_id": proj_b},
                         headers=auth).json()["plaintext"]
    _worker, item = _item(client, hand_b, title="foreign")
    _bind(db, proj_b, item, wave="wave-1")
    refused = _post(client, planner_a, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert refused.status_code == 404, refused.text
    assert _notes(client, hand_b, item) == []


def test_a_seat_from_another_key_or_another_wave_does_not_count(client, auth, db):
    """Matching only `item_id` would let any fleet key write any bound item."""
    proj, hand, planner = _project(client, auth)
    _worker, item = _item(client, hand)
    other = _ok(client, hand, "register_agent", {"label": "other", "branch": "gb/obs"})
    _bind(db, proj, item, wave="not-this-wave", minted_by=other["agent_id"])
    refused = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert refused.status_code == 404, refused.text
    assert _notes(client, hand, item) == []


def test_a_matching_wave_records_without_a_minter_and_a_revoked_seat_still_counts(
        client, auth, db):
    """A human-issued seat has no `minted_by`. Its wave string is the match. Revoking the
    seat afterwards must not make a late receipt 404 — the base is why the route exists."""
    proj, hand, planner = _project(client, auth)
    _worker, item = _item(client, hand, title="human-seat")
    seat = _bind(db, proj, item, wave="wave-1")
    seat.revoked = True
    db.commit()
    got = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert got.status_code == 200, got.text
    assert got.json()["recorded"] is True
    assert any(CUT_FROM_MARKER + BASE in n["detail"] for n in _notes(client, hand, item))


def test_a_sibling_is_covered_by_who_consumed_the_seat(client, auth, db):
    """The seat is bound to one item. The child then claims its cluster-mates, and the PR
    receipt names those. `consumed_by` is the claimer; the wave string still need not
    equal `fleet_wave`."""
    proj, hand, planner = _project(client, auth)
    worker, bound = _item(client, hand, title="bound")
    sibling = _ok(client, hand, "create_item",
                  {"title": "sibling", "status": "next", "effort": 1})["id"]
    supervisor = _ok(client, planner, "register_agent", {"label": "sup", "branch": "gb/obs"})
    seat = _bind(db, proj, bound, wave="wave", minted_by=supervisor["agent_id"])
    seat.consumed_by = worker["agent_id"]
    db.commit()
    row = db.get(Item, sibling)
    assert row is not None
    row.claimed_by = worker["agent_id"]
    db.commit()
    got = _post(client, planner, item_id=sibling, kind="withheld", branch="gb/sib",
                reason="changed file(s) outside every declared touchpoint: web/src/App.tsx")
    assert got.status_code == 200, got.text
    detail = _notes(client, hand, sibling)[0]["detail"]
    assert detail == ("pushed `gb/sib`; not proposed as a PR — changed file(s) outside "
                      "every declared touchpoint: web/src/App.tsx")


def test_the_server_composes_each_receipt_and_refuses_a_kind_it_does_not_record_yet(
        client, auth, db):
    """The caller sends facts. The bytes on the item are the ones the readers already know.

    `pushed_head` is named so it 422s instead of being stored or dropped. `lease` is
    accepted now (GRPH-1116) and is tested below — a refusal that still names it would
    mean the keep-alive never landed.
    """
    proj, hand, planner = _project(client, auth)
    _worker, item = _item(client, hand, title="compose")
    _bind(db, proj, item, wave="wave-1")

    later = _post(client, planner, item_id=item, kind="pushed_head", branch="gb/obs")
    assert later.status_code == 422, later.text
    assert "pushed_head" in later.text
    assert "lease" not in later.text
    assert "not accepted yet" in later.text
    assert _notes(client, hand, item) == []

    bad = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base="abc")
    assert bad.status_code == 422, bad.text

    cut = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert cut.status_code == 200, cut.text
    salvage = _post(client, planner, item_id=item, kind="salvage", branch="gb/obs",
                    commit=HEAD, reason="original ref holds the landed fix")
    assert salvage.status_code == 200, salvage.text
    opened = _post(client, planner, item_id=item, kind="pr_opened", branch="gb/obs",
                   url="https://github.com/o/r/pull/42")
    assert opened.status_code == 200, opened.text
    bad_url = _post(client, planner, item_id=item, kind="pr_opened", branch="gb/obs",
                    url="github.com/o/r/pull/42")
    assert bad_url.status_code == 422, bad_url.text

    got = _ok(client, hand, "get_item_details", {"id": item})
    details = [e.get("detail") for e in got["evidence"]]
    assert (f"{CUT_FROM_MARKER}{BASE} (`gb/obs`) — the base, which carries none of "
            "this item's work") in details
    assert (f"salvaged onto `gb/obs` `{HEAD[:12]}` "
            "(original ref holds the landed fix); not proposed as a PR") in details
    urls = [e for e in got["evidence"] if e.get("kind") == "url"]
    assert urls == [{"kind": "url", "detail": "draft PR opened by gbfleet for gb/obs",
                     "url": "https://github.com/o/r/pull/42"}]
    db.expire_all()
    assert db.get(Item, item).pr_linked_at is not None


# ---- the supervisor half, through the real client ---------------------------------------------


def test_the_real_supervisor_client_records_the_base_and_sign_off_compares(client, auth, db):
    """THE criterion. `ALLOWED_TOOLS` is the supervisor's set. The post is the observations
    route. `sign_off` then reports `commit_is_not_the_base` with `compared: true`.

    The seat's wave is `wave` and the key's `fleet_wave` is `wave-1` — the production
    pair. The seat was minted by an agent on this key, which is the match that holds
    when those two strings differ. The child has not consumed the seat yet: cut-from
    runs before the child starts, so the bound `item_id` is what counts.

    Sabotage (a): send this through `update_item` again. The real client raises
    `NotPermitted` before any HTTP, `wave.failures` names it, and compared stays false.
    """
    proj, hand, planner = _project(client, auth)
    worker, item = _item(client, hand, title="real-client")
    claimed = _ok(client, hand, "claim_next", {"agent_id": worker["agent_id"]})
    assert claimed["item"]["id"] == item
    supervisor = _ok(client, planner, "register_agent", {"label": "sup", "branch": "gb/obs"})
    _bind(db, proj, item, wave="wave", minted_by=supervisor["agent_id"])

    paths: list[str] = []
    gb = Graphban(base_url="http://testserver", api_key=planner, transport=_Bridge(client, paths))
    assert gb.allowed == ALLOWED_TOOLS
    assert "update_item" not in gb.allowed
    wave = Wave()
    _record_cut_from(wave, gb, item, "gb/obs", BASE)

    assert wave.failures == [], wave.failures
    assert paths == ["/api/fleet/observations"]
    notes = _notes(client, hand, item)
    assert notes[0]["detail"] == (
        f"{CUT_FROM_MARKER}{BASE} (`gb/obs`) — the base, which carries none of this item's work"
    )
    assert all(e.get("kind") != "attestation" for e in notes)

    _ok(client, hand, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, hand, "register_agent",
                   {"label": "reviewer", "branch": "gb/obs",
                    "capabilities": {"instance": "reviewer"}})
    out = _ok(client, hand, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})
    assert out["status"] == "done"
    got = _ok(client, hand, "get_item_details", {"id": item})
    signed = [e for e in got["evidence"]
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    pred = {p["name"]: p for p in signed[-1]["predicates"]}["commit_is_not_the_base"]
    assert pred["compared"] is True
    assert pred["passed"] is True


# ---- lease keep-alive (GRPH-1116) -------------------------------------------------------------


def _row(db, item) -> Item:
    db.expire_all()
    return db.get(Item, item)


def _hold(client, auth, db, *, title="lease"):
    """A bound item this wave cut, claimed by the worker the seat is for."""
    proj, hand, planner = _project(client, auth)
    worker, item = _item(client, hand, title=title)
    _bind(db, proj, item, wave="wave-1")
    claimed = _ok(client, hand, "claim_next", {"agent_id": worker["agent_id"]})
    assert claimed["item"]["id"] == item
    return proj, hand, planner, worker, item


def _age(db, item, agent_id) -> None:
    """Push both clocks past the point where the roster releases the hold."""
    row = _row(db, item)
    agent = db.get(Agent, agent_id)
    assert agent is not None and row is not None
    past = datetime.now(timezone.utc) - timedelta(
        seconds=fleet_svc.presence_ttl_seconds() + 60)
    agent.last_seen_at = past
    row.claimed_at = datetime.now(timezone.utc) - timedelta(
        seconds=DEFAULT_LEASE_SECONDS + 60)
    db.commit()


def test_a_lease_is_refused_to_the_same_callers_as_every_other_observation(client, auth, db):
    """The keep-alive is not a second door. A worker key is 403. A project-scoped hand
    key — the setup shape — is admitted and then 404, because it cut nothing. A wave
    that did not cut the item, and another project's `wave-1`, are the same 404
    `cut_from` already gets. Kind `lease` has to take that path or the allowlist was
    widened by a synonym."""
    proj, hand, planner = _project(client, auth)
    worker_key = client.post(
        "/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "wave-1"},
        headers=auth).json()["plaintext"]
    body = {"item_id": "GRPH-1", "kind": "lease", "branch": "gb/obs", "agent_id": "GRPH-A1"}
    assert _post(client, worker_key, **body).status_code == 403
    missed = _post(client, hand, **body)
    assert missed.status_code == 404, missed.text

    _worker, item = _item(client, hand, title="uncut")
    missing = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                    agent_id=_worker["agent_id"])
    assert missing.status_code == 404, missing.text

    proj_b = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    hand_b = client.post("/api/api-keys", json={"name": "hand-b", "project_id": proj_b},
                         headers=auth).json()["plaintext"]
    foreign_worker, foreign = _item(client, hand_b, title="foreign-lease")
    _bind(db, proj_b, foreign, wave="wave-1")
    _ok(client, hand_b, "claim_next", {"agent_id": foreign_worker["agent_id"]})
    crossed = _post(client, planner, item_id=foreign, kind="lease", branch="gb/obs",
                    agent_id=foreign_worker["agent_id"])
    assert crossed.status_code == 404, crossed.text


def test_a_lease_names_the_agent_and_the_branch(client, auth, db):
    _proj, hand, planner, worker, item = _hold(client, auth, db, title="needs-who")
    no_agent = _post(client, planner, item_id=item, kind="lease", branch="gb/obs")
    assert no_agent.status_code == 422, no_agent.text
    assert "agent" in no_agent.text
    no_branch = _post(client, planner, item_id=item, kind="lease", branch="",
                      agent_id=worker["agent_id"])
    assert no_branch.status_code == 422, no_branch.text
    assert _notes(client, hand, item) == []


def test_a_live_process_keeps_both_clocks_and_stopping_the_posts_lets_the_lease_lapse(
        client, auth, db):
    """The suite is one command longer than the presence TTL. While the posts arrive,
    `requeue_offline_items` — what `fleet_status` runs before it returns the roster —
    does not release the holder, and `claimable` stays false. With no further post, both
    clocks age out and the release happens. That second half is the process having exited:
    the supervisor posts nothing once `poll()` is not None."""
    proj, _hand, planner, worker, item = _hold(client, auth, db, title="clocks")
    who = worker["agent_id"]
    _age(db, item, who)
    assert claimable(_row(db, item)) is True

    got = _post(client, planner, item_id=item, kind="lease", branch="gb/obs", agent_id=who)
    assert got.status_code == 200, got.text
    assert got.json() == {"item_id": item, "kind": "lease", "recorded": True, "extended": True}

    row = _row(db, item)
    assert row.claimed_by == who
    assert claimable(row) is False
    agent = db.get(Agent, who)
    assert fleet_svc.presence_state(agent) != "offline"
    assert fleet_svc.requeue_offline_items(db, project_id=proj) == []
    assert _row(db, item).claimed_by == who

    _age(db, item, who)
    assert claimable(_row(db, item)) is True
    released = fleet_svc.requeue_offline_items(db, project_id=proj)
    assert item in released
    lapsed = _row(db, item)
    assert lapsed.claimed_by is None
    assert lapsed.status == "next"


def test_a_lease_does_not_extend_a_hold_that_has_moved(client, auth, db):
    """`extended: false` is the answer, not a 404 and not a re-claim. Refreshing the
    new holder's clocks would keep a replaced agent alive."""
    _proj, _hand, planner, worker, item = _hold(client, auth, db, title="moved")
    row = _row(db, item)
    row.claimed_by = "GRPH-AOTHER"
    db.commit()
    before = _row(db, item).claimed_at

    got = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                agent_id=worker["agent_id"])
    assert got.status_code == 200, got.text
    assert got.json()["extended"] is False
    again = _row(db, item)
    assert again.claimed_by == "GRPH-AOTHER"
    assert again.claimed_at == before
    assert _notes(client, _hand, item) == []


def test_a_lease_does_not_extend_an_item_that_is_no_longer_in_progress(client, auth, db):
    _proj, hand, planner, worker, item = _hold(client, auth, db, title="handed")
    _ok(client, hand, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    before = _row(db, item).claimed_at
    got = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                agent_id=worker["agent_id"])
    assert got.status_code == 200, got.text
    assert got.json()["extended"] is False
    assert _row(db, item).claimed_at == before


def test_a_keep_alive_is_not_a_write_and_a_real_write_stays_one(client, auth, db):
    """GRPH-434, through this route. The beat must not make `updated_at` strictly after
    `claimed_at` — that is what a heartbeat-shaped UPDATE does if `onupdate` is left to
    stamp the column, and it would keep `built_by` for an agent that never touched the
    item. A touchpoints write from before the beat has to stay strictly after, or the
    beat erases the evidence that work happened."""
    _proj, hand, planner, worker, item = _hold(client, auth, db, title="clock-order")
    who = worker["agent_id"]
    bare = _post(client, planner, item_id=item, kind="lease", branch="gb/obs", agent_id=who)
    assert bare.json()["extended"] is True
    row = _row(db, item)
    assert _aware(row.updated_at) <= _aware(row.claimed_at)
    _ok(client, hand, "release_item", {"id": item, "agent_id": who})
    assert _row(db, item).built_by is None

    _ok(client, hand, "claim_next", {"agent_id": who})
    _ok(client, hand, "update_item",
        {"id": item, "touchpoints": ["backend/app/services/fleet.py"], "agent_id": who})
    wrote = _post(client, planner, item_id=item, kind="lease", branch="gb/obs", agent_id=who)
    assert wrote.json()["extended"] is True
    kept = _row(db, item)
    assert _aware(kept.updated_at) > _aware(kept.claimed_at)
    _ok(client, hand, "release_item", {"id": item, "agent_id": who})
    assert _row(db, item).built_by == who


def test_a_commit_on_the_branch_keeps_the_author_a_planner_cannot_inherit(client, auth, db):
    """The child committed and then went quiet. `updated_at` still says it wrote nothing —
    the note is the only signal — so deleting `committed_by` from `release_item` clears
    `built_by` and the planner's move to review becomes the author.

    Sabotage: drop `and not committed_by(...)` from the clear in `release_item`. This
    fails on `built_by == who` after the release.
    """
    _proj, hand, planner, worker, item = _hold(client, auth, db, title="committed")
    who = worker["agent_id"]
    first = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                  agent_id=who, commit=HEAD)
    assert first.status_code == 200, first.text
    again = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                  agent_id=who, commit=HEAD)
    assert again.json()["extended"] is True
    committed = [n for n in _notes(client, hand, item) if COMMITTED_MARKER in n["detail"]]
    assert [n["detail"] for n in committed] == [
        f"{COMMITTED_MARKER}{HEAD} on `gb/obs` by {who}",
    ]
    row = _row(db, item)
    assert _aware(row.updated_at) <= _aware(row.claimed_at), (
        "the note moved the authorship clock, so dropping committed_by would still pass")

    _ok(client, hand, "release_item", {"id": item, "agent_id": who})
    assert _row(db, item).built_by == who

    planner_agent = _ok(client, planner, "register_agent",
                        {"label": "planner", "branch": "gb/plan"})
    _ok(client, planner, "update_item",
        {"id": item, "status": "review", "agent_id": planner_agent["agent_id"]})
    finished = _row(db, item)
    assert finished.status == "review"
    assert finished.built_by == who


def test_the_cut_from_base_is_not_a_commit_and_a_keep_alive_adds_no_note(client, auth, db):
    """HEAD == the recorded base is the tree before this item. Counting it would keep
    `built_by` for a child that never wrote. A beat with no commit, repeated, adds nothing."""
    _proj, hand, planner, worker, item = _hold(client, auth, db, title="base-sha")
    who = worker["agent_id"]
    cut = _post(client, planner, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert cut.status_code == 200, cut.text
    # `cut_from` itself appends evidence, which moves `updated_at`. Pin the clock back
    # to the claim so this test is about the sha, not about that earlier write. A core
    # UPDATE names the column, so `onupdate` cannot stamp it a moment later.
    from sqlalchemy import update as sql_update
    pinned = _row(db, item)
    db.execute(sql_update(Item).where(Item.id == pinned.id).values(
        updated_at=pinned.claimed_at))
    db.commit()
    for _ in range(2):
        beat = _post(client, planner, item_id=item, kind="lease", branch="gb/obs",
                     agent_id=who, commit=BASE)
        assert beat.json()["extended"] is True
    details = [n["detail"] for n in _notes(client, hand, item)]
    assert not any(COMMITTED_MARKER in d for d in details)
    assert any(CUT_FROM_MARKER in d for d in details)
    _ok(client, hand, "release_item", {"id": item, "agent_id": who})
    assert _row(db, item).built_by is None


# ---- the setup key real waves run on (GRPH-1121) ----------------------------------------------


def test_a_worker_only_project_key_is_refused_without_a_wave(client, auth, db):
    """The new branch is `project_id` set and `fleet_wave` empty. Forgetting the planner
    ceiling on that branch would let a worker-only project key post. It is 403, the same
    answer as a worker fleet key."""
    proj, _hand, _planner = _project(client, auth)
    created = client.post(
        "/api/api-keys", json={"name": "worker-only", "project_id": proj}, headers=auth,
    ).json()
    row = db.get(ApiKey, created["id"])
    assert row is not None
    row.roles = ["worker"]
    db.commit()
    refused = _post(client, created["plaintext"], item_id="GRPH-1", kind="cut_from",
                    branch="gb/obs", base=BASE)
    assert refused.status_code == 403, refused.text


def test_a_setup_key_records_the_base_and_the_lease_and_sign_off_compares(client, auth, db):
    """THE criterion for GRPH-1121. The key is the one `gban setup` mints: project-scoped,
    no `fleet_wave`, stored roles empty so the ceiling is planner and worker. The seat's
    wave is `wave` and the key has no wave string, so the wave-string match cannot be what
    lets this through. The minter authenticated with this key.

    Both posts go through the real gbfleet client: `_record_cut_from` for the base, and
    `heartbeat.keep` for the lease — the two callers a wave actually uses. `sign_off` then
    reports `commit_is_not_the_base` with `compared: true`.

    Sabotage: `return False` when `fleet_wave` is empty, at the top of the empty-wave
    branch in `_supervisor_wave_key`. Both posts 403, `wave.failures` names them, and
    `compared` stays absent.
    """
    proj, hand, _planner = _project(client, auth)
    setup, key_id = _setup_key(client, auth, proj)
    _assert_setup_shape(db, key_id)
    worker, item = _item(client, hand, title="setup-key")
    claimed = _ok(client, hand, "claim_next", {"agent_id": worker["agent_id"]})
    assert claimed["item"]["id"] == item
    supervisor = _ok(client, setup, "register_agent", {"label": "sup", "branch": "gb/obs"})
    _bind(db, proj, item, wave="wave", minted_by=supervisor["agent_id"])
    who = worker["agent_id"]
    _age(db, item, who)
    assert claimable(_row(db, item)) is True

    paths: list[str] = []
    gb = Graphban(base_url="http://testserver", api_key=setup, transport=_Bridge(client, paths))
    assert "update_item" not in gb.allowed
    wave = Wave()
    _record_cut_from(wave, gb, item, "gb/obs", BASE)
    child = SimpleNamespace(
        running=True, agent_id=who, held_items=[item], branch="gb/obs",
        lease_kept_at=0.0, worktree=None,
    )
    keep(wave, [child], gb, now=1_000.0)

    assert wave.failures == [], wave.failures
    assert paths == ["/api/fleet/observations", "/api/fleet/observations"]
    notes = _notes(client, hand, item)
    assert notes[0]["detail"] == (
        f"{CUT_FROM_MARKER}{BASE} (`gb/obs`) — the base, which carries none of this item's work"
    )
    row = _row(db, item)
    assert row.claimed_by == who
    assert claimable(row) is False

    _ok(client, hand, "update_item",
        {"id": item, "status": "review", "agent_id": who})
    reviewer = _ok(client, hand, "register_agent",
                   {"label": "reviewer", "branch": "gb/obs",
                    "capabilities": {"instance": "setup-reviewer"}})
    out = _ok(client, hand, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})
    assert out["status"] == "done"
    got = _ok(client, hand, "get_item_details", {"id": item})
    signed = [e for e in got["evidence"]
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    pred = {p["name"]: p for p in signed[-1]["predicates"]}["commit_is_not_the_base"]
    assert pred["compared"] is True
    assert pred["passed"] is True


def test_a_setup_key_that_did_not_mint_the_seat_is_refused(client, auth, db):
    """A project-scoped planner key is not a write on every bound item. The seat is bound
    to the item and carries a wave string; the key's agent is not the minter, and the key
    has no `fleet_wave` for that string to match. Both kinds are 404.

    Sabotage: drop the minted-by requirement so any seat in the project counts
    (`return True` once `seats` is non-empty, or match `seat.minted_by` without comparing
    it to this key). This becomes 200 and a note lands.
    """
    proj, hand, _planner = _project(client, auth)
    setup, key_id = _setup_key(client, auth, proj)
    _assert_setup_shape(db, key_id)
    worker, item = _item(client, hand, title="not-my-seat")
    other = _ok(client, hand, "register_agent", {"label": "other", "branch": "gb/obs"})
    _bind(db, proj, item, wave="wave", minted_by=other["agent_id"])
    cut = _post(client, setup, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert cut.status_code == 404, cut.text
    lease = _post(client, setup, item_id=item, kind="lease", branch="gb/obs",
                  agent_id=worker["agent_id"])
    assert lease.status_code == 404, lease.text
    assert _notes(client, hand, item) == []


def test_a_setup_key_from_another_project_cannot_plant_a_base(client, auth, db):
    """The project check GRPH-975's bounce added, on the branch a setup key actually uses.

    Project A's setup key minted the seat, and the seat is bound to an item in project B.
    The minted-by branch would match. The project comparison has to refuse first, or a
    planner who can write both projects plants a base `commit_is_not_the_base` trusts.
    Narrowing that comparison to keys that carry `fleet_wave` leaves this test the one
    that goes red: A's key has no wave tag, so the wave-string cross-project test stays
    green.
    """
    proj_a, _hand_a, _planner_a = _project(client, auth)
    setup, key_id = _setup_key(client, auth, proj_a)
    _assert_setup_shape(db, key_id)
    supervisor = _ok(client, setup, "register_agent", {"label": "sup-a", "branch": "gb/obs"})
    proj_b = client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]
    hand_b = client.post("/api/api-keys", json={"name": "hand-b", "project_id": proj_b},
                         headers=auth).json()["plaintext"]
    _worker, item = _item(client, hand_b, title="foreign-setup")
    _bind(db, proj_b, item, wave="wave", minted_by=supervisor["agent_id"])
    refused = _post(client, setup, item_id=item, kind="cut_from", branch="gb/obs", base=BASE)
    assert refused.status_code == 404, refused.text
    assert _notes(client, hand_b, item) == []


# ---- a branch behind the trunk (GRPH-1236) ----------------------------------------------------


def _in_review(client, hand, planner, db):
    worker, item = _item(client, hand, title="behind")
    row = db.get(Item, item)
    row.claimed_by = worker["agent_id"]
    row.built_by = worker["agent_id"]
    row.status = "review"
    row.branch = "gb/behind"
    db.commit()
    return worker, item


def test_a_conflicting_branch_goes_back_to_next_without_a_review_verdict(client, auth, db):
    """`behind_conflict` sends a `review` item back to `next`, keeps its branch, and stores the
    resume brief where `choose_resume` reads it. The builder is not charged a bounce: no
    reviewer is written and the attempt is not closed as `bounced`.

    Sabotage: drop the `_send_back_behind` call in `record_observation`. The item stays in
    `review` and this fails.
    """
    from app.models import Delegation

    proj, hand, planner = _project(client, auth)
    worker, item = _in_review(client, hand, planner, db)
    _bind(db, proj, item, wave="wave-1")
    brief = ("Branch gb/behind conflicts with origin/main (a.txt). Next attempt: resume from "
             "gb/behind, `git merge origin/main` (no rebase, no force-push), resolve keeping "
             "both sides, rerun tests, hand to review.")
    got = _post(client, planner, item_id=item, kind="behind_conflict", branch="gb/behind",
                reason=brief)
    assert got.status_code == 200, got.text
    db.expire_all()
    row = db.get(Item, item)
    assert row.status == "next"
    assert row.claimed_by is None and row.review_claimed_by is None
    assert row.branch == "gb/behind"
    assert row.bounce_reason == brief
    assert row.reviewed_by is None
    assert row.built_by == worker["agent_id"]
    outcomes = [d.outcome for d in db.query(Delegation).filter(Delegation.item_id == item)]
    assert "bounced" not in outcomes
    assert any(n["detail"] == f"gbfleet: not proposed as a PR — {brief}"
               for n in _notes(client, hand, item))


def test_a_conflict_note_leaves_a_done_item_where_it_is(client, auth, db):
    """Reopening finished work is a person's call. The note still lands."""
    proj, hand, planner = _project(client, auth)
    _worker, item = _in_review(client, hand, planner, db)
    _bind(db, proj, item, wave="wave-1")
    row = db.get(Item, item)
    row.status = "done"
    db.commit()
    got = _post(client, planner, item_id=item, kind="behind_conflict", branch="gb/behind",
                reason="Branch gb/behind conflicts with origin/main (a.txt).")
    assert got.status_code == 200, got.text
    db.expire_all()
    assert db.get(Item, item).status == "done"


def test_a_trunk_merge_is_a_note_and_moves_nothing(client, auth, db):
    proj, hand, planner = _project(client, auth)
    _worker, item = _in_review(client, hand, planner, db)
    _bind(db, proj, item, wave="wave-1")
    why = ("merged origin/main@0123456789ab into the branch before proposing "
           "(1 behind; shared files: a.txt)")
    got = _post(client, planner, item_id=item, kind="merged_trunk", branch="gb/behind",
                reason=why)
    assert got.status_code == 200, got.text
    db.expire_all()
    assert db.get(Item, item).status == "review"
    assert any(n["detail"] == f"gbfleet: `gb/behind`: {why}" for n in _notes(client, hand, item))
    empty = _post(client, planner, item_id=item, kind="merged_trunk", branch="gb/behind")
    assert empty.status_code == 422, empty.text
