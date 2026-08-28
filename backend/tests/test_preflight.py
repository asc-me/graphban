"""The preflight verifies, or it refuses — it never reports nothing (GRPH-578, PRD-27 S2).

The failure this file exists to prevent is the one this repository names by name: a check that
could not run reading as a check that passed. Every path below either names what it found or
names what to do, and the two are asserted separately.

The decision logic is driven with injected collaborators, so every refusal is exercised without
needing a broken machine to produce it — a preflight only ever run against a working host is one
whose refusals nobody has read. Two tests at the end run against the REAL Postgres this
repository already uses for its Postgres suite, because a checker proved only against fakes has
never checked anything.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import graphban_preflight as pf  # noqa: E402

URL = "postgresql+psycopg://postgres:postgres@localhost:5544/graphban_test"


def psql_returning(*results):
    """A stand-in `psql` answering each query in turn with (rc, output)."""
    seq = list(results)

    def run(parts, sql, **kw):
        return seq.pop(0) if seq else (0, "")
    return run


VERSION = (0, "PostgreSQL 16.4 on aarch64-apple-darwin")
AVAILABLE = (0, "0.7.4")
ENABLED = (0, "0.7.4")


# ---- the DSN ------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "postgresql+psycopg://postgres:postgres@localhost:5544/graphban_test",
    "postgresql://postgres:postgres@localhost:5544/graphban_test",
])
def test_a_compound_driver_scheme_parses_the_same_as_a_plain_one(url):
    """`postgresql+psycopg://` is what this project's own config uses.

    It needs no special handling and the test says so deliberately: an earlier version
    stripped the suffix, with a comment claiming psql could not understand it. True, and
    irrelevant — psql never receives the URL, only `-h/-p/-U/-d`. A sabotage reinstating the
    raw URL broke nothing, because there was nothing to break.
    """
    parts = pf.dsn_parts(url)
    assert parts["host"] == "localhost" and parts["port"] == 5544
    assert parts["database"] == "graphban_test" and parts["user"] == "postgres"


def test_the_password_is_not_in_the_printable_form():
    """`safe` is what refusals print. A DSN echoed into a terminal or a log with the password
    in it is a credential leak performed by the diagnostic."""
    assert "postgres:postgres@" not in pf.dsn_parts(URL)["safe"]
    assert "5544" in pf.dsn_parts(URL)["safe"]


# ---- each refusal, and that it says what to do ---------------------------------------------

def test_a_missing_psql_refuses_with_an_install_line():
    r = pf.preflight(URL, have_psql=False)
    assert r.code == pf.EXIT_NO_PSQL
    assert "not on PATH" in r.problem
    assert "install" in r.remedy.lower()


def test_an_unreachable_server_refuses_and_names_the_target():
    r = pf.preflight(URL, have_psql=True,
                     psql=psql_returning((2, "could not connect to server")))
    assert r.code == pf.EXIT_UNREACHABLE
    assert "5544" in r.problem, "the refusal does not say where it tried"
    assert "pg_isready" in r.remedy


def test_pgvector_absent_refuses_with_the_platform_install_line():
    r = pf.preflight(URL, have_psql=True, psql=psql_returning(VERSION, (0, "")))
    assert r.code == pf.EXIT_NO_VECTOR
    assert ("brew" in r.remedy) or ("apt" in r.remedy)


def test_pgvector_available_but_not_enabled_is_its_own_refusal(monkeypatch):
    """THE DISTINCTION THAT MATTERS. Installed-but-not-enabled and not-installed are one
    keystroke apart in the output and a package install apart in the fix."""
    r = pf.preflight(URL, have_psql=True, psql=psql_returning(VERSION, AVAILABLE, (0, "")))
    assert r.code == pf.EXIT_VECTOR_NOT_ENABLED
    assert "CREATE EXTENSION vector" in r.remedy
    assert "graphban_test" in r.remedy, "it does not say which database to run it in"


def test_it_refuses_to_enable_the_extension_itself():
    """A schema change to a database the operator owns. The PRD's constraint is that this
    installer verifies Postgres and never modifies it."""
    r = pf.preflight(URL, have_psql=True, psql=psql_returning(VERSION, AVAILABLE, (0, "")))
    assert "will not" in r.remedy, "it does not say that it is declining deliberately"


def test_a_busy_port_refuses_and_names_it():
    r = pf.preflight(URL, api_port=8123, have_psql=True,
                     psql=psql_returning(VERSION, AVAILABLE, ENABLED),
                     free=lambda h, p: False)
    assert r.code == pf.EXIT_PORT_BUSY
    assert "8123" in r.problem and "lsof" in r.remedy


def test_every_exit_code_is_distinct():
    """Collapsing any two sends the operator to the wrong afternoon."""
    codes = [pf.EXIT_OK, pf.EXIT_NO_PSQL, pf.EXIT_UNREACHABLE, pf.EXIT_NO_VECTOR,
             pf.EXIT_VECTOR_NOT_ENABLED, pf.EXIT_PORT_BUSY]
    assert len(set(codes)) == len(codes)


# ---- a pass must be distinguishable from a run that checked nothing -------------------------

def test_a_pass_reports_what_it_found(monkeypatch):
    """The absence-reads-as-clean guard. A preflight that printed nothing on success would be
    indistinguishable from one whose checks never ran."""
    r = pf.preflight(URL, have_psql=True, psql=psql_returning(VERSION, AVAILABLE, ENABLED),
                     free=lambda h, p: True)
    assert r.ok
    joined = " ".join(r.found)
    assert "PostgreSQL 16.4" in joined, "a pass does not name the server it checked"
    assert "pgvector" in joined and "enabled" in joined
    assert "port" in joined


def test_no_database_url_is_a_refusal_not_a_pass(monkeypatch, capsys):
    """The sharpest version of the same trap: with nothing to check, the quiet reading is the
    reassuring one. Exit non-zero and say so."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = pf.main(["--database-url", ""])

    assert code != pf.EXIT_OK, "a preflight with nothing to check reported success"
    assert "nothing was verified" in capsys.readouterr().err


def test_the_cli_exits_with_the_refusals_code(monkeypatch, capsys):
    monkeypatch.setattr(pf, "preflight",
                        lambda *a, **k: pf.Result(pf.EXIT_NO_VECTOR, "no vector", "install it"))
    assert pf.main(["--database-url", URL]) == pf.EXIT_NO_VECTOR
    assert "no vector" in capsys.readouterr().err


# ---- against the real thing ----------------------------------------------------------------

pg_only = pytest.mark.skipif(
    shutil.which("psql") is None or not os.environ.get("GRAPHBAN_PREFLIGHT_PG"),
    reason="needs psql and a reachable Postgres; set GRAPHBAN_PREFLIGHT_PG=1 to run",
)


@pg_only
def test_it_passes_against_the_real_postgres():
    r = pf.preflight(os.environ["GRAPHBAN_PREFLIGHT_PG"], api_port=8231)
    assert r.ok, f"{r.problem} / {r.remedy}"
    assert any("PostgreSQL" in f for f in r.found)


@pg_only
def test_a_wrong_port_is_refused_against_the_real_postgres():
    bad = os.environ["GRAPHBAN_PREFLIGHT_PG"].replace(":5544", ":5599")
    assert pf.preflight(bad).code == pf.EXIT_UNREACHABLE
