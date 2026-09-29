"""AL-43: the audit ledger records who did what, and reads back scoped to the
caller's readable projects.

The second half of the file is PRD-47 S10 / GRPH-961: the Activity aggregates the same
endpoint now returns — lenses, the stacked histogram, the three facets — and the places
where an absent answer has to say it is absent instead of reading as a clean zero.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _login(client, email="alex@ascme-labs.com"):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _key(client, auth, **body):
    return client.post("/api/api-keys", json={"name": "a", **body}, headers=auth).json()["plaintext"]


def _mcp(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _events(client, auth, **params):
    return client.get("/api/events", params=params, headers=auth).json()


def test_mcp_write_is_audited_with_key_actor(client, auth):
    key = _key(client, auth, project_id="core")
    created = _mcp(client, key, "create_item", {"title": "audited"})["structuredContent"]
    ev = _events(client, auth, project_id="core")
    top = ev["results"][0]
    assert top["action"] == "create_item"
    assert top["actor_type"] == "apikey"
    assert top["actor_label"] == "a"
    assert top["target_id"] == created["id"]
    assert top["surface"] == "mcp"
    # AL-197: the agent action also names the human principal behind the key
    assert top["agent"] == "a"                       # the key is the agent
    assert top["principal"] and top["principal"] != "a"  # its owner (the human) is captured


def test_principal_and_agent_normalization():
    """The (human, agent) pair the audit UI renders, across the three event shapes."""
    from app.models import Event
    from app.services.events import _principal_and_agent

    agent_ev = Event(actor_type="apikey", actor_id="k1", actor_label="loop",
                     meta={"principal": {"id": "u1", "label": "alex"}})
    assert _principal_and_agent(agent_ev) == ("alex", "loop")

    assistant_ev = Event(actor_type="user", actor_id="u1", actor_label="alex",
                         meta={"origin": "assistant:anthropic"})
    assert _principal_and_agent(assistant_ev) == ("alex", "assistant:anthropic")

    plain_ev = Event(actor_type="user", actor_id="u1", actor_label="alex", meta=None)
    assert _principal_and_agent(plain_ev) == ("alex", "")

    legacy_key = Event(actor_type="apikey", actor_id="k9", actor_label="old-key", meta=None)
    assert _principal_and_agent(legacy_key) == ("", "old-key")  # no owner recorded → agent only


def test_mcp_read_is_not_audited(client, auth):
    key = _key(client, auth, project_id="core")
    before = _events(client, auth, project_id="core")["total"]
    _mcp(client, key, "search_items", {"query": "x"})
    after = _events(client, auth, project_id="core")["total"]
    assert after == before


def test_rest_key_lifecycle_is_audited(client, auth):
    created = client.post("/api/api-keys", json={"name": "temp", "project_id": "core"}, headers=auth).json()
    client.delete(f"/api/api-keys/{created['id']}", headers=auth)
    actions = [e["action"] for e in _events(client, auth, project_id="core")["results"]]
    assert "create_api_key" in actions
    assert "revoke_api_key" in actions


def test_project_update_is_audited(client, auth):
    client.patch("/api/projects/core", json={"description": "changed"}, headers=auth)
    top = _events(client, auth, project_id="core", action="update_project")["results"][0]
    assert top["actor_type"] == "user"
    assert "description" in top["meta"]["fields"]


def test_events_scoped_to_readable_projects(client):
    # ops: read core, none on web. Create a web event as alex, ensure ops can't see web,
    # and can't even query web's ledger.
    alex = _login(client, "alex@ascme-labs.com")
    kate_web_key = _key(client, alex, project_id="web")
    _mcp(client, kate_web_key, "create_item", {"title": "web item"})

    ops = _login(client, "ops@ascme-labs.com")
    # ops querying web directly → 404 (existence-hiding)
    assert client.get("/api/events", params={"project_id": "web"}, headers=ops).status_code == 404
    # ops's default (all-readable) ledger never includes web events
    all_ops = _events(client, ops)
    assert all(e["project_id"] != "web" for e in all_ops["results"])


def test_events_paginated_newest_first(client, auth):
    key = _key(client, auth, project_id="core")
    for i in range(3):
        _mcp(client, key, "create_item", {"title": f"n{i}"})
    page = _events(client, auth, project_id="core", limit=2)
    assert page["limit"] == 2 and page["has_more"] is True
    ids = [e["id"] for e in page["results"]]
    assert ids == sorted(ids, reverse=True)


# ── Activity aggregates (PRD-47 S10 / GRPH-961) ──────────────────────────────
#
# Fixed "now" on a boundary every bucket width divides evenly, so `origin == now` and a
# bucket index is arithmetic rather than a guess about when the test ran.
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _put(db, *, action, actor_type="user", actor_id="u1", actor_label="alex",
         surface="rest", target_type="", target_id="", project_id="core",
         minutes_ago=5, meta=None):
    """Insert one event at NOW - `minutes_ago`.

    The default is 5, not 0: the range window is half-open at the top, so a row stamped
    exactly at the aligned origin belongs to the NEXT bar and a test that meant "just
    now" would silently assert against an empty view.
    """
    from app.models import Event
    db.add(Event(
        ts=NOW - timedelta(minutes=minutes_ago),
        actor_type=actor_type, actor_id=actor_id, actor_label=actor_label,
        surface=surface, action=action, target_type=target_type,
        target_id=target_id, project_id=project_id, meta=meta,
    ))
    db.commit()


def _activity(db, **kw):
    from app.services import events as svc
    kw.setdefault("now", NOW)
    return svc.list_events(db, project_ids=kw.pop("project_ids", ["core"]), **kw)


def test_unranged_ledger_says_the_histogram_was_not_requested(client, auth):
    """No `range` is a different answer from 48 empty bars. Sabotage: zero-fill the
    buckets when range_key is None — this fails."""
    page = _events(client, auth, project_id="core")
    hist = page["histogram"]
    assert hist["coverage"] == "not_requested"
    assert hist["buckets"] == []
    assert hist["bucket_seconds"] is None
    # The six lenses are still counted — over all history, which is what no-range means.
    assert [l["id"] for l in page["lenses"]] == [
        "everything", "agent_writes", "human_decisions", "keys_access", "memory", "rejected",
    ]


def test_rejected_lens_publishes_exactly_what_it_covers(db):
    """The tile must not imply it sees every refusal. Sabotage: drop `covers` from the
    rejected definition — this fails."""
    from app.services import events as svc

    _put(db, action="sign_off_refused", actor_type="apikey", actor_id="k1",
         actor_label="worker", surface="mcp", target_type="item")
    _put(db, action="bounce", actor_type="apikey", actor_id="k1", surface="mcp")
    _put(db, action="auto_reject_shard", actor_type="system", actor_label="memory-auto-triage",
         surface="system", target_type="shard")

    out = _activity(db, range_key="24h")
    rejected = next(l for l in out["lenses"] if l["id"] == "rejected")
    assert rejected["covers"] == list(svc.REFUSAL_ACTIONS)
    assert rejected["count"] == 1                      # only the recorded refusal
    for name in svc.REFUSAL_ACTIONS:
        assert name in rejected["hint"]
    # The other two refusals are NOT lost — they are in Everything, and the hint is what
    # stops the tile reading as "one refusal happened in this project".
    assert next(l for l in out["lenses"] if l["id"] == "everything")["count"] == 3


def test_histogram_series_are_mutually_exclusive(db):
    """A refusal made by an agent key counts once, in `rejected` — a stacked bar whose
    total exceeds the events in it is a number nobody can reconcile. Sabotage: classify
    by actor_type before the refusal check — this fails."""
    _put(db, action="sign_off_refused", actor_type="apikey", actor_id="k1",
         surface="mcp", minutes_ago=30)
    _put(db, action="create_item", actor_type="apikey", actor_id="k1",
         surface="mcp", minutes_ago=30)
    _put(db, action="update_project", minutes_ago=30)

    hist = _activity(db, range_key="24h")["histogram"]
    assert hist["bucket_seconds"] == 1800               # the design's 30-minute bar
    assert len(hist["buckets"]) == 48
    bar = hist["buckets"][47]                           # 30 min before an aligned origin
    assert (bar["agent"], bar["human"], bar["rejected"], bar["system"]) == (1, 1, 1, 0)
    assert sum(bar[s] for s in ("agent", "human", "rejected", "system")) == 3
    assert hist["coverage"] == "full"


def test_system_actor_is_its_own_series_not_a_human(db):
    """Auto-triage and the Stripe webhook record actor_type="system". Folding them into
    `human` attributes a machine's decision to a person. Sabotage: return "human" for
    any non-apikey actor — this fails."""
    _put(db, action="auto_publish_shard", actor_type="system", actor_id="",
         actor_label="memory-auto-triage", surface="system", target_type="shard",
         minutes_ago=30)

    bar = _activity(db, range_key="24h")["histogram"]["buckets"][47]
    assert bar["system"] == 1 and bar["human"] == 0


def test_bucket_width_follows_the_range(db):
    """48 bars whatever the range; 24h is the design's 30 minutes."""
    for rng, width in (("1h", 75), ("24h", 1800), ("7d", 12600), ("30d", 54000)):
        hist = _activity(db, range_key=rng)["histogram"]
        assert hist["bucket_seconds"] == width, rng
        assert len(hist["buckets"]) == 48, rng


def test_bucket_filter_selects_the_clicked_bar(db):
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", surface="mcp",
         minutes_ago=30)
    _put(db, action="update_item", actor_type="apikey", actor_id="k1", surface="mcp",
         minutes_ago=120)

    out = _activity(db, range_key="24h", bucket=47)
    assert [e["action"] for e in out["results"]] == ["create_item"]
    assert out["total"] == 1
    # The histogram still shows every bar, or clicking one would leave no way to the next.
    assert len(out["histogram"]["buckets"]) == 48
    assert out["histogram"]["buckets"][44]["agent"] == 1


def test_bucket_without_a_range_is_422_not_a_guess(client, auth):
    assert client.get("/api/events", params={"project_id": "core", "bucket": 3},
                      headers=auth).status_code == 422


def test_unknown_lens_and_range_are_422_not_a_widening(client, auth):
    """An unrecognised filter that fell back to "everything" would answer a question
    nobody asked and look like a clean result. Sabotage: return None from `_lens_clause`
    for an unknown id — the first assert fails."""
    assert client.get("/api/events", params={"project_id": "core", "lens": "nope"},
                      headers=auth).status_code == 422
    assert client.get("/api/events", params={"project_id": "core", "range": "9y"},
                      headers=auth).status_code == 422


def test_lens_filter_reaches_the_rows(client, auth, db):
    """Sabotage the CALL, not the callee: build the lens clause and never pass it to the
    row query. The lens counts stay right and the rows do not."""
    _put(db, action="create_api_key", target_type="api_key")
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", surface="mcp",
         target_type="item")

    page = client.get("/api/events", params={"project_id": "core", "lens": "keys_access"},
                      headers=auth).json()
    assert page["total"] == 1
    assert [e["action"] for e in page["results"]] == ["create_api_key"]


def test_empty_target_type_selects_untyped_rows_not_all(client, auth, db):
    """`target_type=""` is most of the ledger — MCP writes record no target type — so the
    empty string is a value, not an absent filter."""
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", surface="mcp")
    _put(db, action="update_item", actor_type="apikey", actor_id="k1", surface="mcp",
         target_type="item", target_id="AL-1")

    untyped = client.get("/api/events?project_id=core&target_type=", headers=auth).json()
    assert [e["action"] for e in untyped["results"]] == ["create_item"]
    typed = client.get("/api/events", params={"project_id": "core", "target_type": "item"},
                       headers=auth).json()
    assert [e["action"] for e in typed["results"]] == ["update_item"]
    assert client.get("/api/events", params={"project_id": "core"}, headers=auth).json()["total"] == 2


def test_target_id_pivot_reads_one_targets_history(client, auth, db):
    """The panel's "all events on this target" pivot. Picking one target must not
    collapse the object facet to it — that would read as "this project has one kind of
    object in it"."""
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", surface="mcp",
         target_type="item", target_id="AL-1")
    _put(db, action="update_item", actor_type="apikey", actor_id="k1", surface="mcp",
         target_type="item", target_id="AL-2")
    _put(db, action="update_project", target_type="project", target_id="core")

    page = client.get("/api/events", params={"project_id": "core", "target_id": "AL-1"},
                      headers=auth).json()
    assert [e["action"] for e in page["results"]] == ["create_item"]
    assert page["filters"]["target_id"] == "AL-1"
    kinds = {v["value"] for v in page["facets"]["object"]["values"]}
    assert kinds == {"item", "project"}


def test_object_facet_labels_untyped_instead_of_dropping_it(db):
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", surface="mcp")
    _put(db, action="update_project", target_type="project")

    obj = _activity(db, range_key="24h")["facets"]["object"]
    by_value = {v["value"]: v for v in obj["values"]}
    assert by_value[""]["label"] == "untyped" and by_value[""]["count"] == 1
    assert by_value["project"]["count"] == 1


def test_person_facet_survives_its_own_selection(db):
    """A facet filtered by itself collapses to the one value already picked, which reads
    as "there is only one person here"."""
    _put(db, action="update_project", actor_id="u1", actor_label="alex")
    _put(db, action="update_project", actor_id="u2", actor_label="kate")

    out = _activity(db, range_key="24h", actor="u1")
    assert out["total"] == 1
    labels = {v["label"] for v in out["facets"]["person"]["values"]}
    assert labels == {"alex", "kate"}


def test_facet_truncation_is_stated(db, monkeypatch):
    """40 of 200 people shown must not read as "these are all of them"."""
    from app.services import events as svc
    monkeypatch.setattr(svc, "FACET_LIMIT", 2)
    for i, surf in enumerate(("rest", "mcp", "public")):
        _put(db, action="update_project", surface=surf, minutes_ago=i + 1)

    facet = _activity(db, range_key="24h")["facets"]["surface"]
    assert facet["truncated"] is True and len(facet["values"]) == 2


def test_total_counts_what_the_filters_matched(client, auth, db):
    """`total` used to ignore `action`, so a filtered page reported the size of the whole
    ledger and `has_more` lied about it."""
    for i in range(3):
        _put(db, action="update_project", minutes_ago=i)
    _put(db, action="create_api_key", target_type="api_key")

    page = client.get("/api/events", params={"project_id": "core", "action": "create_api_key",
                                             "limit": 10}, headers=auth).json()
    assert page["total"] == 1 and page["has_more"] is False


def test_range_excludes_events_outside_the_window(db):
    _put(db, action="update_project", minutes_ago=10)
    _put(db, action="update_project", minutes_ago=60 * 24 * 40)   # 40 days back

    assert _activity(db, range_key="24h")["total"] == 1
    assert _activity(db, range_key="30d")["total"] == 1
    assert _activity(db)["total"] == 2                            # no range: all history


def test_ledger_total_ignores_every_filter(db):
    """The view's two empties — "never recorded anything" and "nothing matches this
    selection" — need a number no filter touches. Sabotage: reuse `total` — this fails."""
    _put(db, action="update_project", minutes_ago=10)
    _put(db, action="update_project", minutes_ago=60 * 24 * 40)

    out = _activity(db, range_key="24h", lens="rejected")
    assert out["total"] == 0
    assert out["ledger_total"] == 2
    assert _activity(db)["ledger_total"] == 2


def test_histogram_says_when_it_could_not_scan_the_window(db, monkeypatch):
    """Past the scan ceiling the newest events win. Reporting 48 bars computed from half
    the window would read as "nothing happened before that"."""
    from app.services import events as svc
    monkeypatch.setattr(svc, "HISTOGRAM_SCAN_CAP", 2)
    for i in range(5):
        _put(db, action="update_project", minutes_ago=i + 1)

    hist = _activity(db, range_key="24h")["histogram"]
    assert hist["coverage"] == "partial" and hist["partial"] is True
    assert hist["scanned"] == 2
    assert sum(b["human"] for b in hist["buckets"]) == 2
    assert _activity(db, range_key="24h")["total"] == 5           # rows are not capped


def test_agent_and_memory_lenses_read_the_columns_that_exist(db):
    _put(db, action="create_item", actor_type="apikey", actor_id="k1", actor_label="worker",
         surface="mcp", target_type="item", meta={"principal": {"id": "u1", "label": "alex"}})
    _put(db, action="add_memory", actor_type="apikey", actor_id="k1", surface="mcp")
    _put(db, action="update_project")

    lenses = {l["id"]: l["count"] for l in _activity(db, range_key="24h")["lenses"]}
    assert lenses["agent_writes"] == 2
    assert lenses["human_decisions"] == 1
    assert lenses["memory"] == 1
    assert lenses["rejected"] == 0        # a true zero, and the tile still says what it covers
    assert lenses["everything"] == 3


def test_activity_never_reads_another_projects_ledger(db):
    _put(db, action="update_project", project_id="core")
    _put(db, action="update_project", project_id="web", actor_id="u9", actor_label="intruder")

    out = _activity(db, range_key="24h")
    assert out["total"] == 1
    assert {v["label"] for v in out["facets"]["person"]["values"]} == {"alex"}
    assert all(b["human"] <= 1 for b in out["histogram"]["buckets"])

