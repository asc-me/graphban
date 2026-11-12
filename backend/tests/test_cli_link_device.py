"""GRPH-1519 (P67 §5): `graphban link` is the device-code flow in a terminal.

Exit 0 approved (mapping stored), 1 refused (nothing stored), 2 unknown (nothing stored).
`--project LOCAL=CLOUD` sets the mapping with no prompt. The console is faked at
`httpx.post`, the one call `services.link_device` makes.
"""
import httpx
import pytest

from app import cli
from app.db import SessionLocal
from app.models import SyncLink, SyncProject
from app.services import code_sync, link_device

CONSOLE = "https://console.example"
APPROVED = {"status": "approved", "token": "gb_dep_tok", "org": {"name": "Acme"},
            "email": "eng@acme.dev"}


@pytest.fixture()
def env(client, tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHBAN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return tmp_path / "config.json"


def _console(monkeypatch, poll_answers, start=None, projects=None):
    answers = list(poll_answers)
    reachable = projects if projects is not None else [{"id": "CORE", "name": "Core"}]

    def get(url, headers=None, timeout=None, **kw):  # GET /api/sync/projects, after approval
        return httpx.Response(200, json={"projects": reachable},
                              request=httpx.Request("GET", url))

    def post(url, json=None, timeout=None, **kw):
        if url.endswith("/device/start"):
            if isinstance(start, Exception):
                raise start
            body = {"device_code": "dev-1", "user_code": "ABCD-EFGH",
                    "verification_url": f"{CONSOLE}/link",
                    "expires_at": "2030-01-01T00:00:00Z", "interval": 1}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(a, Exception):
            raise a
        return httpx.Response(200, json=a, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)


def _args(**kw):
    return type("Args", (), {"cloud_url": CONSOLE, "project": None, "org": None, **kw})()


def _state():
    db = SessionLocal()
    try:
        link = db.get(SyncLink, "instance")
        return bool(link and link.token_enc), {m.local_project_id: m.cloud_project_id
                                               for m in db.query(SyncProject)}
    finally:
        db.close()


def test_approved_prints_code_and_org_and_stores_mapping(env, monkeypatch, capsys):
    _console(monkeypatch, [{"status": "pending"}, APPROVED])
    assert cli.cmd_link(_args(project=["core=CORE"])) == 0
    out = capsys.readouterr().out
    assert f"Open {CONSOLE}/link and enter ABCD-EFGH. Expires in 10 minutes." in out
    assert "Acme" in out and "eng@acme.dev" in out
    signed_in, mapping = _state()
    assert signed_in and mapping == {"core": "CORE"}
    assert env.exists()


def test_project_flag_sets_mapping_with_no_prompt(env, monkeypatch):
    _console(monkeypatch, [APPROVED])
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
    assert cli.cmd_link(_args(project=["core=CORE"])) == 0
    assert _state()[1] == {"core": "CORE"}


def test_without_project_flag_it_asks(env, monkeypatch):
    _console(monkeypatch, [APPROVED])
    monkeypatch.setattr(code_sync, "console_projects",
                        lambda db: {"status": "ok", "projects": [{"id": "CORE", "name": "Core"}]})
    monkeypatch.setattr("builtins.input", lambda *a: "CORE")
    assert cli.cmd_link(_args()) == 0
    assert _state()[1].get("core") == "CORE"


@pytest.mark.parametrize("reason", sorted(link_device.REFUSED_REASONS))
def test_each_refusal_exits_1_and_stores_nothing(env, monkeypatch, capsys, reason):
    _console(monkeypatch, [{"status": "refused", "reason": reason}])
    assert cli.cmd_link(_args(project=["core=CORE"])) == 1
    assert reason in capsys.readouterr().err
    signed_in, mapping = _state()
    assert not signed_in and mapping == {}
    assert not env.exists()


@pytest.mark.parametrize("poll,start", [
    ([httpx.ConnectError("down")], None),
    ([{"status": "pending"}], httpx.ConnectError("down")),
])
def test_unknown_exits_2_and_stores_nothing(env, monkeypatch, capsys, poll, start):
    _console(monkeypatch, poll, start=start)
    assert cli.cmd_link(_args(project=["core=CORE"])) == 2
    err = capsys.readouterr().err
    assert err.startswith("UNKNOWN could not reach")
    signed_in, mapping = _state()
    assert not signed_in and mapping == {}
    assert not env.exists()


def test_bad_project_flag_is_a_usage_error(env):
    with pytest.raises(SystemExit):
        cli.cmd_link(_args(project=["core"]))


def test_unknown_local_project_refuses_before_signing_in(env, monkeypatch):
    _console(monkeypatch, [APPROVED])
    assert cli.cmd_link(_args(project=["nope=CORE"])) == 1
    signed_in, mapping = _state()
    assert not signed_in and mapping == {}
    assert not env.exists()


def _seed_mapping(**pairs):
    db = SessionLocal()
    try:
        code_sync.set_project_map(db, list(pairs.items()))
    finally:
        db.close()


def test_non_interactive_relink_keeps_the_stored_mapping(env, monkeypatch):
    _seed_mapping(core="CORE")
    _console(monkeypatch, [APPROVED])
    monkeypatch.setattr(code_sync, "console_projects",
                        lambda db: {"status": "ok", "projects": []})

    def eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cli.cmd_link(_args()) == 0
    assert _state()[1] == {"core": "CORE"}


def test_blank_answer_keeps_and_dash_makes_local_only(env, monkeypatch, capsys):
    _seed_mapping(core="CORE")
    _console(monkeypatch, [APPROVED])
    monkeypatch.setattr(code_sync, "console_projects",
                        lambda db: {"status": "ok", "projects": []})
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert cli.cmd_link(_args()) == 0
    assert _state()[1] == {"core": "CORE"}

    monkeypatch.setattr("builtins.input", lambda *a: "-")
    assert cli.cmd_link(_args()) == 0
    assert _state()[1] == {}


def test_project_flag_keeps_unnamed_mappings(env, monkeypatch):
    db = SessionLocal()
    try:
        from app.models import Project
        db.add(Project(id="other", name="Other", tag="OT"))
        db.commit()
    finally:
        db.close()
    _seed_mapping(core="CORE")
    _console(monkeypatch, [APPROVED])
    assert cli.cmd_link(_args(project=["other=OTHER"])) == 0
    assert _state()[1] == {"core": "CORE", "other": "OTHER"}


def test_a_cli_mapping_is_saved_with_its_tag(env, monkeypatch):
    """GRPH-1568: `--project` stores the console's tag with the mapping, so the box never
    shows `☁ ?` for a project it mapped from the terminal."""
    _console(monkeypatch, [APPROVED], projects=[
        {"id": "b1d36410-0000-4000-8000-000000000001", "tag": "CORE", "name": "Core"}])
    assert cli.cmd_link(_args(project=["core=b1d36410-0000-4000-8000-000000000001"])) == 0
    with SessionLocal() as db:
        assert {m.local_project_id: m.cloud_project_tag for m in db.query(SyncProject)} == {
            "core": "CORE"}


def test_a_cli_mapping_with_an_unreachable_projects_read_still_links(env, monkeypatch):
    _console(monkeypatch, [APPROVED])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("down")))
    assert cli.cmd_link(_args(project=["core=CORE"])) == 0
    with SessionLocal() as db:
        assert {m.local_project_id: (m.cloud_project_id, m.cloud_project_tag)
                for m in db.query(SyncProject)} == {"core": ("CORE", "")}
