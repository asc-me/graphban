"""The ratchet's own tests, plus the coverage it demands (GRPH-495).

The conformance check itself runs as a session hook (see `tests/schema_probe.py` and the
hooks in `conftest.py`) because the question — did any call anywhere in this run emit this
key — cannot be answered by one test in the middle of the run. What CAN be tested here is the
judging: that `report()` fails on drift, fails on absence, and passes only on real
conformance.

**A guard whose failure path is untested is a guard you are hoping about.** This one has three
ways to report a clean manifest while knowing nothing — no data, unreadable data, and a tool
nobody called — and each of them is asserted to fail rather than pass.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests import schema_probe


@pytest.fixture()
def probe_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(schema_probe.ENV_DIR, str(tmp_path))
    return tmp_path


def _write(directory: pathlib.Path, name: str, payload: dict) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _declared_keys(tool: str) -> list[str]:
    from app import mcp_server

    return sorted((mcp_server._OUTPUT_SCHEMAS[tool].get("properties") or {}))


# ---- the judging ----------------------------------------------------------------------


def test_a_declared_key_no_call_emitted_is_a_failure(probe_dir):
    """The defect this exists for: GRPH-485 declared `graded`, promised it in the tool
    description, and the handler returned six other keys for 23 calls."""
    keys = _declared_keys("answer_grill")
    _write(probe_dir, "1.json", {"answer_grill": [k for k in keys if k != "graded"]})

    failures = schema_probe.report(full_run=False)

    assert failures, "a missing declared key must fail"
    assert any("answer_grill" in line and "graded" in line for line in failures), failures


def test_a_tool_emitting_everything_it_declares_passes(probe_dir):
    """The control. Without it every assertion above is satisfied by a report() that always
    fails, which would be a ratchet nobody could ever land."""
    _write(probe_dir, "1.json", {"answer_grill": _declared_keys("answer_grill")})

    assert schema_probe.report(full_run=False) == []


def test_extra_keys_beyond_the_schema_are_not_a_failure(probe_dir):
    """Deliberately one-directional. Several tools gain envelope fields downstream via
    `_attach_directive`, and forbidding that is a different decision from the one this
    ratchet is about."""
    _write(probe_dir, "1.json",
           {"answer_grill": _declared_keys("answer_grill") + ["some_envelope_field"]})

    assert schema_probe.report(full_run=False) == []


# ---- failing closed -------------------------------------------------------------------


def test_no_probe_data_on_a_PARTIAL_run_is_not_a_failure(probe_dir):
    """Found by using it: `pytest tests/test_prd_sync.py` exited 1 on a green suite, because
    those tests make no MCP calls and the empty aggregate was failed unconditionally.

    A selection that records nothing has learned nothing — it has not learned that the
    manifest is clean. Only a FULL run can distinguish "the probe is broken" from "you ran
    two doc tests", and a ratchet that goes red on every subset run is one people switch off.
    """
    assert schema_probe.report(full_run=False) == []


def test_no_probe_data_at_all_is_a_failure_not_a_pass(probe_dir):
    """THE ONE THAT MATTERS MOST. If the probe fails to install, or no MCP call happens, the
    aggregate is empty — and an empty aggregate has no drift in it. Reporting that as a clean
    manifest is precisely the failure mode this whole ratchet exists to prevent, committed by
    the ratchet itself."""
    failures = schema_probe.report(full_run=True)

    assert failures, "an empty probe must fail; absence of evidence is not conformance"
    assert "recorded nothing" in failures[0]


def test_unreadable_probe_data_is_a_failure(probe_dir):
    """A truncated or half-written dump must not be silently skipped, for the same reason."""
    (probe_dir / "1.json").write_text("{not json", encoding="utf-8")

    failures = schema_probe.report(full_run=False)

    assert failures and "could not read" in failures[0]


def test_a_tool_nobody_called_fails_a_full_run(probe_dir):
    """An unexercised tool is how a wrong schema gets in: there is no call to disagree with
    it. Only checked on a full run — see `_is_full_run`."""
    _write(probe_dir, "1.json", {"answer_grill": _declared_keys("answer_grill")})

    failures = schema_probe.report(full_run=True)

    assert any("never exercised" in line for line in failures)


def test_a_tool_nobody_called_does_not_fail_a_partial_run(probe_dir):
    """Running one file must not fail because the other fifty tools did not happen. A ratchet
    that cries on every `pytest tests/test_api.py` is one people learn to ignore."""
    _write(probe_dir, "1.json", {"answer_grill": _declared_keys("answer_grill")})

    assert schema_probe.report(full_run=False) == []


def test_observations_from_several_workers_are_unioned(probe_dir):
    """Under `-n auto` no single worker sees every call. A key emitted only on gw3 counts."""
    keys = _declared_keys("answer_grill")
    _write(probe_dir, "1.json", {"answer_grill": keys[:2]})
    _write(probe_dir, "2.json", {"answer_grill": keys[2:]})

    assert schema_probe.report(full_run=False) == []


# ---- the coverage the ratchet demands --------------------------------------------------


def test_report_graphban_issue_emits_every_key_it_declares(client, auth, monkeypatch):
    """The tool the ratchet's first run flagged — as a FALSE POSITIVE, which is why the probe
    now resolves `TOOL_ALIASES`. It is exercised in test_upstream.py, but only under its old
    name `report_agentledger_issue`, and only for `ok`/`request_id`/`target`.

    `duplicates` was asserted by nothing, so the schema's fourth property was unverified even
    once the alias was accounted for. httpx is mocked exactly as test_upstream does it, so
    nothing leaves the process."""
    import app.services.upstream as up_svc

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"request": {"id": "R-7", "title": "x"}, "duplicates": []}

    monkeypatch.setattr(up_svc.httpx, "post", lambda *a, **k: _Resp())
    key = client.post("/api/api-keys", json={"name": "reporter"}, headers=auth).json()
    r = client.post("/api/mcp", headers={"X-API-Key": key["plaintext"]},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "report_graphban_issue",
                                     "arguments": {"title": "a tool refused a valid path",
                                                   "type": "bug", "detail": "steps here"}}})

    payload = r.json()["result"]["structuredContent"]
    assert set(_declared_keys("report_graphban_issue")) <= set(payload), payload


def test_a_call_through_an_alias_is_recorded_under_the_canonical_name(client, auth, monkeypatch):
    """Pins the alias resolution, which a sabotage proved was otherwise decorative.

    `_call_tool` resolves `TOOL_ALIASES` one line into its own body, so the probe wrapping it
    sees the name the CALLER used. `report_agentledger_issue` is exercised in test_upstream.py
    under that old name; without resolving it here the probe files those calls under a name no
    schema knows and reports the canonical tool as never exercised — a finding the guard
    manufactured about itself, which is how a guard gets switched off.
    """
    import app.services.upstream as up_svc

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"request": {"id": "R-9", "title": "x"}, "duplicates": []}

    monkeypatch.setattr(up_svc.httpx, "post", lambda *a, **k: _Resp())
    key = client.post("/api/api-keys", json={"name": "aliased"}, headers=auth).json()
    client.post("/api/mcp", headers={"X-API-Key": key["plaintext"]},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "report_agentledger_issue",
                                 "arguments": {"title": "via the old name"}}})

    assert "report_graphban_issue" in schema_probe._seen
    assert "report_agentledger_issue" not in schema_probe._seen, (
        "the probe filed the call under the alias; a tool reached only by its old name "
        "would read as never exercised"
    )


def test_answer_grill_emits_graded(client, auth, monkeypatch):
    """GRPH-485's missing half, which this ratchet found. The service already reported it;
    the tool built its dict by hand and dropped it."""
    from app.services import prds as prd_svc

    prd_id = client.post("/api/prds", json={"title": "Spec", "body": "## D1\n\nwork",
                                            "project_id": "core"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "relay"}, headers=auth).json()
    monkeypatch.setattr(prd_svc, "_classify_dimensions", lambda *a, **k: None)
    monkeypatch.setattr(prd_svc, "_grader_id", lambda *a, **k: "ollama")

    r = client.post("/api/mcp", headers={"X-API-Key": key["plaintext"]},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "answer_grill",
                                     "arguments": {"prd_id": prd_id, "answer": "an answer"}}})

    payload = r.json()["result"]["structuredContent"]
    assert payload["graded"] is False, payload
    assert "could not be asked" in payload["ungraded_reason"]
