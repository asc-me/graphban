"""`gban code map service` — hourly refresh and nightly reconcile as a user unit (GRPH-1821)."""
from __future__ import annotations

import plistlib
from datetime import datetime

import pytest
from test_codemap import _git
from test_codemap_since import PinnedGraph, _origin

from gban import codemap, config
from gban import codemap_service as svc
from gban.cli import main
from gban.client import NoSession, Refused, Unreachable

KEY = "gb_sk_supersecretkey123456"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("GRAPHBAN_HOME", str(h / ".graphban"))
    calls = []
    monkeypatch.setattr(svc, "_run", lambda cmd, timeout=20.0: (calls.append(cmd) or (0, "")))
    return h, calls


def _plan(work, kind, **kw):
    return svc.make_plan(work, url="http://srv", project="p1", kind=kind,
                         path_env="/usr/bin:/bin", gban=["/opt/gban"], **kw)


def test_launchd_unit_has_both_schedules_and_no_key(tmp_path, home):
    work = _origin(tmp_path)
    plan = _plan(work, "launchd", hourly=":30", nightly="02:15")
    assert svc.install(plan, KEY) == []
    raw = plan.units[0].read_bytes()
    job = plistlib.loads(raw)
    assert job["StartCalendarInterval"] == [{"Minute": 30}, {"Hour": 2, "Minute": 15}]
    assert str(plan.env_file) in job["ProgramArguments"][2]
    assert KEY.encode() not in raw
    assert "code map service run" in job["ProgramArguments"][2]
    assert plan.units[0].is_relative_to(home[0])
    assert config.is_private(plan.env_file)
    assert KEY in plan.env_file.read_text()


def test_systemd_units_have_both_schedules_and_no_key(tmp_path, home):
    work = _origin(tmp_path)
    plan = _plan(work, "systemd")
    svc.install(plan, KEY)
    service, timer = (p.read_text() for p in plan.units)
    assert f"EnvironmentFile={plan.env_file}" in service
    assert KEY not in service and KEY not in timer
    assert "OnCalendar=*-*-* *:17:00" in timer
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert config.is_private(plan.env_file)
    assert ["systemctl", "--user", "enable", "--now", f"gban-codemap-{plan.sid}.timer"] in home[1]


def test_empty_path_refused(tmp_path, home):
    work = _origin(tmp_path)
    with pytest.raises(svc.Refused, match="PATH"):
        svc.make_plan(work, url="http://srv", project="p", kind="systemd", path_env="  ")


def test_round_trip_cli_and_one_unit_per_repo(tmp_path, home, monkeypatch, capsys):
    work = _origin(tmp_path)
    monkeypatch.setattr(svc, "host_kind", lambda: "systemd")
    monkeypatch.setenv("GBFLEET_API_KEY", KEY)
    base = ["--server", "http://srv", "--project", "p1", "code", "map", "service"]
    assert main([*base, "install", "--repo", str(work)]) == 0
    sid = svc.service_id(svc.repo_toplevel(work))
    assert svc.installed(sid, "systemd")
    capsys.readouterr()
    assert main([*base, "install", "--repo", str(work)]) == 0
    out = capsys.readouterr().out
    assert "already installed" in out and "never run" in out
    assert len(list((home[0] / ".config/systemd/user").glob("*.service"))) == 1
    assert main([*base, "uninstall", "--repo", str(work)]) == 0
    assert not svc.installed(sid, "systemd")
    assert not svc.env_path(sid).exists()


def test_status_never_run(tmp_path, home, monkeypatch):
    work = _origin(tmp_path)
    lines = svc.status_lines(work, "systemd")
    assert lines[0] == "never run"
    assert not any("current" in ln for ln in lines)


def test_run_ok_then_behind_after_origin_moves(tmp_path, home):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    sid = svc.service_id(work.resolve())
    rec = svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 10, 0))
    assert rec["outcome"] == "ok" and rec["mode"] == "full"
    sha = codemap.head(work)
    assert svc.status_lines(work, "systemd")[0].startswith("last run ok")
    assert "behind by 0 commits" in svc.status_lines(work, "systemd")[1]
    (work / "new.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "two")
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    lines = svc.status_lines(work, "systemd")
    assert "behind by 1 commits" in lines[1]
    rec = svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 11, 0))
    assert rec["mode"] == "since" and rec["outcome"] == "ok"
    assert rec["sha"] != sha
    assert "behind by 0 commits" in svc.status_lines(work, "systemd")[1]


def test_failed_fetch_is_failed_never_current(tmp_path, home):
    work = _origin(tmp_path)
    graph = PinnedGraph()
    sid = svc.service_id(work.resolve())
    svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 10, 0))
    (work / "n.py").write_text("y = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "two")
    _git(work, "push", "origin", "main")
    _git(work, "fetch", "origin")
    _git(work, "remote", "set-url", "origin", str(tmp_path / "nope.git"))
    rec = svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 11, 0))
    assert rec["outcome"] == "failed" and rec["stage"] == "fetch"
    lines = svc.status_lines(work, "systemd")
    assert lines[0].startswith("last run failed: fetch: ")
    assert lines[1].startswith("behind by 1 commits as of the last successful fetch at 2026-01-01T10:00:00")


def test_partial_run_reports_counts(tmp_path, home):
    work = _origin(tmp_path)

    class Flaky(PinnedGraph):
        def __call__(self, tool, args):
            if tool == "describe_code" and "edges" in args:
                raise codemap.MapError("describe_code: boom")
            return super().__call__(tool, args)

    sid = svc.service_id(work.resolve())
    rec = svc.record_run(sid, work, Flaky(), (3, 0), datetime(2026, 1, 1, 10, 0))
    assert rec["outcome"] == "partial"
    sha = codemap.head(work)[:12]
    line = svc.status_lines(work, "systemd")[0]
    assert line.startswith(f"partial: {rec['nodes_done']} of {rec['nodes_total']} nodes at {sha}")
    assert rec["nodes_done"] > 0


def test_choose_mode_nightly_catch_up():
    now = datetime(2026, 1, 2, 3, 17)
    assert svc.choose_mode(now, (3, 0), []) == "full"
    assert svc.choose_mode(datetime(2026, 1, 2, 2, 17), (3, 0), []) == "since"
    done = [{"mode": "full", "outcome": "ok", "ts": datetime(2026, 1, 2, 3, 1).timestamp()}]
    assert svc.choose_mode(now, (3, 0), done) == "since"
    failed = [{"mode": "full", "outcome": "failed", "ts": datetime(2026, 1, 2, 3, 1).timestamp()}]
    assert svc.choose_mode(now, (3, 0), failed) == "full"


def test_same_minute_refused(tmp_path, home):
    work = _origin(tmp_path)
    with pytest.raises(svc.Refused, match="at once"):
        _plan(work, "systemd", hourly=":00", nightly="03:00")


def test_launchd_install_needs_no_getuid(tmp_path, home, monkeypatch):
    """Windows has no os.getuid; building a launchd plan there must not raise."""
    import os
    monkeypatch.delattr(os, "getuid", raising=False)
    work = _origin(tmp_path)
    plan = _plan(work, "launchd")
    assert svc.install(plan, KEY) == []
    assert ["launchctl", "bootstrap", "gui/0", str(plan.units[0])] in home[1]
    svc.uninstall(plan.sid, "launchd")
    assert not plan.units[0].exists()


@pytest.mark.parametrize("exc", [
    lambda: Unreachable("connection refused"),
    lambda: Refused(401, "key revoked"),
    lambda: NoSession("session expired"),
])
def test_server_failure_is_recorded_as_failed(tmp_path, home, exc):
    work = _origin(tmp_path)
    sid = svc.service_id(work.resolve())
    svc.record_run(sid, work, PinnedGraph(), (3, 0), datetime(2026, 1, 1, 10, 0))

    def down(tool, args):
        raise exc()

    rec = svc.record_run(sid, work, down, (3, 0), datetime(2026, 1, 1, 11, 0))
    assert rec["outcome"] == "failed" and rec["stage"] == "server"
    assert len(svc.read_log(sid)) == 2
    assert svc.status_lines(work, "systemd")[0].startswith("last run failed: server: ")


# --- the stack map step (GRPH-1826) ---------------------------------------------------------

def _compose(work, name):
    (work / "compose.yml").write_text(
        f"services:\n  {name}:\n    build: .\n    image: x\n")
    (work / "Dockerfile").write_text("FROM python:3.12\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"compose {name}")
    _git(work, "push", "origin", "main")


def _names(record):
    return {c.get("id") for c in record.get("components", [])}


def test_stack_posts_at_target_not_head_and_current_posts_nothing(tmp_path, home):
    work = _origin(tmp_path)
    _compose(work, "alpha")
    target = codemap.head(work)
    # local HEAD moves away from the target with a different producer input
    (work / "compose.yml").write_text("services:\n  beta:\n    build: .\n    image: x\n")
    _git(work, "commit", "-am", "local only")
    assert codemap.head(work) != target
    posted = []
    graph = PinnedGraph()
    sid = svc.service_id(work.resolve())
    rec = svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 10, 0),
                         stack_post=posted.append, project="p1")
    assert rec["outcome"] == "ok" and rec["stack"] == "ok" and rec["sha"] == target
    assert len(posted) == 1 and posted[0]["mapped_sha"] == target
    names = _names(posted[0])
    assert "alpha" in names and "beta" not in names  # sabotage: build from HEAD fails this
    assert f"stack map: ok at {target[:12]}" in svc.status_lines(work, "systemd")
    # a current run posts nothing
    rec = svc.record_run(sid, work, graph, (3, 0), datetime(2026, 1, 1, 11, 0),
                         stack_post=posted.append, project="p1")
    assert rec["note"] == "current" and "stack" not in rec and len(posted) == 1


def test_refused_stack_post_is_its_own_failure(tmp_path, home):
    work = _origin(tmp_path)
    _compose(work, "alpha")

    def refuse(record):
        raise Refused(422, "bad")

    sid = svc.service_id(work.resolve())
    rec = svc.record_run(sid, work, PinnedGraph(), (3, 0), datetime(2026, 1, 1, 10, 0),
                         stack_post=refuse, project="p1")
    assert rec["outcome"] == "ok" and rec["stack"] == "failed"
    assert rec["stack_reason"]
    assert any(ln.startswith("stack map: failed: ") for ln in svc.status_lines(work, "systemd"))
