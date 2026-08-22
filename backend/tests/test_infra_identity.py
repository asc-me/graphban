"""Infra identifiers: what the rename may touch, and what it must not (AL-264).

The dividing line is the one PRD-13 arrived at the hard way — **an identifier that
existing data is keyed by is identity, not branding.** Package names and health strings
are labels and rename freely. The Postgres volume key, the compose project name, and the
`POSTGRES_*` defaults are keyed to data that already exists on deployed instances.

These are guard tests, aimed squarely at a future cosmetic sweep (tier 4) doing a
find-and-replace across the repo. Getting any of them "consistent" would orphan a volume
or lock an instance out of its own database, and both failures present as data loss
rather than as a rename bug.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "docker-compose.yml").read_text()
DEPLOY_DOC = (REPO / "docs" / "deploy.md").read_text()


# ---- renamed: labels, nothing keyed by them ---------------------------------------
def test_health_reports_the_new_service_name(client):
    assert client.get("/health").json()["service"] == "graphban-api"


def test_packages_are_renamed():
    assert 'name = "graphban-api"' in (REPO / "backend" / "pyproject.toml").read_text()
    assert '"name": "graphban-web"' in (REPO / "web" / "package.json").read_text()


def test_the_ephemeral_test_database_is_renamed():
    """Created fresh per CI run and per local Postgres pass, so nothing is keyed by it."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "graphban_test" in ci and "agentledger_test" not in ci
    agents = (REPO / "AGENTS.md").read_text()
    assert "graphban_test" in agents and "agentledger_test" not in agents


# ---- frozen: deployed data is keyed by these --------------------------------------
def test_compose_pins_its_project_name():
    """`docker compose` otherwise derives the project name from the DIRECTORY, and names
    volumes `<project>_<volume-key>`. Live that is `agentledger_agentledger_pgdata`, so
    renaming the repo directory would silently create an empty volume and the database
    would read as wiped. Pinning it is what makes the directory safe to rename."""
    assert "\nname: agentledger\n" in COMPOSE, (
        "docker-compose.yml must pin `name:` — without it the compose project name "
        "tracks the directory name and the Postgres volume moves with it"
    )


def test_the_volume_key_is_not_renamed():
    assert "agentledger_pgdata:" in COMPOSE, (
        "renaming the volume key orphans the existing volume; Postgres comes up empty "
        "and it reads as data loss, not as a rename"
    )


@pytest.mark.parametrize("var", ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"])
def test_postgres_defaults_are_not_renamed(var):
    """Baked into a volume at initdb. Moving them breaks any self-host that never wrote a
    `.env` — `password authentication failed for user "agentledger"`, which docs/deploy.md
    documents as a deploy-breaking failure."""
    assert f"${{{var}:-agentledger}}" in COMPOSE, f"{var} default must stay `agentledger`"
    assert f"{var}=agentledger" in (REPO / ".env.example").read_text()


# ---- the runbook has to name the SAME frozen identifiers ---------------------------
# Added after the tier-4 cosmetic sweep renamed them in docs/deploy.md while leaving the
# box untouched: the runbook told an operator to `cd ~/graphban` and `psql -U graphban`,
# neither of which exists. Freezing the values in compose is not enough if the document
# people actually follow says something else — the section explaining which identifiers
# must never be renamed had its own identifiers renamed, and nothing noticed for days.

def test_the_runbook_uses_the_real_postgres_role_and_database():
    """Every psql invocation in the runbook must use the frozen role/db. A doc that names
    a role which does not exist fails at the worst moment — mid-recovery."""
    for bad in ("psql -U graphban", "-d graphban "):
        assert bad not in DEPLOY_DOC, (
            f"docs/deploy.md uses {bad!r}; the live role and database are `agentledger` "
            "(frozen at initdb — see the tests above)"
        )
    assert "psql -U agentledger -d agentledger" in DEPLOY_DOC


def test_the_runbook_targets_the_real_deploy_directory():
    """`~/graphban` does not exist on the box. Following it would rsync into a fresh
    directory with no `.env`, and compose would come up on default ports — the exact
    port-conflict failure documented a few lines below it."""
    assert "~/graphban" not in DEPLOY_DOC, (
        "docs/deploy.md points at ~/graphban; the server directory is ~/agentledger"
    )
    assert "ubuntu-srv:~/agentledger/" in DEPLOY_DOC


def test_the_runbook_quotes_the_compose_project_name_correctly():
    """It claims compose pins a project name; that claim has to match the pin, or the
    volume-orphaning explanation around it is nonsense."""
    assert "pins `name: agentledger`" in DEPLOY_DOC


# ---- source files must stay diffable -----------------------------------------
def test_no_tracked_text_file_contains_control_bytes():
    """A NUL byte in a source file makes it BINARY to git, and unreviewable.

    `web/src/lib/graph/galaxy.ts` shipped through a PR with two NUL bytes in it, used as the
    separator in `` `${x}\\0${y}` `` and `key.split("\\0")`. They were consistent with each
    other, so every test passed and `tsc` was clean — the separator is arbitrary and the same
    one was used both times. Nothing was functionally wrong.

    What was wrong is that git stored it as binary. The merge showed
    `galaxy.ts | Bin 8159 -> 10020 bytes` instead of a diff, `grep` and `ripgrep` skipped the
    file entirely, and a reviewer had no way to read the change. In a repo whose whole practice
    is reviewable commits carrying their reasoning, an undiffable source file defeats the
    review rather than failing it — which is why this is a guard and not a lint rule someone
    can skip.

    Tabs, newlines and carriage returns are allowed; everything else below 0x20 is not.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True
    ).stdout.split()
    binary_ext = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip"}

    offenders = []
    for rel in tracked:
        path = REPO / rel
        if not path.is_file() or path.suffix.lower() in binary_ext:
            continue
        data = path.read_bytes()
        bad = [i for i, b in enumerate(data) if b == 0 or b < 9 or 13 < b < 32]
        if bad:
            offenders.append(f"{rel} (first at byte {bad[0]}, {len(bad)} total)")

    assert offenders == [], (
        "control bytes in tracked text files — git will treat these as BINARY and they cannot "
        f"be diffed or grepped: {offenders}"
    )


def test_an_absent_revision_reads_as_unknown_rather_than_blank(client):
    """`/health` must never answer `ok` with an empty revision (GRPH-426).

    Railway resolves `GIT_SHA` from `RAILWAY_GIT_COMMIT_SHA`, which the platform supplies
    **only for GitHub-triggered deploys**. A redeploy started any other way — a variable
    change, a manual restart — sets it to the empty string, and an explicit empty value wins
    over the `"unknown"` default. The hosted instance answered `"git_sha": ""` for weeks
    because of exactly that.

    A blank revision is the absence-reads-as-clean shape in ops form: `ok` with nothing to
    say, where what happened is that it could not find out. Every question that starts "is
    the fix live?" then ends in a guess.
    """
    from app.config import settings

    original = settings.git_sha
    try:
        for blank in ("", "   "):
            settings.git_sha = blank
            body = client.get("/health").json()
            assert body["git_sha"] == "unknown", f"{blank!r} leaked through as {body['git_sha']!r}"

        settings.git_sha = "b41944e"
        assert client.get("/health").json()["git_sha"] == "b41944e"
    finally:
        settings.git_sha = original
