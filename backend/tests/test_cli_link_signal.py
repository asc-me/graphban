"""`graphban link` records the link where the SERVER reads it (AL-281 / PRD-14 D5).

The CLI used to write `~/.graphban/config.json` while `code_sync.link_status()` read the
`sync_link` row, so a CLI-linked box reported `linked: false` and AL-284's authority gate
failed open. Since GRPH-1519 the link is written by the device-code approval itself
(`link_device.poll`), so the server-visible row exists exactly when the CLI says approved.
These tests pin both directions.
"""
import httpx
import pytest

from app.db import SessionLocal
from app.services import code_sync

CONSOLE = "https://console.example"


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    from app import cli

    monkeypatch.setenv("GRAPHBAN_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return cli


def _fake_console(monkeypatch, poll):
    def post(url, json=None, timeout=None, **kw):
        if url.endswith("/device/start"):
            body = {"device_code": "dev-1", "user_code": "ABCD-EFGH",
                    "verification_url": f"{CONSOLE}/link", "expires_at": "2030-01-01T00:00:00Z",
                    "interval": 1}
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))
        return httpx.Response(200, json=poll, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)


def _args(**kw):
    return type("Args", (), {"cloud_url": CONSOLE, "project": None, "org": None, **kw})()


def _linked() -> bool:
    db = SessionLocal()
    try:
        return code_sync.link_status(db)["linked"]
    finally:
        db.close()


def test_an_approved_cli_link_is_visible_to_the_server(client, cli_env, monkeypatch):
    _fake_console(monkeypatch, {"status": "approved", "token": "gb_dep_tok",
                                "org": {"name": "Acme"}, "email": "e@acme.dev"})
    assert not _linked()
    assert cli_env.cmd_link(_args(project=["core=CORE"])) == 0
    assert _linked(), "a CLI-linked instance must not read as unlinked"
    cfg = cli_env.load_config()
    assert cfg["cloud_url"] == CONSOLE and "api_key" not in cfg


def test_a_refused_cli_link_leaves_the_server_unlinked(client, cli_env, monkeypatch):
    _fake_console(monkeypatch, {"status": "refused", "reason": "declined"})
    assert cli_env.cmd_link(_args()) == 1
    assert not _linked()
    assert cli_env.load_config() == {}


def test_config_file_is_chmod_600(client, cli_env, monkeypatch, tmp_path):
    _fake_console(monkeypatch, {"status": "approved", "token": "t",
                                "org": {"name": "Acme"}, "email": "e@acme.dev"})
    cli_env.cmd_link(_args(project=["core=CORE"]))
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_status_warns_when_the_two_records_disagree(client, cli_env, capsys):
    cli_env.save_config({"cloud_url": "https://cloud.example"})
    db = SessionLocal()
    try:
        code_sync.set_link(db, cloud_url="https://elsewhere.example", api_key="k")
    finally:
        db.close()
    cli_env.cmd_status(type("A", (), {"project": "core"})())
    out = capsys.readouterr().out
    assert "WARNING" in out and "elsewhere.example" in out, out
