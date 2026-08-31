"""`scripts/deploy.sh` deploys where it is told, and still refuses what it always refused
(GRPH-573).

The script was hardcoded to one host, one directory, one pair of ports and one Postgres role,
so the runbook the product recommends worked for exactly one person. Everybody else deployed
by hand — and deploying by hand is what the script's own header documents as having shipped
`git_sha: unknown` once and come one command from shipping an agent's uncommitted work another
time.

**These assert the FILE, not a deployment.** An end-to-end run needs a reachable host, Docker
and a built image, none of which exist in a test. What can be checked is everything that made
the script unusable elsewhere: the hardcoded values, and whether the guarantees that justify
the script survived being generalised. That is the same trade `test_deploy_docs.py` already
makes for the smoke script.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy.sh"


@pytest.fixture(scope="module")
def source() -> str:
    assert SCRIPT.exists(), "scripts/deploy.sh is gone; docs/deploy.md still points at it"
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(source: str) -> str:
    """The script with comments stripped.

    Checking for a hardcoded value has to read the CODE: this file documents the old
    hardcoded port at length, and a test that grepped the whole source would fail on its own
    explanation of the bug — passing only if the reasoning were deleted, which is precisely
    backwards.
    """
    return "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))


def rsync_invocations(source: str) -> list[str]:
    """Each `rsync …` command, joined across its backslash continuations.

    Written as a real parse rather than a regex over the whole file: a greedy pattern reads
    past the end of the command into whatever follows, so two identical invocations compare
    unequal and the test fails for a reason that has nothing to do with rsync.
    """
    out, lines = [], source.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("rsync"):
            continue
        block = [line]
        while block[-1].rstrip().endswith("\\") and i + len(block) < len(lines):
            block.append(lines[i + len(block)])
        out.append(" ".join(b.strip().rstrip("\\").strip() for b in block))
    return out


# ---- what made it unusable for anyone else -------------------------------------------------

def test_the_target_host_is_configurable(code):
    """`--host`, or the environment. Without one of these a second install has to edit the
    script and re-edit it on every pull.

    Asserted against the CODE, and against the branch that PARSES the flag rather than the
    string anywhere. Both of these survived a sabotage that deleted the parsing branch,
    because the usage comment at the top still mentioned the flag — the test was reading the
    documentation for the feature instead of the feature.
    """
    assert re.search(r"--host\)\s", code), "no case branch parses --host"
    assert "GRAPHBAN_DEPLOY_HOST" in code, "the env fallback is gone"


def test_a_local_deploy_needs_no_ssh(code):
    """A Mac Studio or a Linux box running the stack on ITSELF has no ssh hop. Without this
    the script cannot deploy to the machine it is running on."""
    assert re.search(r"--local\)\s", code), "no case branch parses --local"
    assert re.search(r"LOCAL=1", code), "--local does not set the flag the helper reads"


def test_the_ports_are_read_from_the_target_rather_than_assumed(source, code):
    """THE LATENT BUG THIS FIXES. The old script polled `localhost:8001`, which is true only
    of this repository's box because that box had `:8000` busy. On a default install it
    checked a port nothing served — so a perfectly good deploy would report as broken, which
    is how people learn to ignore a verification step."""
    assert "target_env API_PORT" in code, "the api port is not read from the target"
    assert "target_env WEB_PORT" in code, "the web port is not read from the target"
    hardcoded = re.findall(r"localhost:(\d+)", code)
    assert not hardcoded, f"a port is hardcoded in executable code: {hardcoded}"


def test_the_postgres_role_is_read_from_the_target(source):
    """`psql -U agentledger -d agentledger` is this deployment's role and database, and the
    alembic check silently fails against any install that named them differently."""
    assert "target_env POSTGRES_USER" in source
    assert "target_env POSTGRES_DB" in source


def test_compose_defaults_are_the_fallbacks(source):
    """An install that sets nothing must still verify correctly, so the fallbacks have to be
    compose's own defaults rather than this box's overrides."""
    assert "target_env API_PORT 8000" in source
    assert "target_env WEB_PORT 8080" in source


def test_ssh_is_not_called_directly_anywhere_that_matters(source):
    """One helper decides whether there is an ssh hop. Two call sites — one going through the
    helper and one calling `ssh` directly — is how a local deploy half-works: the build runs
    locally and the verification silently checks a different machine."""
    body = source.split("set -euo pipefail", 1)[1]
    direct = [ln.strip() for ln in body.splitlines()
              if re.search(r"(?<![\w-])ssh\s+\"?\$REMOTE", ln) and "on_target()" not in ln]
    assert len(direct) <= 1, (
        f"ssh is invoked directly outside the on_target helper: {direct}"
    )


# ---- what must NOT have been lost --------------------------------------------------------

def test_it_still_deploys_a_commit_from_a_detached_worktree(source):
    """The property the two recorded incidents produced. `rsync` ships what is on disk, and
    a shared checkout stopped being safe the day agents started working in it."""
    assert "agentledger-wt-deploy" in source
    assert "--detach" in source


def test_it_still_refuses_a_dirty_worktree(source):
    """Anything in a worktree nobody works in means something is wrong that a deploy must not
    paper over. A generalisation that dropped this would be worse than the hardcoding."""
    assert "refusing" in source
    assert "status --short" in source


def test_it_still_verifies_release_identity_on_both_surfaces(code):
    """A deploy that builds cleanly and serves the PREVIOUS revision looks identical to a
    successful one from the outside. Both the api and the web bundle are checked, because the
    web bundle is baked at build time and can lag independently.

    Against `code`, not `source` (GRPH-573 bounce). Commenting out the LIVE_SHA
    comparison left this green when it grepped the full file, because the
    commented line still contained the substring.
    """
    assert 'LIVE_SHA" = "$GIT_SHA' in code, "the api sha is not compared"
    assert 'WEB_SHA" = "$GIT_SHA' in code, "the web sha is not compared"


def test_tilde_is_expanded_only_for_a_local_deploy(code):
    """THE CALL. Unguarded `${TARGET_DIR/#\\~/$HOME}` rewrote the default
    `~/agentledger/` to this machine's home before rsync, so a remote deploy
    targeted `ubuntu-srv:/Users/alex/agentledger/`.
    """
    assert re.search(
        r'\[ -n "\$LOCAL" \]; then\n(?:[^\n]*\n)*?[^\n]*TARGET_DIR="\$\{TARGET_DIR/#\\~/\$HOME\}"',
        code,
    ), "tilde expansion is not gated on --local"
    unguarded = [
        ln for ln in code.splitlines()
        if "TARGET_DIR=" in ln and "/#\\~/$HOME}" in ln.replace(" ", "")
        and "LOCAL" not in ln
    ]
    # The rewrite line itself does not mention LOCAL; the `if` above must be
    # the only one. Fail if a second, unguarded rewrite exists.
    rewrites = [ln for ln in code.splitlines() if "/#\\~/$HOME}" in ln.replace(" ", "")]
    assert len(rewrites) == 1, f"tilde rewrite should appear once, found {rewrites}"


def test_the_env_file_is_still_excluded_from_the_sync(source):
    """`--delete` without this removes the target's `.env`, after which compose reverts to
    default ports while the persisted volume keeps the old password — a broken box that looks
    like a config mistake."""
    for line in source.splitlines():
        if line.strip().startswith("rsync"):
            continue
    assert source.count("--exclude .env") >= 1
    assert source.count("--exclude sync") >= 1


def test_every_rsync_invocation_excludes_the_same_things(source):
    """Adding a local branch means there are now TWO rsync calls, and a difference between
    them is invisible until it deletes something on one path only.

    `.env` is the one that bites: `--delete` without it removes the target's file, compose
    reverts to default ports, and the persisted volume keeps the old password.
    """
    blocks = rsync_invocations(source)
    assert len(blocks) >= 2, f"expected a local and a remote rsync, found {len(blocks)}"
    excludes = [sorted(re.findall(r"--exclude (\S+)", b)) for b in blocks]
    assert all(e == excludes[0] for e in excludes), (
        f"rsync invocations disagree on what they exclude: {excludes}"
    )
    assert ".env" in excludes[0] and "sync" in excludes[0]
