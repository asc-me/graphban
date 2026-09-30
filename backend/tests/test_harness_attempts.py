"""GRPH-993 — the model that ANSWERED, as distinct from the one that was asked for.

**Accept:** the supervisor's exit post carries a measured model, the attempt row stores it in
a column of its own, and the preference matrix attributes the cell to it while saying which
of the two names it used. A null measurement stays null.

**What went wrong.** GRPH-982 taught the supervisor to read qwen's own `-o json` result
record, so `init.model` — the EFFECTIVE model, not an echo of argv — was available at reap
and already spread into `POST /api/fleet/attempts` as `model`. The route had no such field,
so pydantic dropped it on the floor and the reading stopped at the wave summary. The attempt
row's `model` was already taken, by what the child DECLARED about itself, and the two are not
the same fact:

    qwen -m qwen3.8-max                 ->  init.model = "qwen3.8-max"
    qwen -m definitely-not-a-model-zzz  ->  init.model = "qwen3.7-plus"   (the silent default)

Across 1489 real children the measured name was `qwen3.7-plus` 1488 times and `qwen3.8-max`
once, while all 123 measured cells were filed under `alibaba` + model `""`. A preference
matrix that cannot separate two models of one vendor cannot say "this model is worth its tier
and that one is not", which is the point of PRD-37.

**The sabotage the item names:** store the requested model instead of the measured one, and
`test_the_measured_model_wins_when_it_differs_from_the_requested_one` goes red. Recording the
request as a measurement is the defect this exists to prevent — it puts a confident wrong
number in the matrix instead of a gap.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AttemptTelemetry, HarnessRollup
from app.services import delegation as dsvc
from app.services import harness as hsvc
from app.services import items as items_svc


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


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Attempts"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "attempts", "project_id": proj,
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


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent",
                    {"branch": "gb/test", "label": label, **kw}))["agent_id"]


def _post(client, key, body, expect=200):
    r = client.post("/api/fleet/attempts", json=body, headers={"X-API-Key": key})
    assert r.status_code == expect, r.text
    return r.json()


def _telemetry(db, delegation_id) -> AttemptTelemetry | None:
    db.expire_all()
    return db.scalar(select(AttemptTelemetry).where(
        AttemptTelemetry.delegation_id == delegation_id))


def _run(client, key, db, planner, *, declared: dict, title: str, measured: str | None,
         post_first: bool = False):
    """One signed-off attempt by a child declaring `declared`, measured as `measured`.

    `post_first` picks which of the two writers lands first — the supervisor's exit post or
    the server's own outcome derivation. There is no ordering requirement between them
    (PRD-38 D3), so a field that only survives one order is a field that is usually lost.
    """
    item = _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner}))
    did = d["delegation_id"]
    child = _agent(client, key, f"child-{title}", parent_agent_id=planner,
                   capabilities={"instance": title, **declared})
    assert items_svc.claim_item(db, item, child) is not None

    body = {"delegation_id": did, "binary_version": "0.23.0"}
    if measured is not None:
        body["model"] = measured
    if post_first:
        _post(client, key, body, expect=202)

    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"rev-{title}", capabilities={"instance": f"rev-{title}"})
    _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                       "evidence": [{"kind": "note", "detail": "read it"}]}))
    if not post_first:
        _post(client, key, body)
    return item, did


def _cells(db, proj) -> list[tuple]:
    return [(c["model"], c.get("model_source")) for c in dsvc.measured(db, proj)]


# ---- 1: a measured model is stored, and its cell names it -----------------------------------

def test_a_measured_model_is_stored_and_its_matrix_cell_names_it(client, key, db, proj):
    """Acceptance 1. Both halves, because a stored field nothing reads is not delivered."""
    planner = _agent(client, key, "planner-1")
    _item, did = _run(client, key, db, planner, title="stored",
                      declared={"vendor": "alibaba"}, measured="qwen3.7-plus")

    assert _telemetry(db, did).model_measured == "qwen3.7-plus"
    assert ("qwen3.7-plus", "measured") in _cells(db, proj)


def test_the_route_echoes_the_measurement_it_stored(client, key, db):
    """The post's own reply, which is all the supervisor gets back. A field the route
    accepts and then cannot show is a field an operator cannot check."""
    planner = _agent(client, key, "planner-echo")
    item = _ok(_mcp(client, key, "create_item", {
        "title": "echo", "status": "next", "touchpoints": ["backend/app/x.py"]}))["id"]
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner}))
    child = _agent(client, key, "child-echo", parent_agent_id=planner,
                   capabilities={"instance": "echo", "vendor": "alibaba"})
    assert items_svc.claim_item(db, item, child) is not None

    served = _post(client, key, {"delegation_id": d["delegation_id"],
                                 "model": "qwen3.7-plus"}, expect=202)

    assert served["model_measured"] == "qwen3.7-plus"
    assert served["model"] is None, "the declared half is the server's to derive, not the post's"


# ---- 2: null stays null ---------------------------------------------------------------------

def test_an_unmeasured_attempt_stores_null_and_its_cell_has_no_model(client, key, db, proj):
    """Acceptance 2, in the shape the 123 real cells have: a child that declared a vendor and
    no model, and a stream nobody could read. The cell keeps the empty declared name rather
    than borrowing the request — better a gap than a confident wrong attribution."""
    planner = _agent(client, key, "planner-2")
    _item, did = _run(client, key, db, planner, title="unmeasured",
                      declared={"vendor": "alibaba"}, measured=None)

    row = _telemetry(db, did)
    assert row.model_measured is None
    cells = _cells(db, proj)
    assert ("", "declared") in cells
    assert all(source != "measured" for _model, source in cells)


def test_a_repost_without_a_measurement_does_not_clear_one(client, key, db):
    """D3's merge rule applied to the new field: a second post from a supervisor that could
    not read the stream this time must not un-measure what the first one recorded. The
    opposite error — writing a null over a value — is the one `_merge` exists to prevent."""
    planner = _agent(client, key, "planner-repost")
    _item, did = _run(client, key, db, planner, title="repost",
                      declared={"vendor": "alibaba"}, measured="qwen3.7-plus")

    _post(client, key, {"delegation_id": did, "binary_version": "0.23.0"})

    assert _telemetry(db, did).model_measured == "qwen3.7-plus"


# ---- 3: measured wins over requested, and the cell says which it used -----------------------

def test_the_measured_model_wins_when_it_differs_from_the_requested_one(client, key, db, proj):
    """Acceptance 3, on the real pair: qwen3.8-max was requested and qwen3.7-plus answered,
    which is what 1488 of 1489 real children did. THE SABOTAGE TARGET — store the request
    instead and this fails on both halves."""
    planner = _agent(client, key, "planner-3")
    _item, did = _run(client, key, db, planner, title="differs",
                      declared={"vendor": "alibaba", "model": "qwen3.8-max"},
                      measured="qwen3.7-plus")

    row = _telemetry(db, did)
    assert row.model_measured == "qwen3.7-plus", "the model that answered"
    assert row.model == "qwen3.8-max", "the declaration is kept beside it, not overwritten"

    cells = _cells(db, proj)
    assert ("qwen3.7-plus", "measured") in cells
    assert ("qwen3.8-max", "declared") not in cells, "the request is not a measurement"


def test_a_declared_model_with_no_measurement_is_still_attributed(client, key, db, proj):
    """The other side of the preference: a declaration is not nothing, it is a weaker fact,
    and dropping it would empty cells that were populated before this column existed."""
    planner = _agent(client, key, "planner-4")
    _item, did = _run(client, key, db, planner, title="declared-only",
                      declared={"vendor": "alibaba", "model": "qwen3.8-max"}, measured=None)

    assert _telemetry(db, did).model_measured is None
    assert ("qwen3.8-max", "declared") in _cells(db, proj)


def test_a_cell_holding_both_kinds_of_attempt_says_mixed(client, key, db, proj):
    """Two attempts of one vendor land in one cell — one measured `qwen3.7-plus`, one that
    declared it. Labelling that cell `measured` would claim a provenance half its evidence
    does not have, and `declared` would hide the measurement."""
    planner = _agent(client, key, "planner-5")
    _run(client, key, db, planner, title="mixed-a", declared={"vendor": "alibaba"},
         measured="qwen3.7-plus")
    _run(client, key, db, planner, title="mixed-b",
         declared={"vendor": "alibaba", "model": "qwen3.7-plus"}, measured=None)

    assert ("qwen3.7-plus", "mixed") in _cells(db, proj)


# ---- the ordering trap ----------------------------------------------------------------------

@pytest.mark.parametrize("post_first", [True, False])
def test_the_measurement_survives_whichever_writer_lands_first(client, key, db, post_first):
    """There is no ordering requirement between the supervisor's post and the server's own
    derivation (PRD-38 D3), and `derive` writes the DECLARED model unconditionally. Sharing
    one column between the two would make the measurement survive only in the order nobody
    runs: the child exits and is reaped before its item is ever signed off."""
    planner = _agent(client, key, f"planner-order-{post_first}")
    _item, did = _run(client, key, db, planner, title=f"order-{post_first}",
                      declared={"vendor": "alibaba", "model": "qwen3.8-max"},
                      measured="qwen3.7-plus", post_first=post_first)

    row = _telemetry(db, did)
    assert row.model_measured == "qwen3.7-plus"
    assert row.model == "qwen3.8-max"


# ---- the rollup reads the same rule ---------------------------------------------------------

def test_the_rollup_attributes_to_the_same_model_as_the_live_cell(client, key, db, proj):
    """The page's grid is rolled, the resolver's cells are live, and they are two surfaces of
    one matrix. Attributing an attempt to the measured model in one and to the declaration in
    the other would leave the page and the brief disagreeing about the same run."""
    planner = _agent(client, key, "planner-roll")
    _item, did = _run(client, key, db, planner, title="rolled",
                      declared={"vendor": "alibaba", "model": "qwen3.8-max"},
                      measured="qwen3.7-plus")

    row = _telemetry(db, did)
    assert hsvc.model_of(row) == ("qwen3.7-plus", "measured")

    hsvc.roll(db, proj)
    db.commit()
    db.expire_all()
    rolled = {r.model for r in db.scalars(select(HarnessRollup).where(
        HarnessRollup.project_id == proj)).all()}
    assert "qwen3.7-plus" in rolled
    assert "qwen3.8-max" not in rolled


def test_model_of_reports_a_null_measurement_as_declared_not_as_the_request():
    """The rule on its own, including the empty declared name the 123 real cells carry."""
    measured = AttemptTelemetry(id="at_x", model="qwen3.8-max", model_measured="qwen3.7-plus")
    assert hsvc.model_of(measured) == ("qwen3.7-plus", "measured")

    declared = AttemptTelemetry(id="at_y", model="qwen3.8-max", model_measured=None)
    assert hsvc.model_of(declared) == ("qwen3.8-max", "declared")

    pre_measurement = AttemptTelemetry(id="at_z", model="", model_measured=None)
    assert hsvc.model_of(pre_measurement) == ("", "declared")
