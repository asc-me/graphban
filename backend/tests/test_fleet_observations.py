"""GRPH-975 — a supervisor credential records the base without `update_item`.

The unit tests that post the cut-from note themselves, with a wave-grade key, stay in
`test_fleet_base_commit_gate`. They pass while every real wave logs `base not recorded`,
because the supervisor client refuses `update_item`. The test at the bottom drives the
real `gbfleet` client — its real `ALLOWED_TOOLS` and `ALLOWED_PATHS` — against this app.

Sabotage (a): put the receipt back on `client.call("update_item", ...)` inside
`_record_cut_from`. The real client raises `NotPermitted`, the note is absent, and
`test_the_real_supervisor_client_records_the_base_and_sign_off_compares` fails.

Sabotage (b): `return True` at the top of `fleet._wave_cut_item`. The route then accepts
an item the wave did not cut, and `test_a_wave_that_did_not_cut_the_item_is_refused` fails.
Dropping only the project comparison fails
`test_the_same_wave_label_in_another_project_is_not_this_wave` and leaves (b)'s test green.
"""
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "fleet" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gbfleet.client import ALLOWED_TOOLS, Graphban  # noqa: E402
from gbfleet.supervisor import Wave, _record_cut_from  # noqa: E402

from app.models import Item  # noqa: E402
from app.services import fleet as fleet_svc  # noqa: E402
from app.services.items import CUT_FROM_MARKER  # noqa: E402

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
    """The route is a supervisor wave credential. Widening it to every key that can write
    would put the receipt on the same door `update_item` already is."""
    proj, hand, _planner = _project(client, auth)
    worker_key = client.post(
        "/api/fleet/keys", json={"project_id": proj, "role": "worker", "wave": "wave-1"},
        headers=auth).json()["plaintext"]
    body = {"item_id": "GRPH-1", "kind": "cut_from", "branch": "gb/x", "base": BASE}
    assert _post(client, worker_key, **body).status_code == 403
    assert _post(client, hand, **body).status_code == 403


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

    `pushed_head` and `lease` are named so they 422 instead of being stored or dropped.
    """
    proj, hand, planner = _project(client, auth)
    _worker, item = _item(client, hand, title="compose")
    _bind(db, proj, item, wave="wave-1")

    later = _post(client, planner, item_id=item, kind="pushed_head", branch="gb/obs")
    assert later.status_code == 422, later.text
    assert "pushed_head" in later.text and "lease" in later.text
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
