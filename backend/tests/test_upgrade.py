"""An upgrade verifies what a deploy verifies, and puts the old release back (GRPH-585).

PRD-27 S5. The property being defended is the one `docs/deploy.md` exists for: *a deploy that
builds cleanly and serves the PREVIOUS revision looks identical to a successful one from the
outside.* That is not a Docker property, so the native path checks the same three facts.

The rollback tests matter most. A recovery path nobody exercises is one that does not work,
and this one cannot be exercised in production — so it is exercised here, against a real
directory tree, with a service that genuinely fails to come up.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_upgrade as up  # noqa: E402

DEPLOY_SH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"
NEW, OLD = "beef1234", "cafe5678"


@pytest.fixture()
def install(tmp_path: pathlib.Path) -> pathlib.Path:
    """A root with a current release, as S3/S4 would have left it. API-only: no web bundle."""
    (tmp_path / "current").mkdir()
    (tmp_path / "current" / "marker").write_text(OLD, encoding="utf-8")
    (tmp_path / "backend").mkdir()
    return tmp_path


@pytest.fixture()
def bundled_release(release: pathlib.Path) -> pathlib.Path:
    """A release that CARRIES a built SPA.

    S1 mounts the SPA only when `web/dist` exists, so the web sha is a fact about installs that
    have one — which the S6 walk found by upgrading an API-only install and watching it refuse
    forever. The bundle belongs to the RELEASE rather than the install it replaces, because the
    check runs after the swap and therefore describes the incoming code; putting it on the old
    install (my first attempt) proves nothing, since the swap removes it.
    """
    dist = release / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    return release


@pytest.fixture()
def release(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "incoming"
    d.mkdir()
    (d / "marker").write_text(NEW, encoding="utf-8")
    return d


def serving(sha: str):
    """A box that answers healthy at one revision."""
    return (lambda base, **kw: {"status": "ok", "git_sha": sha, "db": "ok"},
            lambda base, **kw: sha)


def calls_recorder():
    seen: list[str] = []
    return seen, lambda action: seen.append(action)


# ---- the happy path, and that it says what it verified --------------------------------------

def test_a_good_upgrade_succeeds_and_reports_all_three_facts(install, release, capsys,
                                                             monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    probe, web = serving(NEW)
    _, restart = calls_recorder()

    rc = up.upgrade(install, release, NEW, base="http://x", python=pathlib.Path("py"),
                    restart=restart, probe=probe, web=web, head=lambda r, p: "0091")

    assert rc == up.EXIT_OK
    out = capsys.readouterr().out
    for fact in up.IDENTITY_FACTS:
        assert fact in out, f"the report does not name {fact}"
    assert "0091" in out


def test_the_new_release_is_in_place_afterwards(install, release, monkeypatch):
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    probe, web = serving(NEW)
    _, restart = calls_recorder()

    up.upgrade(install, release, NEW, base="http://x", python=pathlib.Path("py"),
               restart=restart, probe=probe, web=web, head=lambda r, p: "0091")

    assert (install / "current" / "marker").read_text() == NEW
    assert (install / "previous" / "marker").read_text() == OLD, "the old release was not kept"


def test_it_stops_before_swapping_and_starts_after(install, release, monkeypatch):
    """Swapping the directory under a running process is how you get a service holding files
    that no longer exist."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    probe, web = serving(NEW)
    seen, restart = calls_recorder()

    up.upgrade(install, release, NEW, base="http://x", python=pathlib.Path("py"),
               restart=restart, probe=probe, web=web, head=lambda r, p: "0091")

    assert seen[0] == "stop" and "start" in seen


def test_the_operators_env_survives_the_swap(install, tmp_path, monkeypatch):
    """copytree of the tarball would replace the operator's secret. This slice's
    suite never noticed `preserve_env` becoming `pass`.
    """
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    (install / "current" / "backend").mkdir(parents=True)
    (install / "current" / "backend" / ".env").write_text("JWT_SECRET=keep-me\n", encoding="utf-8")
    incoming = tmp_path / "incoming-with-env"
    incoming.mkdir()
    (incoming / "backend").mkdir()
    (incoming / "backend" / ".env").write_text("JWT_SECRET=from-the-tarball\n", encoding="utf-8")
    (incoming / "marker").write_text(NEW, encoding="utf-8")
    probe, web = serving(NEW)
    _, restart = calls_recorder()

    up.upgrade(install, incoming, NEW, base="http://x", python=pathlib.Path("py"),
               restart=restart, probe=probe, web=web, head=lambda r, p: "0091")

    assert (install / "current" / "backend" / ".env").read_text(encoding="utf-8") == "JWT_SECRET=keep-me\n"


def test_the_cli_upgrade_command_calls_upgrade(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr(up, "platform_restart", lambda **k: (lambda action: None))
    monkeypatch.setattr(
        up, "upgrade",
        lambda *a, **k: seen.append("upgrade") or up.EXIT_OK,
    )
    rc = up.main(["upgrade", "--root", str(tmp_path), "--release", str(tmp_path), "--sha", NEW])
    assert rc == up.EXIT_OK
    assert seen == ["upgrade"], "the CLI upgrade command never ran upgrade()"


def test_the_cli_uninstall_command_calls_uninstall(monkeypatch, tmp_path):
    seen: list[str] = []
    monkeypatch.setattr(up, "platform_restart", lambda **k: (lambda action: None))
    monkeypatch.setattr(
        up, "uninstall",
        lambda *a, **k: seen.append("uninstall") or up.EXIT_OK,
    )
    rc = up.main(["uninstall", "--root", str(tmp_path)])
    assert rc == up.EXIT_OK
    assert seen == ["uninstall"], "the CLI uninstall command never ran uninstall()"


# ---- the rollback, which is the reason this slice exists ------------------------------------

def test_a_release_that_never_becomes_healthy_is_rolled_back(install, release, monkeypatch):
    """THE ONE THAT MATTERS. A recovery path nobody runs does not work, and this one cannot be
    rehearsed in production."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    monkeypatch.setattr(up, "wait_healthy", lambda base, **kw: None)
    _, restart = calls_recorder()

    rc = up.upgrade(install, release, NEW, base="http://x", python=pathlib.Path("py"),
                    restart=restart, probe=lambda b, **k: None, web=lambda b, **k: "",
                    head=lambda r, p: "")

    assert rc == up.EXIT_ROLLED_BACK
    assert (install / "current" / "marker").read_text() == OLD, "the old release was not restored"


def test_a_release_that_serves_the_wrong_sha_is_rolled_back(install, release, monkeypatch):
    """The subtle failure: it came up perfectly healthy — serving the PREVIOUS revision. That
    is exactly what the runbook says looks identical to success from the outside."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    probe, web = serving(OLD)  # healthy, but it is the old code
    _, restart = calls_recorder()

    rc = up.upgrade(install, release, NEW, base="http://x", python=pathlib.Path("py"),
                    restart=restart, probe=probe, web=web, head=lambda r, p: "0091")

    assert rc == up.EXIT_ROLLED_BACK
    assert (install / "current" / "marker").read_text() == OLD


def test_a_web_bundle_lagging_the_api_is_caught(install, bundled_release, monkeypatch):
    """The bundle is baked at build time and can lag the API independently, which is why
    `deploy.sh` checks it separately rather than trusting `/health` alone."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    _, restart = calls_recorder()

    rc = up.upgrade(install, bundled_release, NEW, base="http://x", python=pathlib.Path("py"),
                    restart=restart,
                    probe=lambda b, **k: {"status": "ok", "git_sha": NEW, "db": "ok"},
                    web=lambda b, **k: OLD,
                    head=lambda r, p: "0091")

    assert rc == up.EXIT_ROLLED_BACK


def test_the_failure_names_every_wrong_fact_not_just_the_first(install, bundled_release, monkeypatch,
                                                               capsys):
    """An operator who fixes one problem and re-runs to find a second was told half of what
    was already known."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    _, restart = calls_recorder()

    up.upgrade(install, bundled_release, NEW, base="http://x", python=pathlib.Path("py"),
               restart=restart,
               probe=lambda b, **k: {"status": "ok", "git_sha": OLD, "db": "ok"},
               web=lambda b, **k: OLD, head=lambda r, p: "0091")

    err = capsys.readouterr().err
    assert "api serves" in err and "web serves" in err


def test_with_no_previous_release_it_says_so_rather_than_pretending(tmp_path, release,
                                                                    monkeypatch):
    """A first install has nothing to go back to, and reporting a successful rollback would be
    the worst possible lie here."""
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    (tmp_path / "backend").mkdir()
    _, restart = calls_recorder()

    rc = up.upgrade(tmp_path, release, NEW, base="http://x", python=pathlib.Path("py"),
                    restart=restart, probe=lambda b, **k: None, web=lambda b, **k: "",
                    head=lambda r, p: "")

    assert rc == up.EXIT_UNHEALTHY


# ---- the two paths must not drift -----------------------------------------------------------

def test_the_identity_facts_match_deploy_sh():
    """Two checkers that drift is the real risk, and it drifts silently — each looks correct
    on its own. `deploy.sh` is the older one; this asserts the native path checks what it does.
    """
    sh = DEPLOY_SH.read_text(encoding="utf-8")
    assert set(up.IDENTITY_FACTS) == {"api", "web", "alembic"}
    for fact in up.IDENTITY_FACTS:
        assert f'"    {fact}' in sh or f"    {fact}" in sh, (
            f"deploy.sh no longer reports {fact}; the two verifications have diverged"
        )


def test_the_alembic_head_is_asked_of_the_database_not_the_tree():
    """Reading `alembic/versions/` reports what the RELEASE contains — right by construction
    and therefore worth nothing. The question is which migration actually ran.

    Read from the function's BODY with the docstring removed. The first version grepped the
    whole source and failed on the docstring that explains why the tree is not read — passing
    only if the reasoning were deleted. That is the third time in this session a test of mine
    has asserted against prose instead of code.
    """
    import ast
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(up.alembic_head))).body[0]
    # Every string literal EXCEPT the docstring — which is the function's actual behaviour,
    # with its explanation excluded rather than grepped.
    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)][1:]

    assert any("alembic" in s for s in literals), "it does not invoke alembic"
    assert any("current" in s for s in literals), "it does not ask which migration ran"
    assert not any("versions" in s for s in literals), (
        f"it reads the tree rather than asking the database: {literals}"
    )


# ---- uninstall -------------------------------------------------------------------------------

def test_uninstall_removes_the_code_and_keeps_the_data(tmp_path, capsys):
    (tmp_path / "current").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "graphban.db").write_text("rows", encoding="utf-8")
    _, restart = calls_recorder()

    up.uninstall(tmp_path, restart=restart)

    assert not (tmp_path / "current").exists()
    assert (tmp_path / "data" / "graphban.db").read_text() == "rows", "it deleted the data"
    out = capsys.readouterr().out
    assert "DATABASE was not touched" in out
    assert "service account" in out, "it does not say the account was left"


def test_purge_removes_the_data_directory_but_is_opt_in(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x").write_text("y", encoding="utf-8")
    _, restart = calls_recorder()

    up.uninstall(tmp_path, restart=restart, purge=True)

    assert not (tmp_path / "data").exists()


def test_uninstall_stops_the_service_first(tmp_path):
    seen, restart = calls_recorder()
    up.uninstall(tmp_path, restart=restart)
    assert seen and seen[0] == "stop"
