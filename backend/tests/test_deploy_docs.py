"""The deployment docs have to describe the tree they ship with (GRPH-36).

Deployment docs rot in a specific way: they are read once during setup, then only again at
3am when something is broken — which is the worst moment to discover the runbook describes a
command that no longer exists. This session already fixed two instances of the class
(GRPH-424, PRD copies drifting; GRPH-528, doc tables drifting from the adapters) and created
a third: `deploy.md` documented `uvicorn --port ${PORT:-8000}` for a day after the image
moved to `python -m app.serve`.

Asserted against source, and here that is the right instrument rather than a compromise —
the claim genuinely is "the file says X", and `test_observability.py` carries the longer
argument about when source-reading is and is not enough.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.exists(), f"{rel} does not exist — this suite is asserting about nothing"
    return path.read_text()


def test_railway_lives_in_exactly_one_document():
    """GRPH-36 asked for a `deploy-railway.md`. Written as a NEW file beside deploy.md's
    Railway section it would have been a second environment-variable table, and two variable
    tables disagree within a month — the failure GRPH-424 was filed for. The section moved;
    deploy.md points at it. This fails if a copy grows back."""
    railway = _read("docs/deploy-railway.md")
    deploy = _read("docs/deploy.md")

    assert "| `JWT_SECRET` |" in railway, "the variable table is not in deploy-railway.md"
    assert "| `JWT_SECRET` |" not in deploy, (
        "deploy.md has grown its own copy of the Railway variable table — one of the two is "
        "already wrong and nothing says which"
    )
    assert "deploy-railway.md" in deploy, "deploy.md no longer points anywhere for Railway"


def test_the_pgvector_requirement_is_documented_where_it_bites():
    """The single thing that stops a Railway deploy dead, and it was documented nowhere in
    the Railway section before this item. Migration 0001's FIRST statement creates the
    extension, migrations run on startup, and Railway's stock Postgres does not ship it."""
    migration = (BACKEND / "alembic" / "versions" / "0001_initial.py").read_text()
    assert 'CREATE EXTENSION IF NOT EXISTS vector' in migration, (
        "migration 0001 no longer creates the vector extension — if that is deliberate, the "
        "warnings in deploy-railway.md and README are now scaremongering and should go"
    )

    for doc in ("docs/deploy-railway.md", "README.md"):
        body = _read(doc)
        assert "vector" in body and "pgvector" in body, f"{doc} does not mention pgvector"
        assert "CREATE EXTENSION IF NOT EXISTS vector" in body, (
            f"{doc} warns about pgvector without showing the statement that fails, so a "
            f"reader cannot match it against what they see in the logs"
        )


def test_the_smoke_script_exists_and_is_executable():
    """A runbook step nobody can run is prose. The README tells operators to run this after
    every deploy."""
    script = REPO / "scripts" / "smoke-deployment.sh"
    assert script.exists(), "scripts/smoke-deployment.sh is gone; README still points at it"
    assert script.stat().st_mode & 0o111, "smoke-deployment.sh is not executable"

    body = script.read_text()
    assert "GRAPHBAN_API_KEY" in body, "the write-path check vanished"
    assert "/api/mcp" in body, "the MCP check vanished"


def test_the_smoke_script_never_counts_a_check_it_could_not_run():
    """The first version printed a green `pgvector ok` for a SQLite deployment, reasoning
    that `db=ok` implies migration 0001 ran. True on Postgres, vacuous on SQLite — which uses
    `create_all` and runs no migration at all. That is the exact false pass this script
    exists to prevent, produced by the script itself.

    `skip` must therefore touch neither counter.
    """
    body = (REPO / "scripts" / "smoke-deployment.sh").read_text()

    start = body.index("skip()")
    assert "PASS=$((PASS+1))" not in body[start:body.index("\n", start)], \
        "skip() increments the pass count — an unrunnable check would report as passed"
    assert "FAIL=$((FAIL+1))" not in body[start:body.index("\n", start)], \
        "skip() increments the fail count — an unrunnable check would report as a failure"


def test_the_readme_and_the_runbook_name_the_same_start_command():
    """`docs/deploy.md` drifted on exactly this a day ago. Now two files describe the
    deployment, so there are two places to drift from `backend/Dockerfile`."""
    dockerfile = (BACKEND / "Dockerfile").read_text()
    assert "app.serve" in dockerfile, "the image no longer starts through app.serve"

    for doc in ("docs/deploy-railway.md", "README.md"):
        assert "uvicorn --port" not in _read(doc), \
            f"{doc} documents the old uvicorn CLI invocation the image stopped using"
