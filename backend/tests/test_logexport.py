"""PRD-47 S15 / GRPH-966 — deployment-wide OTLP log export.

Organised by the claim each group defends, because the acceptance list is a list of claims and
each one needs a test that FAILS when the behaviour is removed:

- placement and scoping: one row for the box, and no project can get its own;
- Send test batch: a portless endpoint is that specific failure, and an unrun probe is not a
  passed one;
- the state floor: unavailable counters are unknown, never zero, and a failed read is not "off";
- the exporter: a drain sends, advances, and counts what it gives up on;
- redaction: the three choices each change exactly what they say, and the sample reflects them.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Agent, Event, LogExportBatch, LogExportConfig, Project
from app.services import events as events_svc
from app.services import logexport

PANEL = "/api/settings/log-export"
ENDPOINT = "http://localhost:4318"


@pytest.fixture()
def db(client):
    """A session of its own, opened after the app's lifespan has seeded."""
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _project(db) -> str:
    """A real project id, so records built in these tests name one.

    Read from `projects` rather than off an existing event: a fresh ledger has no events at all,
    and `project_id=None` there would make every record a deployment-scoped one — so the
    assertions about `gb.project` would pass by never being reached.
    """
    pid = db.scalar(select(Project.id).limit(1))
    assert pid, "the seeded dataset always has a project"
    return pid


def ok_transport(status: int = 200, payload: dict | None = None):
    """A collector that answers without a socket. Records what it was handed."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {})

    return httpx.MockTransport(handler), seen


def down_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def _enable(db, **over):
    """Turn export on at a reachable endpoint, starting from the current high-water marks."""
    cfg = logexport.get_config(db)
    cfg.enabled = True
    cfg.endpoint = ENDPOINT
    cfg.protocol = "http/protobuf"
    for key, value in over.items():
        setattr(cfg, key, value)
    db.commit()
    return cfg


def _event(db, action="create_item", *, project_id=None, actor_type="user", meta=None,
           actor_label="alex", surface="rest"):
    events_svc.record(db, actor_type=actor_type, actor_id="usr_1", actor_label=actor_label,
                      surface=surface, action=action, target_type="item",
                      target_id="GRPH-1", project_id=project_id, meta=meta)
    return db.scalars(select(Event).order_by(Event.id.desc()).limit(1)).first()


# ── placement and scoping ────────────────────────────────────────────────────

def test_the_config_table_has_no_project_column(db):
    """The decision, asserted structurally: a column nobody can add a value to is the strongest
    form of "one config row for the deployment"."""
    assert "project_id" not in LogExportConfig.__table__.columns
    assert LogExportConfig.__table__.primary_key.columns.keys() == ["id"]


def test_one_row_for_the_deployment_not_one_per_project(db, decoy):
    """A second project does not get its own config, and reading from either returns the one row.

    `decoy` exists precisely so this is not a test with nothing to differ from: with one project
    in the database, "scoped to this project" and "the whole instance" are the same set.
    """
    from tests.decoy import assert_populated

    assert_populated(db, decoy)
    first = logexport.get_config(db)
    first.endpoint = "http://one.example:4318"
    db.commit()

    # A different session, as a different request would use — the row must come back the same.
    from app.db import SessionLocal

    other = SessionLocal()
    try:
        second = logexport.get_config(other)
        assert second.id == first.id == logexport.SINGLETON_ID
        assert second.endpoint == "http://one.example:4318"
    finally:
        other.close()
    assert int(db.scalar(select(func.count(LogExportConfig.id)))) == 1


def test_the_panel_route_takes_no_project(db, client, auth, decoy):
    """And neither does the write. There is no parameter to pass a second project through, so
    "a second project does not get its own" is not a behaviour that can regress quietly."""
    spec = client.get("/openapi.json").json()
    for path in (p for p in spec["paths"] if "log-export" in p):
        for op in spec["paths"][path].values():
            names = {p["name"] for p in op.get("parameters", [])}
            assert "project_id" not in names, f"{path} grew a project_id"
    body = spec["paths"]["/api/settings/log-export"]["patch"].get("requestBody")
    assert body is not None
    r = client.patch(PANEL, json={"endpoint": "http://two.example:4318"}, headers=auth)
    assert r.status_code == 200
    assert int(db.scalar(select(func.count(LogExportConfig.id)))) == 1


def test_a_record_names_its_project_even_though_the_config_does_not(db, decoy):
    """The other half of the decision: exported records span projects, so each one says whose
    event it was. Scoping the CONFIG was refused; scoping the RECORD was not."""
    from tests.decoy import assert_populated

    assert_populated(db, decoy)
    _enable(db)
    core_id = _project(db)
    assert core_id != decoy["project_id"]
    _event(db, "create_item", project_id=core_id)
    _event(db, "create_item", project_id=decoy["project_id"], actor_label="decoy-agent")
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()

    groups, _marks = logexport._collect(db, cfg, logexport.Redaction.of(cfg), limit=500)
    by_project = {r["attributes"].get("gb.project") for r in groups["log"]}
    assert "DEC" in by_project, f"the decoy project's event was not exported: {by_project}"
    scoped = [r for r in groups["log"] if r["attributes"].get("gb.project_id") == decoy["project_id"]]
    assert scoped, "the decoy's event was exported without naming its project"
    assert scoped[0]["attributes"]["gb.scope"] == "project"
    assert any(r["attributes"].get("gb.project_id") == core_id for r in groups["log"]), (
        "one batch spans projects, so both must be in it and each must say whose it was")


# ── Send test batch ──────────────────────────────────────────────────────────

def test_a_portless_endpoint_is_that_specific_failure(db, client, auth):
    """The item calls this one out by name. Every other check on `http://collector.internal`
    passes — scheme, host — so a generic "could not connect" would send the operator off to
    look at their network instead of at the port they forgot."""
    r = client.post(f"{PANEL}/test-batch", json={"endpoint": "http://collector.internal"},
                    headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == logexport.NO_PORT_ERROR
    assert body["detail"] == logexport.NO_PORT_DETAIL
    # A failure that reports a count would be a failure that looks like a partial success.
    assert body["records"] is None
    assert body["latency_ms"] is None


def test_a_portless_endpoint_never_opens_a_socket(db):
    """THE CALL, not just the callee: the check must happen BEFORE anything is sent. A message
    that happened to be right after a connection attempt would still be a connection attempt."""
    transport, seen = ok_transport()
    out = logexport.send_test_batch(db, endpoint="http://collector.internal",
                                    transport=transport)
    assert out["error"] == "no_port"
    assert seen == []


def test_a_reachable_collector_reports_records_and_latency(db, client, auth):
    transport, seen = ok_transport()
    _enable(db)
    _event(db, "create_item", project_id=db.scalar(select(Event.project_id)))
    out = logexport.send_test_batch(db, transport=transport)
    assert out["ok"] is True
    assert out["records"] >= 1
    assert out["attempted"] >= 1
    assert isinstance(out["latency_ms"], int) and out["latency_ms"] >= 0
    assert str(out["records"]) in out["detail"]
    assert "ms" in out["detail"]
    assert seen and seen[0].url.path.endswith("/v1/logs")

    r = client.post(f"{PANEL}/test-batch", json={}, headers=auth)
    assert r.status_code == 200


def test_an_unrun_probe_does_not_look_like_a_passed_one(db, client, auth):
    """`last_batch_state == "never"` is a third answer, distinct from a batch that ran and sent
    nothing and from a read that failed."""
    body = client.get(PANEL, headers=auth).json()
    assert body["status"]["last_batch"] is None
    assert body["status"]["last_batch_state"] == "never"
    assert body["status"]["sent_24h"] == 0


def test_a_failed_probe_is_recorded_as_the_last_batch(db, client, auth):
    client.post(f"{PANEL}/test-batch", json={"endpoint": "http://collector.internal"},
                headers=auth)
    body = client.get(PANEL, headers=auth).json()
    last = body["status"]["last_batch"]
    assert body["status"]["last_batch_state"] == "measured"
    assert last is not None and last["ok"] is False
    assert last["kind"] == "test"
    assert last["error"] == "no_port"


def test_a_probe_is_not_this_deployments_telemetry(db, client, auth):
    """`sent 24h` counts exported records. Folding a test batch in would make the strip's number
    go up every time the operator pressed a button to check their typing."""
    transport, _seen = ok_transport()
    _enable(db)
    logexport.send_test_batch(db, transport=transport)
    body = client.get(PANEL, headers=auth).json()
    assert body["status"]["sent_24h"] == 0
    assert body["status"]["last_batch"]["kind"] == "test"


def test_a_collector_that_answers_500_is_named(db):
    transport, _seen = ok_transport(status=503, payload={})
    _enable(db)
    out = logexport.send_test_batch(db, transport=transport)
    assert out["ok"] is False
    assert out["error"] == "bad_status"
    assert "503" in out["detail"]


def test_a_collector_that_is_down_is_unreachable_not_no_port(db):
    _enable(db)
    out = logexport.send_test_batch(db, transport=down_transport())
    assert out["ok"] is False
    assert out["error"] == "unreachable"
    assert out["error"] != logexport.NO_PORT_ERROR


def test_partial_success_is_not_counted_as_sent(db):
    """A collector that kept 1 of 3 records says so in `partialSuccess`. Reporting 3 would make
    the strip's number higher than what the collector holds."""
    transport, _seen = ok_transport(payload={"partialSuccess": {"rejectedRecords": "2"}})
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    for i in range(3):
        _event(db, f"create_item_{i}")
    accepted, _latency = logexport.send_records(
        logexport._probe_records(db, cfg, logexport.Redaction.of(cfg), limit=3),
        endpoint=ENDPOINT, protocol="http/protobuf", compression="none", transport=transport)
    assert accepted == 1


def test_grpc_is_refused_with_a_reason_not_silently_downgraded(db, client, auth):
    """The design offers gRPC. This build cannot speak it, so the answer names that instead of
    quietly posting over HTTP and reporting success."""
    support = {p["id"]: p for p in logexport.protocol_support()}
    assert support["grpc"]["supported"] is False
    assert support["http/protobuf"]["supported"] is True

    transport, seen = ok_transport()
    out = logexport.send_test_batch(db, endpoint=ENDPOINT, protocol="grpc", transport=transport)
    assert out["ok"] is False
    assert out["error"] == "unsupported"
    assert "grpcio" in out["detail"]
    assert seen == [], "a refused protocol must not send anything"

    body = client.get(PANEL, headers=auth).json()
    assert {p["id"] for p in body["protocols"]} == set(logexport.PROTOCOLS)


def test_the_payload_is_otlp_and_gzip_is_applied(db):
    transport, seen = ok_transport()
    _enable(db, compression="gzip")
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    records = logexport._probe_records(db, cfg, logexport.Redaction.of(cfg), limit=1)
    logexport.send_records(records, endpoint=ENDPOINT, protocol="http/protobuf",
                           compression="gzip", transport=transport)
    req = seen[0]
    assert req.headers["content-encoding"] == "gzip"
    import gzip
    import json

    body = json.loads(gzip.decompress(req.content))
    assert "resourceLogs" in body
    record = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert {"timeUnixNano", "severityText", "body", "attributes"} <= set(record)


def test_an_operator_header_reaches_the_collector_but_content_type_does_not_get_clobbered(db):
    transport, seen = ok_transport()
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    records = logexport._probe_records(db, cfg, logexport.Redaction.of(cfg), limit=1)
    logexport.send_records(records, endpoint=ENDPOINT, protocol="http/protobuf",
                           compression="none", transport=transport,
                           headers={"X-Scope": "org-1", "Content-Type": "text/plain"})
    req = seen[0]
    assert req.headers["x-scope"] == "org-1"
    assert req.headers["content-type"] == "application/json"


# ── the state floor ──────────────────────────────────────────────────────────

def test_counters_are_measured_zeros_before_anything_is_sent(db):
    """The baseline the sabotage tests below are measured against: a working read of an empty
    batch table is a real zero, not an unknown."""
    _enable(db)
    good = logexport.status(db)
    assert good["coverage"] == "full"
    assert good["sent_24h"] == 0
    assert good["dropped_24h"] == 0
    assert good["note"] == ""


def test_a_failed_queue_read_leaves_the_counters_it_did_read(db, monkeypatch):
    """One guard around the whole strip would blank the 24h counters too, and the panel would
    report less than it knows. Each read fails on its own."""
    _enable(db)
    transport, _seen = ok_transport()
    logexport.drain_once(db, transport=transport)

    def explode(*_a, **_k):
        raise RuntimeError("counter table unavailable")

    monkeypatch.setattr(logexport, "pending", explode)
    monkeypatch.setattr(logexport, "exporter_running", lambda: True)
    out = logexport.status(db)
    assert out["queue_depth"] is None
    assert out["queue_state"] == "unavailable"
    assert out["coverage"] == "unavailable"
    assert "queue depth" in out["note"]
    assert isinstance(out["sent_24h"], int), "a failed queue read must not blank a good counter"


def test_a_failed_counter_read_is_unknown_not_a_zero(db, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("counter table unavailable")

    _enable(db)
    monkeypatch.setattr(logexport, "_window_totals", explode)
    out = logexport.status(db)
    assert out["sent_24h"] is None
    assert out["dropped_24h"] is None
    assert out["coverage"] == "unavailable"
    assert "24h counters" in out["note"]
    assert "not a zero" in out["note"]


def test_a_failed_last_batch_read_is_unavailable_not_never(db, monkeypatch):
    """"We could not read it" must not arrive as "nothing has ever been sent" — the second is a
    clean result and the first is an absence of one."""
    def explode(*_a, **_k):
        raise RuntimeError("counter table unavailable")

    _enable(db)
    monkeypatch.setattr(logexport, "_last_batch", explode)
    out = logexport.status(db)
    assert out["last_batch"] is None
    assert out["last_batch_state"] == "unavailable"
    assert out["last_batch_state"] != "never"
    assert out["coverage"] == "unavailable"


def test_the_router_passes_an_unavailable_counter_through_rather_than_coalescing_it(
        db, client, auth, monkeypatch):
    """THE CALL, not only the callee. A `or 0` in the router — or a `?? 0` in the panel — is the
    whole defect, and it is invisible from the service's own tests."""
    def explode(*_a, **_k):
        raise RuntimeError("counter table unavailable")

    _enable(db)
    monkeypatch.setattr(logexport, "_window_totals", explode)
    body = client.get(PANEL, headers=auth).json()
    assert body["status"]["sent_24h"] is None
    assert body["status"]["dropped_24h"] is None
    assert body["status"]["coverage"] == "unavailable"
    assert body["status"]["note"]


def test_queue_depth_is_unknown_when_the_exporter_is_not_running(db, client, auth):
    """The item's named case. A backlog of zero beside a dead exporter reads as "all clear"."""
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")

    logexport.mark_exporter_stopped()
    try:
        out = logexport.status(db)
        assert out["state"] == "not_running"
        assert out["queue_depth"] is None
        assert out["queue_state"] == "exporter_not_running"
        assert out["state_note"], "on-but-inert must say so"
        # And the real backlog is not zero — which is exactly why reporting 0 would be a lie.
        assert logexport.pending(db)["total"] > 0

        body = client.get(PANEL, headers=auth).json()
        assert body["status"]["queue_depth"] is None
    finally:
        logexport.mark_exporter_stopped()

    logexport.mark_exporter_running()
    try:
        out = logexport.status(db)
        assert out["state"] == "exporting"
        assert out["queue_state"] == "draining"
        assert isinstance(out["queue_depth"], int) and out["queue_depth"] > 0
    finally:
        logexport.mark_exporter_stopped()


def test_a_failed_config_read_is_unknown_not_off(db, monkeypatch):
    """"Off" and "we could not read it" are different states, and off is the reassuring one."""

    def explode(*_a, **_k):
        raise RuntimeError("no database")

    monkeypatch.setattr(logexport, "get_config", explode)
    out = logexport.status(db=None)
    assert out["state"] == "unknown"
    assert out["enabled"] is None, "an unread config must not claim to be disabled"
    assert out["coverage"] == "unavailable"
    assert out["last_batch_state"] == "unavailable"
    assert "not 'off'" in out["note"]


def test_paused_not_running_and_unknown_are_three_states(db):
    _enable(db)
    logexport.mark_exporter_stopped()
    assert logexport.status(db)["state"] == "not_running"

    logexport.mark_exporter_running()
    try:
        assert logexport.status(db)["state"] == "exporting"
    finally:
        logexport.mark_exporter_stopped()

    cfg = logexport.get_config(db)
    cfg.enabled = False
    db.commit()
    out = logexport.status(db)
    assert out["state"] == "paused"
    assert out["enabled"] is False
    assert "off" in out["state_note"]


def test_the_paused_note_reports_retention_it_can_read(db):
    """The design says events are kept for 90 days. Nothing sweeps `events` and `agent_calls`
    are swept at AGENT_CALL_RETENTION_DAYS, so the number is read, not typed."""
    from app.config import settings

    note = logexport.retention_note()
    assert str(settings.agent_call_retention_days) in note
    assert "90 day" not in note


# ── the exporter ─────────────────────────────────────────────────────────────

def test_a_drain_sends_and_advances_the_cursor(db):
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    before = int(db.scalar(select(func.count(Event.id))))
    assert before > 0
    transport, seen = ok_transport()

    out = logexport.drain_once(db, transport=transport)
    assert out["ran"] is True and out["ok"] is True
    assert out["sent"] == before
    assert seen, "nothing was posted"
    db.expire_all()
    assert logexport.get_config(db).cursor_event_id == int(
        db.scalar(select(func.max(Event.id))))
    assert logexport.pending(db)["logs"] == 0

    row = db.scalars(select(LogExportBatch).order_by(LogExportBatch.id.desc())).first()
    assert row.kind == "export" and row.ok is True and row.sent == before
    assert logexport.status(db)["sent_24h"] == before


def test_a_paused_exporter_does_nothing_and_says_why(db):
    """`ran: False, reason: paused` is not `sent: 0`. The first says the exporter is not
    exporting; the second says it exported and there was nothing to send."""
    logexport.get_config(db)
    _event(db, "create_item")
    transport, seen = ok_transport()
    out = logexport.drain_once(db, transport=transport)
    assert out == {"ran": False, "reason": "paused", "sent": 0, "dropped": 0}
    assert seen == []
    assert db.scalar(select(func.count(LogExportBatch.id))) == 0


def test_an_empty_drain_writes_no_batch_row(db):
    _enable(db)
    transport, _seen = ok_transport()
    out = logexport.drain_once(db, transport=transport)
    assert out["ran"] is True and out["reason"] == "empty"
    assert db.scalar(select(func.count(LogExportBatch.id))) == 0, (
        "an empty pass every interval would bury last_batch under batches that sent nothing")


def test_a_down_collector_keeps_the_records_and_then_counts_what_it_gives_up_on(db):
    """Unbounded retry is an unbounded queue; giving up silently is a zero that means nothing.
    The backlog is dropped after a bounded streak and COUNTED, so `dropped 24h` can be a real
    non-zero measurement rather than a column nobody ever writes."""
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    down = down_transport()

    for _ in range(logexport.MAX_CONSECUTIVE_FAILURES):
        out = logexport.drain_once(db, transport=down)
        assert out["sent"] == 0
        assert logexport.pending(db)["total"] > 0, "a failed batch must not lose the records"
    assert logexport.failure_streak(db) == logexport.MAX_CONSECUTIVE_FAILURES

    out = logexport.drain_once(db, transport=down)
    assert out["reason"] == "backlog_dropped"
    assert out["dropped"] > 0
    assert logexport.pending(db)["total"] == 0
    assert logexport.status(db)["dropped_24h"] == out["dropped"]


def test_a_give_up_does_not_stop_the_exporter_from_trying_again(db):
    """The `backlog_dropped` row is a marker, not another failed send.

    Counting it would pin the streak at the ceiling forever, so every later drain would drop its
    backlog on sight and the collector would never be asked again — a permanent silent loss
    wearing the "counted, not kept" receipt that was supposed to prevent it.
    """
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    down = down_transport()

    for _ in range(logexport.MAX_CONSECUTIVE_FAILURES):
        logexport.drain_once(db, transport=down)
    assert logexport.drain_once(db, transport=down)["reason"] == "backlog_dropped"
    assert logexport.failure_streak(db) == 0, "the give-up marker was counted as a failure"

    # The collector comes back. The next drain must SEND rather than drop on sight.
    _event(db, "after_recovery")
    transport, seen = ok_transport()
    out = logexport.drain_once(db, transport=transport)
    assert seen, "the exporter never asked the collector again"
    assert out["ok"] is True
    assert out["sent"] > 0
    assert out["reason"] != "backlog_dropped"


def test_a_failing_signal_does_not_lose_the_one_that_succeeded(db, decoy):
    """Per-signal cursors: one collector path being broken must not stall the others, and must
    not re-send what already landed."""
    from tests.decoy import assert_populated

    assert_populated(db, decoy)  # the decoy's agent is the heartbeat this test needs
    _enable(db, send_heartbeats=True)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    cfg.cursor_heartbeat_at = None
    db.commit()
    _event(db, "create_item")
    assert db.scalar(select(func.count(Agent.id))) > 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/metrics"):
            raise httpx.ConnectError("metrics port closed", request=request)
        return httpx.Response(200, json={})

    out = logexport.drain_once(db, transport=httpx.MockTransport(handler))
    assert out["ok"] is False
    assert out["sent"] > 0
    db.expire_all()
    cfg = logexport.get_config(db)
    assert cfg.cursor_event_id > 0, "the signal that landed must advance"
    assert cfg.cursor_heartbeat_at is None, "the signal that failed must not"


def test_enabling_starts_from_now_rather_than_back_filling_the_ledger(db, client, auth):
    """Turning export on must not empty the whole history into a collector the operator has just
    pointed at it. Stated in the response, so the panel can say it rather than leave the operator
    wondering where the flood came from."""
    project_id = _project(db)
    for i in range(3):
        _event(db, f"create_item_{i}", project_id=project_id)
    newest_before = int(db.scalar(select(func.max(Event.id))))
    assert newest_before >= 3

    r = client.patch(PANEL, json={"enabled": True, "endpoint": ENDPOINT}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["notes"]["catch_up"] == "from_now"
    assert body["notes"]["catch_up_note"]

    # The PATCH ran in the app's session; without this the read below answers from this
    # session's identity map and passes whether or not the cursor moved.
    db.expire_all()
    cfg = logexport.get_config(db)
    assert cfg.cursor_event_id == newest_before, "the existing ledger was back-filled"
    # The only rows past the cursor are the audit events this very save wrote.
    assert logexport.pending(db)["logs"] <= 2

    # A re-enable is not a first enable: it must not jump the cursor forward again, or
    # everything written while export was off is skipped and never counted as dropped.
    client.patch(PANEL, json={"enabled": False}, headers=auth)
    _event(db, "written_while_off", project_id=project_id)
    client.patch(PANEL, json={"enabled": True}, headers=auth)
    db.expire_all()
    cfg = logexport.get_config(db)
    assert cfg.cursor_event_id == newest_before, "a re-enable moved the cursor and skipped rows"
    assert logexport.pending(db)["logs"] > 0


def test_the_event_type_filter_narrows_the_export(db):
    _enable(db, event_types=["create_item"])
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")
    _event(db, "revoke_api_key")
    assert logexport.pending(db)["logs"] == 1
    groups, _marks = logexport._collect(db, cfg, logexport.Redaction.of(cfg), limit=50)
    assert [r["body"] for r in groups["log"]] == ["create_item"]


def test_a_refusal_exports_as_warn(db):
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, events_svc.REFUSAL_ACTIONS[0])
    groups, _m = logexport._collect(db, cfg, logexport.Redaction.of(cfg), limit=10)
    assert groups["log"][0]["severity"] == "WARN"


def test_heartbeats_export_as_metrics_naming_their_project(db, decoy):
    from tests.decoy import assert_populated

    assert_populated(db, decoy)
    _enable(db, send_heartbeats=True, send_events=False, send_tool_calls=False)
    cfg = logexport.get_config(db)
    cfg.cursor_heartbeat_at = None
    db.commit()
    groups, _m = logexport._collect(db, cfg, logexport.Redaction.of(cfg), limit=50)
    assert groups["metric"], "no heartbeat metric records were built"
    assert groups["log"] == [] and groups["trace"] == []
    one = groups["metric"][0]
    assert one["kind"] == "metric" and one["metric_name"] == "gb.agent.heartbeat"
    assert one["attributes"]["gb.scope"] in ("project", "deployment")
    payload = logexport.otlp_payload(groups["metric"])
    assert "resourceMetrics" in payload


# ── redaction ────────────────────────────────────────────────────────────────

def _sample(db, **flags):
    cfg = logexport.get_config(db)
    return logexport.sample_record(db, logexport.Redaction.of(cfg, **flags))


def test_each_redaction_choice_changes_exactly_what_it_says(db):
    """Three independent toggles. Each is asserted both ways, so a flag wired to the wrong field
    fails on the pair rather than passing on one side."""
    project_id = _project(db)
    _event(db, "create_item", project_id=project_id, actor_type="apikey", actor_label="loop",
           surface="mcp", meta={"summary": "rewrote the retry budget",
                                "client_ip": "203.0.113.7"})

    masked = _sample(db, summaries=True, client_ips=True, api_keys=True)["attributes"]
    assert masked["gb.summary"] == "[redacted]"
    assert "gb.client_ip" not in masked, "a dropped IP must be absent, not a placeholder"
    assert masked["gb.api_key"] == logexport.MASK_SENTINEL

    open_ = _sample(db, summaries=False, client_ips=False, api_keys=False)["attributes"]
    assert open_["gb.summary"] == "rewrote the retry budget"
    assert open_["gb.client_ip"] == "203.0.113.7"
    # The record still names its project with every redaction on: that is the point of naming
    # it on the record rather than scoping the config.
    assert masked["gb.project"] == open_["gb.project"]


def test_scrub_runs_whatever_the_toggles_say(db):
    """A collector is off-box, so the existing write-path redactor is not optional. A secret in
    free text leaves masked even with every redaction toggle OFF — otherwise "turn off
    redaction" would mean "send your credentials"."""
    project_id = _project(db)
    _event(db, "create_item", project_id=project_id,
           meta={"summary": "token was sk-abcdefghijklmnop1234 for the retry loop"})
    attrs = _sample(db, summaries=False, client_ips=False, api_keys=False)["attributes"]
    assert "sk-abcdefghijklmnop1234" not in attrs["gb.summary"]
    assert "[redacted:key]" in attrs["gb.summary"]


def test_the_sample_from_an_empty_ledger_says_it_is_not_your_data(db):
    db.execute(Event.__table__.delete())
    db.commit()
    out = _sample(db, summaries=True, client_ips=True, api_keys=True)
    assert out["source"] == "synthetic"
    assert out["note"], "an invented record presented as a real one is the panel fabricating"
    # The synthetic set exists so each toggle has something to act on before your data does.
    assert "gb.client_ip" not in out["attributes"]
    assert out["attributes"]["gb.summary"] == "[redacted]"
    assert out["attributes"]["gb.api_key"] == logexport.MASK_SENTINEL


def test_the_sample_follows_the_stored_config_when_no_flag_is_given(db):
    project_id = _project(db)
    _event(db, "create_item", project_id=project_id, meta={"summary": "keep me"})
    cfg = logexport.get_config(db)
    cfg.redact_summaries = False
    db.commit()
    assert logexport.sample_record(db, logexport.Redaction.of(cfg))["attributes"][
        "gb.summary"] == "keep me"


def test_the_sample_endpoint_recomputes_from_unsaved_choices(db, client, auth):
    """THE CALL: the panel's live sample must be able to ask about flags it has not saved. If the
    router dropped the query parameters the sample would quietly keep following the stored config
    and every toggle would look inert."""
    project_id = _project(db)
    _event(db, "create_item", project_id=project_id, meta={"summary": "a real summary"})
    cfg = logexport.get_config(db)
    cfg.redact_summaries = False
    db.commit()

    r = client.get(f"{PANEL}/sample", params={"redact_summaries": "true"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["redaction"]["summaries"] is True
    assert body["sample"]["attributes"]["gb.summary"] == "[redacted]"

    r = client.get(f"{PANEL}/sample", params={"redact_summaries": "false"}, headers=auth)
    assert r.json()["sample"]["attributes"]["gb.summary"] == "a real summary"


# ── config surface ───────────────────────────────────────────────────────────

def test_headers_round_trip_masked_without_wiping_the_secret(db, client, auth):
    secret = "Bearer a-very-long-collector-token"
    r = client.patch(PANEL, json={"headers": {"Authorization": secret}}, headers=auth)
    assert r.status_code == 200
    shown = r.json()["config"]["headers"]
    assert shown == [{"name": "Authorization", "value": logexport.mask_secret(secret)}]
    assert secret not in r.text

    # Sending the mask back means "unchanged" — the panel round-trips what it was given.
    r = client.patch(PANEL, json={"headers": {"Authorization": shown[0]["value"]}}, headers=auth)
    assert r.status_code == 200
    assert logexport.get_config(db).headers["Authorization"] == secret

    # A new value replaces; an absent name is deleted, not silently kept.
    client.patch(PANEL, json={"headers": {"Authorization": "Bearer something-else-entirely"}},
               headers=auth)
    assert logexport.get_config(db).headers["Authorization"] == "Bearer something-else-entirely"
    client.patch(PANEL, json={"headers": {}}, headers=auth)
    assert logexport.get_config(db).headers == {}


def test_a_short_secret_is_masked_whole(db):
    """`abc1…cdef` out of an eight-character token leaves enough to guess the rest."""
    assert logexport.mask_secret("abcdef12") == logexport.MASK_SENTINEL
    assert logexport.mask_secret("") == ""
    assert logexport.mask_secret("abcdefghijkl") == "abcd…ijkl"


def test_an_unknown_field_is_refused_not_ignored(db):
    """A silently dropped key is a save that reports success and changes nothing."""
    with pytest.raises(logexport.BadConfig):
        logexport.update_config(db, project_id="other")
    with pytest.raises(logexport.BadConfig):
        logexport.update_config(db, cursor_event_id=0)


def test_an_unknown_protocol_or_compression_is_refused(db, client, auth):
    assert client.patch(PANEL, json={"protocol": "thrift"}, headers=auth).status_code == 422
    assert client.patch(PANEL, json={"compression": "zstd"}, headers=auth).status_code == 422
    assert client.patch(PANEL, json={"protocol": "grpc"}, headers=auth).status_code == 200


def test_saving_cannot_move_the_cursor(db, client, auth):
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 4242
    db.commit()
    client.patch(PANEL, json={"endpoint": "http://other.example:4318"}, headers=auth)
    # Read past this session's identity map: a stale row would assert 4242 whether or not the
    # PATCH moved it, which is a test that cannot fail.
    db.expire_all()
    assert logexport.get_config(db).cursor_event_id == 4242


def test_event_type_options_are_measured_from_the_ledger(db, client, auth):
    _event(db, "a_distinctive_action_for_options")
    body = client.get(PANEL, headers=auth).json()
    assert "a_distinctive_action_for_options" in body["event_type_options"]


# ── gating ───────────────────────────────────────────────────────────────────

def test_the_panel_needs_an_operator(client):
    assert client.get(PANEL).status_code == 401
    assert client.patch(PANEL, json={}).status_code == 401
    assert client.post(f"{PANEL}/test-batch", json={}).status_code == 401
    assert client.get(f"{PANEL}/sample").status_code == 401


def test_hosted_refuses_rather_than_letting_a_tenant_move_the_boxes_telemetry(client, auth,
                                                                             monkeypatch):
    from app.routers import settings as settings_routes

    monkeypatch.setattr(settings_routes.settings, "hosted_mode", True)
    r = client.get(PANEL, headers=auth)
    assert r.status_code == 403
    assert settings_routes.HOSTED_REFUSED in r.json()["detail"]
    assert client.patch(PANEL, json={"enabled": True}, headers=auth).status_code == 403
    assert client.post(f"{PANEL}/test-batch", json={}, headers=auth).status_code == 403


def test_a_write_is_recorded_in_the_audit_ledger(db, client, auth):
    _enable(db)
    before = int(db.scalar(select(func.count(Event.id))
                             .where(Event.action == "update_log_export")))
    client.patch(PANEL, json={"endpoint": "http://collector.example:4318"}, headers=auth)
    after = db.scalar(select(func.count(Event.id)).where(Event.action == "update_log_export"))
    assert after == before + 1
    row = db.scalars(select(Event).where(Event.action == "update_log_export")
                     .order_by(Event.id.desc())).first()
    assert row.meta["fields"] == ["endpoint"]
    assert row.target_id == logexport.SINGLETON_ID


def test_run_once_never_raises_into_the_loop(db, monkeypatch):
    """A collector being down must not take the API process with it, and a task that dies
    silently leaves `enabled` saying something the box is not doing."""
    _enable(db)
    cfg = logexport.get_config(db)
    cfg.cursor_event_id = 0
    db.commit()
    _event(db, "create_item")

    def explode(*_a, **_k):
        raise RuntimeError("collector on fire")

    monkeypatch.setattr(logexport, "drain_once", explode)
    assert logexport.run_once(db) == 0


def test_the_loop_marks_the_exporter_running_and_stops_claiming_when_it_ends(db, monkeypatch):
    """`queue depth` is reported only while the drain is alive, and the flag is cleared in a
    `finally` — an exporter that died and left 0 on the panel is the defect this item is about."""
    import asyncio

    from app import main as main_mod

    monkeypatch.setattr(main_mod.settings, "log_export_seconds", 0)
    passes: list[int] = []

    def one_pass() -> int:
        passes.append(1)
        raise RuntimeError("collector on fire")

    monkeypatch.setattr(main_mod, "_one_log_export_pass", one_pass)
    logexport.mark_exporter_stopped()

    async def drive():
        task = asyncio.create_task(main_mod._log_export_loop())
        await asyncio.sleep(0.05)
        assert logexport.exporter_running() is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert logexport.exporter_running() is False
    assert passes, "the loop never ran a pass"


def test_the_drain_is_disabled_for_the_suite_so_it_cannot_race_a_test():
    """The conftest knob, asserted rather than assumed: if it were ever left on, a background
    pass firing mid-test would turn an unrelated assertion into a flake."""
    from app.config import settings

    assert settings.log_export_seconds == 0
