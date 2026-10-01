"""GRPH-1003 — the tier map: a deployment's override, stored beside the catalog it layers on.

`Build.dc.html` draws the tier map as an editable panel and what shipped was a read-only
catalog, so the map was retuned by hand in `fleet/src/gbfleet/matrix.toml` — package data
inside the published wheel. These tests pin the three properties the item's decision turns
on, from the storage side:

- an override SURVIVES a restart, because it lives in the deployment's DB and not in the
  process that saved it (the fleet half of the same claim is in `fleet/tests/test_matrix.py`);
- clearing falls back to the PACKAGED matrix and never to an empty map, which would route
  nothing while reading as a clean result;
- a cell names a model the catalog carries, so an override pins a committed row rather than
  inventing one with no status behind it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import TierOverride
from app.services import fleet_matrix


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "TierMap"}, headers=auth).json()["id"]


@pytest.fixture()
def other(client, auth):
    return client.post("/api/projects", json={"name": "Elsewhere"}, headers=auth).json()["id"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _cell(payload, harness, tier):
    for c in payload["cells"]:
        if c["harness"] == harness and c["tier"] == tier:
            return c
    raise AssertionError(f"no cell {harness}/{tier} in {payload['cells']}")


def _save(client, auth, proj, cells):
    r = client.put("/api/fleet/tier-map", json={"project_id": proj, "cells": cells},
                   headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---- the panel is served a map, not only a catalog ------------------------------------------

def test_the_map_is_served_beside_the_catalog_with_the_packaged_model_named(client, auth, proj):
    """`cells` is the editable surface. Every cell carries what the committed matrix says, so
    the panel can show an override AS an override rather than as the only value it has."""
    got = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth)
    assert got.status_code == 200, got.text
    payload = got.json()
    assert payload["rows"], "the catalog facts went missing when the map was added"
    assert payload["overridden"] is False

    cell = _cell(payload, "gbagent", "cheap")
    assert cell["packaged_model"] == "qwen3.6:35b-a3b-coding-mtp-det"
    assert cell["override"] is None and cell["overridden"] is False
    assert cell["effective_model"] == cell["packaged_model"]
    # Two committed rows for this cell, so there is a choice to make — the point of the panel.
    assert cell["models"] == ["qwen3.6:35b-a3b-coding-mtp-det", "qwen3-coder:30b"]


def test_a_cell_the_catalog_names_no_model_for_is_served_as_one_not_dropped(client, auth, proj):
    """qwen-code/cheap and codex/frontier carry `model = ""`: the harness runs its own default.
    Omitting those cells would make the map look complete while hiding the two harnesses that
    have nothing to pin — and there is nothing to pin, which is a fact, not a gap."""
    payload = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    cell = _cell(payload, "qwen-code", "cheap")
    assert cell["models"] == [] and cell["packaged_model"] == ""
    assert cell["effective_model"] == "" and cell["overridden"] is False


def test_no_cell_is_graded_until_something_is_measured(client, auth, proj, monkeypatch):
    """*Inherit from performance grading* must have nothing to inherit from on a fresh
    project. `graded_model: null` is the honest answer; the packaged model wearing a grading's
    clothes would fill every cell and look like a measurement (PRD-47 G3, nothing invented)."""
    payload = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert all(c["graded_model"] is None for c in payload["cells"])

    # With a measured cell above the floor, grading names a model — and only for the harness
    # the measurement is about, joined on (vendor, model) rather than guessed at.
    from app.services import delegation as delegation_svc
    monkeypatch.setattr(delegation_svc, "measured", lambda _db, _pid: [
        {"vendor": "gbagent", "model": "qwen3-coder:30b", "capability": "B5", "layer": "project",
         "quality": {"value": 0.9, "n": fleet_matrix.GRADE_FLOOR}},
    ])
    graded = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert _cell(graded, "gbagent", "cheap")["graded_model"] == "qwen3-coder:30b"
    assert _cell(graded, "claude", "frontier")["graded_model"] is None


def test_grading_below_the_floor_is_not_grading(client, auth, proj, monkeypatch):
    """One attempt under the floor is noise. Naming a model from it would be the same invention
    with a smaller number on it.

    `n` is a LITERAL, not `GRADE_FLOOR - 1`. The first version of this test derived its input
    from the constant it exists to pin, so lowering the floor to 0 lowered the sample with it
    and the test still passed — sabotage found that, which is the only reason it is worth
    anything. The guard assertion keeps the literal honest if the floor ever moves.
    """
    from app.services import delegation as delegation_svc
    assert fleet_matrix.GRADE_FLOOR > 4, "re-pick the literal n below to stay under the floor"
    monkeypatch.setattr(delegation_svc, "measured", lambda _db, _pid: [
        {"vendor": "gbagent", "model": "qwen3-coder:30b", "capability": "B5", "layer": "project",
         "quality": {"value": 1.0, "n": 4}},
    ])
    payload = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert _cell(payload, "gbagent", "cheap")["graded_model"] is None

    # The control: the same cell one attempt above the floor IS graded, so the assertion above
    # is about the floor and not about a join that never matches anything.
    monkeypatch.setattr(delegation_svc, "measured", lambda _db, _pid: [
        {"vendor": "gbagent", "model": "qwen3-coder:30b", "capability": "B5", "layer": "project",
         "quality": {"value": 1.0, "n": 5}},
    ])
    graded = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert _cell(graded, "gbagent", "cheap")["graded_model"] == "qwen3-coder:30b"


# ---- saving, and what survives a restart ------------------------------------------------------

def test_a_saved_override_changes_what_the_deployment_runs(client, auth, proj):
    out = _save(client, auth, proj,
                [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"}])
    cell = _cell(out, "gbagent", "cheap")
    assert cell["override"] == "qwen3-coder:30b" and cell["overridden"] is True
    assert cell["effective_model"] == "qwen3-coder:30b"
    assert out["overridden"] is True
    # The catalog facts are untouched: an override layers on top, it does not rewrite history.
    assert cell["packaged_model"] == "qwen3.6:35b-a3b-coding-mtp-det"
    # And the untouched cell is still packaged.
    assert _cell(out, "claude", "frontier")["effective_model"] == "opus"


def test_an_override_survives_a_fleet_restart(client, auth, proj, db):
    """Criterion: an override survives a fleet restart.

    Restart means a NEW process reading the map again, so what is asserted is that the map
    lives somewhere the saving process does not own: a fresh Session sees the row, a fresh
    HTTP read sees the override, and `fleet_status` — the call a supervisor makes at wave
    start — carries it. Nothing here is served from a cache built during the save.
    """
    _save(client, auth, proj, [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"}])

    # A brand-new session, i.e. a process that was not the one that wrote it.
    from app.db import SessionLocal
    from app.services import fleet as fleet_svc
    fresh = SessionLocal()
    try:
        stored = fresh.scalars(select(TierOverride).where(
            TierOverride.project_id == proj)).all()
        assert [(r.harness, r.tier, r.model) for r in stored] == [
            ("gbagent", "cheap", "qwen3-coder:30b")]
        assert all(r.updated_at is not None for r in stored), "an undatable map cannot be argued with"

        # What the supervisor reads at wave start, from the fresh session.
        status = fleet_svc.fleet_status(fresh, proj)
        assert status["tier_map"]["overridden"] is True
        assert [(o["harness"], o["tier"], o["model"])
                for o in status["tier_map"]["overrides"]] == [("gbagent", "cheap", "qwen3-coder:30b")]
    finally:
        fresh.close()

    # And over the wire, which is how a restarted gbfleet actually reaches it.
    again = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert again["overridden"] is True
    assert _cell(again, "gbagent", "cheap")["effective_model"] == "qwen3-coder:30b"
    overview = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()
    assert _cell(overview["matrix"], "gbagent", "cheap")["overridden"] is True


def test_resaving_one_cell_updates_it_rather_than_adding_a_second(client, auth, proj, db):
    """One row per cell. Two would make "which model does this harness run for this tier" a
    question with two answers, and the supervisor reads it once."""
    _save(client, auth, proj, [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"}])
    first_id = db.scalars(select(TierOverride)).one().id
    out = _save(client, auth, proj,
                [{"harness": "gbagent", "tier": "cheap", "model": "qwen3.6:35b-a3b-coding-mtp-det"}])
    assert _cell(out, "gbagent", "cheap")["override"] == "qwen3.6:35b-a3b-coding-mtp-det"
    db.expire_all()
    rows = db.scalars(select(TierOverride)).all()
    assert len(rows) == 1 and rows[0].id == first_id


def test_saving_is_a_replace_so_a_cell_left_out_is_a_cell_cleared(client, auth, proj):
    """PUT semantics, and the reason the panel saves its whole draft. Asserted because the
    quiet version — a partial save that kept the cells it did not mention — would leave an
    override running that the operator just removed from the screen."""
    _save(client, auth, proj, [
        {"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"},
        {"harness": "claude", "tier": "frontier", "model": "opus"},
    ])
    out = _save(client, auth, proj, [{"harness": "claude", "tier": "frontier", "model": "opus"}])
    assert _cell(out, "gbagent", "cheap")["override"] is None
    assert _cell(out, "gbagent", "cheap")["effective_model"] == "qwen3.6:35b-a3b-coding-mtp-det"
    assert _cell(out, "claude", "frontier")["overridden"] is True


def test_an_explicit_empty_model_clears_that_one_cell(client, auth, proj):
    out = _save(client, auth, proj, [{"harness": "gbagent", "tier": "cheap", "model": ""}])
    assert out["overridden"] is False
    assert _cell(out, "gbagent", "cheap")["effective_model"] == "qwen3.6:35b-a3b-coding-mtp-det"


# ---- clearing falls back to the packaged matrix, never to empty --------------------------------

def test_clearing_falls_back_to_the_packaged_matrix_and_not_to_empty(client, auth, proj, db):
    """Criterion: clearing an override falls back to the packaged matrix and not to empty.

    The reply is the panel's next render, so an empty `cells` would tell the operator their
    deployment can now route nothing. Both halves are asserted — that every override row is
    GONE, and that every cell is still served with its packaged model — because keeping blank
    rows instead of deleting them would satisfy the second and leave "cleared" and "never set"
    as different states.
    """
    _save(client, auth, proj, [
        {"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"},
        {"harness": "claude", "tier": "frontier", "model": "opus"},
    ])
    r = client.delete(f"/api/fleet/tier-map?project_id={proj}", headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()

    assert out["overridden"] is False
    assert out["cells"], "clearing returned an empty tier map — that routes nothing"
    assert all(c["override"] is None and c["overridden"] is False for c in out["cells"])
    by_cell = {(c["harness"], c["tier"]): c for c in out["cells"]}
    assert by_cell[("gbagent", "cheap")]["effective_model"] == "qwen3.6:35b-a3b-coding-mtp-det"
    assert by_cell[("claude", "frontier")]["effective_model"] == "opus"
    # And it is the SAME cell list an untouched deployment gets, not a reduced one.
    pristine = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert {(c["harness"], c["tier"], c["effective_model"]) for c in pristine["cells"]} == \
           {(c["harness"], c["tier"], c["effective_model"]) for c in out["cells"]}

    db.expire_all()
    assert db.scalars(select(TierOverride)).all() == [], "cleared by blanking, not by deleting"


def test_clearing_a_map_that_was_never_set_is_still_the_packaged_matrix(client, auth, proj):
    """The other direction of the same rule: a clear on a fresh project must not be the thing
    that empties the map."""
    out = client.delete(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert out["cells"] and out["overridden"] is False
    assert _cell(out, "gbagent", "cheap")["effective_model"] == "qwen3.6:35b-a3b-coding-mtp-det"


# ---- an override pins a committed row; it cannot invent one ------------------------------------

def test_a_model_the_catalog_does_not_carry_for_that_cell_is_refused(client, auth, proj, db):
    """422, and NOTHING written. An override decides which committed row a tier runs; letting
    it name any string would hand the resolver a model no status describes, which is the
    invention this module exists to refuse."""
    before = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "gbagent", "tier": "cheap", "model": "gpt-9-nobody-committed"}],
    }, headers=auth)
    assert r.status_code == 422, r.text
    assert "gpt-9-nobody-committed" in r.text and "qwen3-coder:30b" in r.text
    after = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert after["overridden"] is False
    assert {(c["harness"], c["tier"], c["override"]) for c in after["cells"]} == \
           {(c["harness"], c["tier"], c["override"]) for c in before["cells"]}
    db.expire_all()
    assert db.scalars(select(TierOverride)).all() == []


def test_a_valid_cell_does_not_rescue_an_invalid_one_in_the_same_save(client, auth, proj, db):
    """Validated as a whole BEFORE any write. A map half-applied is a routing nobody can read
    back, and "the save 422'd but one of my two cells took" is worse than either outcome."""
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "claude", "tier": "frontier", "model": "opus"},
                  {"harness": "gbagent", "tier": "cheap", "model": "not-a-model"}],
    }, headers=auth)
    assert r.status_code == 422, r.text
    db.expire_all()
    assert db.scalars(select(TierOverride)).all() == [], "the valid cell was written anyway"


def test_a_cell_the_catalog_does_not_name_is_refused(client, auth, proj):
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "gbagent", "tier": "frontier", "model": "opus"}],
    }, headers=auth)
    assert r.status_code == 422, r.text
    assert "not a cell the catalog names" in r.text


def test_a_cell_with_no_model_to_pin_cannot_be_overridden(client, auth, proj):
    """qwen-code/cheap names no model, so there is nothing to pin and offering one would be
    inventing a model for a harness whose row says the binary picks its own default."""
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "qwen-code", "tier": "cheap", "model": "qwen3.8-max"}],
    }, headers=auth)
    assert r.status_code == 422, r.text
    assert "no model at all" in r.text


def test_a_cell_listed_twice_is_refused_rather_than_last_write_wins(client, auth, proj):
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"},
                  {"harness": "gbagent", "tier": "cheap", "model": "qwen3.6:35b-a3b-coding-mtp-det"}],
    }, headers=auth)
    assert r.status_code == 422 and "listed twice" in r.text


def test_a_cell_naming_no_harness_or_tier_is_refused(client, auth, proj):
    """A structurally missing field is refused by the schema and a blank one by the service —
    both 422, and the service's own wording is asserted where it is the service answering."""
    for cell in ({"tier": "cheap", "model": "opus"}, {"harness": "claude", "model": "opus"}):
        r = client.put("/api/fleet/tier-map", json={"project_id": proj, "cells": [cell]},
                       headers=auth)
        assert r.status_code == 422, (cell, r.text)
    for cell in ({"harness": "  ", "tier": "frontier", "model": "opus"},
                 {"harness": "claude", "tier": "", "model": "opus"}):
        r = client.put("/api/fleet/tier-map", json={"project_id": proj, "cells": [cell]},
                       headers=auth)
        assert r.status_code == 422, (cell, r.text)
        assert "names its harness and its tier" in r.text


# ---- scoping and the gate ----------------------------------------------------------------------

def test_an_override_is_this_project_and_not_the_next_one(client, auth, proj, other):
    """Two projects on one deployment route independently, which is the deliberate cost the
    item's decision names — so it has to actually be per project and not instance-wide."""
    _save(client, auth, proj, [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"}])
    elsewhere = client.get(f"/api/fleet/tier-map?project_id={other}", headers=auth).json()
    assert elsewhere["overridden"] is False
    assert _cell(elsewhere, "gbagent", "cheap")["effective_model"] == "qwen3.6:35b-a3b-coding-mtp-det"
    here = client.get(f"/api/fleet/tier-map?project_id={proj}", headers=auth).json()
    assert here["overridden"] is True


def test_writing_the_map_takes_the_project_write_gate(client, proj):
    """Retuning how a wave routes is a write, not a preference: an unauthenticated caller gets
    no map to save."""
    r = client.put("/api/fleet/tier-map", json={
        "project_id": proj,
        "cells": [{"harness": "gbagent", "tier": "cheap", "model": "qwen3-coder:30b"}]})
    assert r.status_code in (401, 403), r.text
    assert client.delete(f"/api/fleet/tier-map?project_id={proj}").status_code in (401, 403)
    assert client.get(f"/api/fleet/tier-map?project_id={proj}").status_code in (401, 403)


def test_the_tier_map_rides_on_fleet_status_for_a_supervisor(client, auth, proj):
    """The supervisor reads the map on the call it already makes at wave start. Absent here and
    the panel would save a map no wave ever routed on — the feature looking shipped."""
    key = client.post("/api/api-keys", json={"name": "sup", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fleet_status", "arguments": {}}}, headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res.get("isError"), res
    status = res["structuredContent"]
    assert "tier_map" in status, "the roster dropped the tier map the supervisor routes on"
    assert status["tier_map"] == {"overrides": [], "overridden": False}

    _save(client, auth, proj, [{"harness": "claude", "tier": "frontier", "model": "opus"}])
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "fleet_status", "arguments": {}}}, headers={"X-API-Key": key})
    status = r.json()["result"]["structuredContent"]
    assert status["tier_map"]["overridden"] is True
    assert status["tier_map"]["overrides"][0]["model"] == "opus"


def test_an_empty_map_on_fleet_status_is_present_and_not_omitted(client, auth, proj):
    """`overridden: false` with an empty list, never a missing key. A supervisor that read an
    absent `tier_map` as "no override" would be right by accident and wrong the release that
    drops the key — the absence-reads-as-clean failure this repo keeps re-finding."""
    key = client.post("/api/api-keys", json={"name": "sup2", "project_id": proj,
                                             "scopes": ["read", "write", "gate"]},
                      headers=auth).json()["plaintext"]
    for view in ("lean", "full", "live"):
        r = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "fleet_status", "arguments": {"view": view}}},
            headers={"X-API-Key": key})
        status = r.json()["result"]["structuredContent"]
        assert "tier_map" in status, f"the {view} roster view dropped the tier map"
        assert status["tier_map"]["overrides"] == [] and status["tier_map"]["overridden"] is False


def test_the_service_answers_from_the_catalog_alone_with_no_session():
    """The no-session path — what a caller with no project has. Every cell packaged, nothing
    graded, and `cells` still served: an empty list here would be a silent gap."""
    payload = fleet_matrix.payload()
    assert payload["overridden"] is False and payload["cells"]
    assert all(c["override"] is None and c["graded_model"] is None for c in payload["cells"])
    assert all(c["effective_model"] == c["packaged_model"] for c in payload["cells"])
    assert fleet_matrix.overrides_for(None, None) == {"overrides": [], "overridden": False}
