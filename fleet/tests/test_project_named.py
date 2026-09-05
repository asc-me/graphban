"""GRPH-718 / GRPH-719: on a credential that spans projects, every call names its project.

Found by the PRD-36 criterion-18 check: the child registered on the seat's project (#619) but
its later reads landed on the key's default, and the supervisor polled a roster from that
default project, never saw its child, and reported it never_registered.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import httpx

from gbagent import cli as gbagent_cli
from gbfleet import doctor
from gbfleet.client import ALLOWED_TOOLS, Graphban

from conftest import telemetry_ack  # noqa: E402


def _recording(payloads: dict, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        ack = telemetry_ack(request)
        if ack is not None:
            return ack
        body = json.loads(request.content)
        name = body["params"]["name"]
        sent.append((name, dict(body["params"].get("arguments") or {})))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"structuredContent": payloads.get(name, {})}})
    return handler


def test_a_client_with_a_project_names_it_on_every_call_unless_the_caller_did():
    sent: list = []
    c = Graphban("http://gb.invalid", "k", transport=httpx.MockTransport(_recording({}, sent)),
                 allowed=frozenset({"fleet_status", "propose_allocation"}), project_id="agentledger")
    c.fleet_status()
    c.propose_allocation(project_id="elsewhere")
    assert sent == [("fleet_status", {"project_id": "agentledger"}),
                    ("propose_allocation", {"project_id": "elsewhere"})]


def test_a_client_without_a_project_sends_none_as_before():
    sent: list = []
    c = Graphban("http://gb.invalid", "k", transport=httpx.MockTransport(_recording({}, sent)))
    c.fleet_status()
    assert sent == [("fleet_status", {})]
    assert ALLOWED_TOOLS == frozenset({"fleet_status", "propose_allocation"}), "no widening rode along"


def test_gbagent_learns_its_project_at_registration():
    sent: list = []
    payloads = {"register_agent": {"agent_id": "GRPH-A7", "active_role": "worker", "project_id": "agentledger",
                                   "assigned": {"item": "GRPH-717", "state": "claimed"}}}
    client = Graphban("http://gb.invalid", "k", transport=httpx.MockTransport(_recording(payloads, sent)),
                      allowed=frozenset({"register_agent"}))
    agent_id, role, assigned, project = gbagent_cli.register(client, code="W", model="m", worktree="/w", branch="b")
    assert (agent_id, role, project) == ("GRPH-A7", "worker", "agentledger")
    assert assigned["state"] == "claimed" and assigned["item"] == "GRPH-717"


def _doctor_report(readable: list[str], default: str, project: str, monkeypatch) -> doctor.Report:
    sent: list = []
    payloads = {"get_context": {"project_id": default, "readable_projects": readable}}

    real = Graphban.__init__

    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(_recording(payloads, sent))
        real(self, *a, **kw)
    monkeypatch.setattr(Graphban, "__init__", patched)
    report = doctor.Report()
    doctor.check_project(report, "http://gb.invalid", "k", project)
    return report


def _finding(report: doctor.Report, name: str) -> doctor.Finding:
    return next(f for f in report.findings if f.name == name)


def test_doctor_fails_a_multi_project_key_with_no_project_named(monkeypatch):
    r = _doctor_report(["fleet-walk", "agentledger"], "fleet-walk", "", monkeypatch)
    f = _finding(r, "project")
    assert f.status == doctor.FAIL and "fleet-walk" in f.detail and "--project" in f.remedy


def test_doctor_passes_a_named_readable_project(monkeypatch):
    assert _finding(_doctor_report(["fleet-walk", "agentledger"], "fleet-walk", "agentledger", monkeypatch), "project").status == doctor.PASS


def test_doctor_passes_a_single_project_key_without_a_flag(monkeypatch):
    # One report per test: the monkeypatch stacks otherwise and the first server answers both.
    assert _finding(_doctor_report(["agentledger"], "agentledger", "", monkeypatch), "project").status == doctor.PASS


def test_doctor_fails_a_project_the_key_cannot_read(monkeypatch):
    f = _finding(_doctor_report(["fleet-walk"], "fleet-walk", "agentledger", monkeypatch), "project")
    assert f.status == doctor.FAIL and "not readable" in f.detail
