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


def _smoke_against(tmp_path, health_body: str) -> str:
    """Run the smoke script against a stub serving `health_body` at /health, return its output.

    RUN rather than read. Every other assertion in this file is a source-read, which the
    docstring above argues for on the grounds that the claim genuinely is "the file says X".
    The claim here is different — *the script fails on this input* — and a source-read of the
    `case` arm would pass against an arm that printed a failure and counted a pass, which is
    precisely the bug class this script exists to prevent. So it is driven.

    Everything after the identity check fails against the stub, which is fine and deliberate:
    the assertions read the identity line only.
    """
    import http.server
    import socket
    import subprocess
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = health_body.encode() if self.path.startswith("/health") else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET  # noqa: N815
        def log_message(self, *a):  # noqa: D102 — keep the test output readable
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return subprocess.run(
            ["sh", str(REPO / "scripts" / "smoke-deployment.sh"), f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    finally:
        server.shutdown()


def test_the_smoke_script_refuses_an_instance_that_cannot_state_its_revision(tmp_path):
    """`unknown` is a SENTINEL, not a revision (GRPH-426).

    The check was `[ -n "$sha" ]`, and `unknown` is not empty — so an instance that could not
    work out what it was running reported a green `release identity: git_sha=unknown`. The
    API returns that value *deliberately*, so absence stays legible rather than answering
    blank; the smoke script then read the admission as a pass.

    That is the same shape as `skip()` counting toward the pass total, one input over: a
    report that overstates what it saw. And it defeats the check's own stated purpose —
    Railway reports SUCCESS for a deploy serving stale code, and `git_sha` is how you tell.
    With `unknown` there is nothing to compare against `origin/main` at all.
    """
    out = _smoke_against(tmp_path, '{"status":"ok","git_sha":"unknown","db":"ok"}')
    identity = [ln for ln in out.splitlines() if "git_sha" in ln or "release identity" in ln]
    assert identity, f"the release-identity check did not run at all:\n{out}"
    assert any("FAIL" in ln for ln in identity), (
        f"git_sha=unknown was not reported as a failure: {identity}")
    assert not any("ok " in ln and "release identity" in ln for ln in identity), (
        f"git_sha=unknown was counted as a pass: {identity}")


def test_the_smoke_script_still_accepts_a_real_revision(tmp_path):
    """The complement, and the reason it is here: failing on every value would satisfy the
    test above while making the check useless. A real sha must still read as a pass."""
    out = _smoke_against(tmp_path, '{"status":"ok","git_sha":"26427c4","db":"ok"}')
    identity = [ln for ln in out.splitlines() if "release identity" in ln]
    assert identity, f"the release-identity check did not run at all:\n{out}"
    assert any("26427c4" in ln and "FAIL" not in ln for ln in identity), (
        f"a real revision was not accepted: {identity}")


def test_the_release_identity_check_covers_the_hosted_instance(tmp_path):
    """GRPH-426's fourth acceptance point. The runbook said release identity "applies to
    Railway as well" while the only command in the section was
    `ssh ubuntu-srv 'curl … localhost:8001/health'` — which cannot run there.

    A runbook that claims coverage it does not have is worse than one that admits a gap: the
    hosted instance is the one an operator cannot check by hand, so it is exactly where the
    unrunnable instruction costs the most. Either the section covers both, or it names what
    it does not cover.
    """
    verify = _read("docs/deploy.md")
    start = verify.index("## Verify (release identity)")
    section = verify[start:verify.index("\n## ", start + 1)]

    # The hosted HEALTH command specifically, not merely the domain appearing somewhere in
    # the section. Sabotage caught this: deleting the hosted `curl` line left the domain
    # present in the smoke-script example below it, and a bare substring check passed while
    # the direct check it is asserting about was gone.
    assert "https://cloud.graphban.dev/health" in section, (
        "the Verify section has no health check against the hosted endpoint — it verifies "
        "the self-host only, while deploy.md tells the reader this section applies to "
        "Railway too")
    assert "smoke-deployment.sh https://cloud.graphban.dev" in section, (
        "the section no longer points at the smoke script for the hosted instance, which is "
        "the check that fails on an unidentifiable build")
    assert "unknown" in section, (
        "the section does not say that git_sha=unknown is a failed verification rather than "
        "an acceptable default")
    assert "does not cover the hosted instance" in section, (
        "the alembic check is self-host-only (no ssh, no docker compose on Railway) and the "
        "section no longer says so — an inapplicable step that reads as universal")
