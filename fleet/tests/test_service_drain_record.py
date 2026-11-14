"""GRPH-1649: a service drain tells the server it is one, and how often it restarts.

Between cycles no agent is present, and an answered wait used to say "No drain is running"
while launchd was about to restart the drain. The server can only say "between runs" from a
record — so the unit names the service and its interval, `until` declares both on its own
`register_agent`, and stamps presence once more as it exits.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import service, until  # noqa: E402
from gbfleet.client import ToolFailed  # noqa: E402

BINARY = "/opt/fake/bin/gbfleet"


def _plan(repo: Path, kind: str, every: int = 900):
    return service.make_plan(
        ["--repo", str(repo), "--server", "http://gb.invalid", "--project", "p"],
        kind=kind, binary=BINARY, name="P75W-P1", every=every)


@pytest.mark.parametrize("kind", ["systemd", "launchd", "schtasks"])
def test_every_unit_names_the_service_and_its_interval(git_repo: Path, kind):
    plan = _plan(git_repo, kind)
    if kind == "launchd":
        env = plistlib.loads(service.render(plan))["EnvironmentVariables"]
        assert env[service.SERVICE_NAME_ENV] == "P75W-P1"
        assert env[service.SERVICE_EVERY_ENV] == "900"
    elif kind == "systemd":
        text = service.render(plan).decode()
        assert f"Environment={service.SERVICE_NAME_ENV}=P75W-P1\n" in text
        assert f"Environment={service.SERVICE_EVERY_ENV}=900\n" in text
    else:
        runner = service._render_schtasks_runner(plan)
        assert f'set "{service.SERVICE_NAME_ENV}=P75W-P1"' in runner
        assert f'set "{service.SERVICE_EVERY_ENV}=900"' in runner
        # Set BEFORE until runs, or it reads nothing.
        assert runner.index(service.SERVICE_EVERY_ENV) < runner.index(" until ")
    # The variables `until` reads are the ones the unit sets.
    assert service.SERVICE_NAME_ENV == until._SERVICE_NAME_ENV
    assert service.SERVICE_EVERY_ENV == until._SERVICE_EVERY_ENV


def test_service_drain_reads_only_a_complete_positive_record():
    env = {until._SERVICE_NAME_ENV: "P75W-P1", until._SERVICE_EVERY_ENV: "900"}
    assert until.service_drain("P75W-P1", env) == {
        "service": "P75W-P1", "every": 900, "prd": "P75W-P1"}
    assert until.service_drain(None, env)["prd"] is None
    assert until.service_drain(None, {}) is None, "a one-off until declares nothing"
    assert until.service_drain(None, {until._SERVICE_NAME_ENV: "x"}) is None
    for bad in ("0", "-5", "abc", ""):
        assert until.service_drain(None, {until._SERVICE_NAME_ENV: "x",
                                          until._SERVICE_EVERY_ENV: bad}) is None


class _Planner:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def call(self, tool, **kw):
        self.calls.append((tool, kw))
        if self.fail:
            raise ToolFailed(tool, "internal", "down")
        if tool == "register_agent":
            return {"agent_id": "GRPH-A9", "active_role": "planner",
                    "eligible_roles": ["planner", "worker"], "tools_off_limits": []}
        return {}


def test_a_service_declares_its_drain_record_at_register(git_repo: Path, monkeypatch):
    monkeypatch.setenv(until._SERVICE_NAME_ENV, "P75W-P1")
    monkeypatch.setenv(until._SERVICE_EVERY_ENV, "900")
    planner = _Planner()
    until._identify(planner, git_repo, "claude", None, "P75W-P1")
    tool, kw = planner.calls[0]
    assert tool == "register_agent"
    assert kw["capabilities"] == {"drain": {"service": "P75W-P1", "every": 900,
                                            "prd": "P75W-P1"}}


def test_a_one_off_until_declares_no_drain_record(git_repo: Path, monkeypatch):
    monkeypatch.delenv(until._SERVICE_NAME_ENV, raising=False)
    monkeypatch.delenv(until._SERVICE_EVERY_ENV, raising=False)
    planner = _Planner()
    until._identify(planner, git_repo, "claude", None, "P75W-P1")
    assert "capabilities" not in planner.calls[0][1]


class _Wave:
    planner_agent_id = "GRPH-A9"


def test_a_service_stamps_presence_on_exit_and_a_one_off_does_not(monkeypatch):
    monkeypatch.setenv(until._SERVICE_NAME_ENV, "P75W-P1")
    monkeypatch.setenv(until._SERVICE_EVERY_ENV, "300")
    planner = _Planner()
    until._stamp_service_exit(planner, _Wave())
    assert planner.calls == [("fleet_status", {"agent_id": "GRPH-A9"})]

    # A lost stamp is silence, never a crash of the exit path.
    until._stamp_service_exit(_Planner(fail=True), _Wave())

    monkeypatch.delenv(until._SERVICE_NAME_ENV)
    quiet = _Planner()
    until._stamp_service_exit(quiet, _Wave())
    assert quiet.calls == []
