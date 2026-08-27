"""The observability floor had no tests at all (GRPH-33).

`app/observability.py` is the only thing standing between an operator and a silent box, and
every claim in it was prose. Two were false:

**Nothing logged a request.** `configure_logging` silenced `uvicorn.access` because it
"duplicates what we already see", and nothing else emitted a request line. Measured against
a real uvicorn with LOG_JSON=true: three requests, including a 404, produced **zero** log
lines mentioning a path or a status. Railway log search is the only way to ask what a
deployed box served, and the answer was nothing. The recent proxy outage was diagnosed from
*nginx's* 499s precisely because the API service kept no record of its own traffic.

**LOG_JSON=true did not make the stream JSON.** uvicorn runs `dictConfig(LOGGING_CONFIG)`
when the server boots — before the app's lifespan calls `configure_logging` — and that pins
a plain-text handler on the parent `uvicorn` logger with `propagate=False`. Setting only the
root handler left app lines as JSON and every uvicorn line as `INFO:     ...` on one stream,
which is worse for an ingester than either format alone.

Asserted on EMITTED OUTPUT, never on configuration. "The handler is installed" and "the line
comes out in that format" are different claims, and the gap between them is where both of
these lived.

Async is driven with `asyncio.run` rather than a plugin: the repo has no async test support
and one middleware is not a reason to add a dependency to every suite.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import logging.config

import pytest

from app.config import settings

SHARE_TOKEN = "super-secret-share-token"


@pytest.fixture(autouse=True)
def _restore_logging():
    """Put the logging module back exactly as found, for EVERY test in this file.

    Not hygiene — this is the bug the file is about, turned on itself. Three of the eight
    CI failures that led here were other suites' `caplog` coming back empty, because
    something had replaced the root handlers pytest installs. `configure_logging` does
    `root.handlers[:] = [...]`, and `fileConfig` below additionally flips `.disabled` on
    every logger it does not name. Both outlive the test that caused them and are invisible
    to it: the damage lands on whatever runs next in the same worker, which under `-n auto`
    is a different file on every run.

    So: handlers, level, and every logger's disabled flag, saved and restored.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_disabled = {
        name: lg.disabled
        for name, lg in logging.root.manager.loggerDict.items()
        if isinstance(lg, logging.Logger)
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, was_disabled in saved_disabled.items():
        lg = logging.root.manager.loggerDict.get(name)
        if isinstance(lg, logging.Logger):
            lg.disabled = was_disabled


@pytest.fixture()
def capture(monkeypatch):
    """Install the app's real handler and redirect it to a buffer.

    The handler and formatter are the ones `configure_logging` builds — a hand-rolled
    formatter here could drift from the shipped one and this suite would keep passing.
    """
    def install(json_mode: bool) -> io.StringIO:
        from app import observability

        monkeypatch.setattr(settings, "log_json", json_mode)
        monkeypatch.setattr(settings, "log_level", "INFO")
        observability.configure_logging()
        stream = io.StringIO()
        for h in logging.getLogger().handlers:
            h.stream = stream
        return stream

    return install


def drive(*, status=200, headers=None, raises=False, path="/api/public/roadmap"):
    """Run one request through RequestIdMiddleware. Returns the messages it sent."""
    from app import observability

    scope = {"type": "http", "method": "GET", "path": path,
             "query_string": f"token={SHARE_TOKEN}".encode(), "headers": headers or []}
    sent: list[dict] = []

    async def app(scope, receive, send):
        if not raises:
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        else:
            raise RuntimeError("boom")

    async def send(message):
        sent.append(message)

    async def go():
        mw = observability.RequestIdMiddleware(app)
        await mw(scope, lambda: None, send)

    if raises:
        with pytest.raises(RuntimeError):
            asyncio.run(go())
    else:
        asyncio.run(go())
    return sent


def lines(stream: io.StringIO) -> list[str]:
    return [ln for ln in stream.getvalue().splitlines() if ln.strip()]


def test_a_request_produces_exactly_one_access_line(capture):
    """The hole itself. Before this, three requests including a 404 produced zero lines.

    Exactly one, not at least one: a per-request line that fires twice doubles the log bill
    and makes every rate computed from it wrong.
    """
    stream = capture(False)
    drive()

    got = lines(stream)
    assert len(got) == 1, f"expected one access line per request, got {len(got)}: {got}"
    assert "GET" in got[0] and "/api/public/roadmap" in got[0] and "200" in got[0]


def test_the_access_line_never_carries_the_query_string(capture):
    """A SECURITY assertion, not a formatting one.

    The public share link's token is a query parameter (`routers/public.py::_public_project`)
    and it IS the credential — holding it is what separates a reader from the 404 everyone
    else gets. Logging it copies a live secret into a store that outlives the request, is
    searchable by anyone with log access, and is reachable by no revocation path the app has.
    """
    stream = capture(True)
    drive()

    out = stream.getvalue()
    assert SHARE_TOKEN not in out, f"the share token was written to the log: {out}"
    assert "token=" not in out, f"a query string reached the log: {out}"
    assert "/api/public/roadmap" in out, "the path is gone too — this test would prove nothing"


def test_an_exception_is_logged_as_the_500_the_client_got(capture):
    """The request that most needs a record is the one that blew up. Status 0 would invent a
    response nothing served; no line at all would make a crash the one event with no trace."""
    stream = capture(True)
    drive(raises=True)

    got = lines(stream)
    assert got, "a request that raised produced no access line at all"
    assert json.loads(got[-1])["http"]["status"] == 500


def test_the_access_line_carries_the_same_request_id_as_the_response(capture):
    """Correlation is the entire point of the id, and the join is response-header → log.
    Emitting the line after the contextvar is reset yields `-` on the one line an operator
    starts from."""
    stream = capture(True)
    sent = drive(headers=[(b"x-request-id", b"abc123")])

    echoed = dict(sent[0]["headers"])[b"x-request-id"].decode()
    logged_id = json.loads(lines(stream)[-1])["request_id"]
    assert echoed == "abc123", "an inbound request id was not honoured"
    assert logged_id == echoed, \
        f"response says {echoed!r}, log says {logged_id!r} — the two cannot be joined"


def test_structured_fields_are_filterable_not_buried_in_a_sentence(capture):
    """A log platform can filter `http.status >= 500`. It cannot filter a sentence."""
    from app import observability

    stream = capture(True)
    observability._log_access({"method": "POST", "path": "/api/items", "query_string": b""},
                              201, 12.34)

    assert json.loads(stream.getvalue().strip())["http"] == {
        "method": "POST", "path": "/api/items", "status": 201, "duration_ms": 12.3}


def test_log_json_puts_uvicorns_own_lines_in_json_too(capture):
    """The second defect, reproduced in the true production order: uvicorn's dictConfig
    first, the app's lifespan second."""
    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    stream = capture(True)
    logging.getLogger("uvicorn.error").info("Application startup complete.")

    line = stream.getvalue().strip()
    assert line.startswith("{"), f"a uvicorn line escaped the JSON formatter: {line!r}"
    assert json.loads(line)["message"] == "Application startup complete."


def test_uvicorns_access_logger_stays_quiet_so_the_line_is_not_doubled(capture):
    """`_log_access` and `uvicorn.access` describe the same event. Funnelling uvicorn's
    loggers into our handler without also keeping this one quiet would log every request
    twice, in two formats."""
    capture(True)

    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_text_mode_still_reads_like_text(capture):
    """The control. Forcing JSON unconditionally would satisfy the JSON test above and make
    `docker compose logs` unreadable for the self-host default, which is LOG_JSON=false."""
    from app import observability

    stream = capture(False)
    observability._log_access({"method": "GET", "path": "/health", "query_string": b""},
                              200, 1.0)

    out = stream.getvalue()
    assert not out.strip().startswith("{"), f"text mode emitted JSON: {out!r}"
    assert "GET /health 200" in out


def test_the_container_starts_through_the_entrypoint_that_configures_logging():
    """A module nobody runs is indistinguishable from one that does not exist.

    `app/serve.py` exists solely to call `configure_logging()` before uvicorn's first line;
    if the Dockerfile goes back to the `uvicorn` CLI, the two plain-text boot lines return
    and nothing else notices — the suite never boots a container.
    """
    import pathlib

    dockerfile = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"
    body = dockerfile.read_text()

    assert "app.serve" in body, "the image no longer starts through app.serve"
    assert "uvicorn app.main:app" not in body, \
        "the image starts the uvicorn CLI again — boot lines will bypass configure_logging"


def test_the_entrypoint_stops_uvicorn_reconfiguring_logging_underneath_it():
    """`log_config=None` is the load-bearing argument. Without it uvicorn runs its own
    dictConfig after ours and the boot lines revert, which is invisible to every test that
    only checks the module is imported."""
    import inspect

    from app import serve

    src = inspect.getsource(serve.main)
    assert "configure_logging()" in src
    assert "log_config=None" in src, \
        "uvicorn is free to install its own logging config over ours"
    assert src.index("configure_logging()") < src.index("uvicorn.run"), \
        "logging is configured after uvicorn starts, which is the bug this module exists for"


def test_the_bind_stays_ipv4_because_nginx_resolves_ipv4_only():
    """A cross-file invariant that only prose held. `web/nginx.conf.template` sets
    `ipv6=off` on its resolver because uvicorn binds 0.0.0.0, and says whoever changes the
    bind must change that too. Nothing enforced it: an entrypoint reading HOST from the
    environment would let the two drift with no diff to review, and nginx would resolve an
    AAAA record pointing at an address nothing is listening on.
    """
    import inspect
    import pathlib

    from app import serve

    src = inspect.getsource(serve.main)
    assert 'host="0.0.0.0"' in src, "the bind is no longer a hardcoded IPv4 address"

    template = pathlib.Path(__file__).resolve().parents[2] / "web" / "nginx.conf.template"
    assert "ipv6=off" in template.read_text(), (
        "nginx no longer resolves IPv4-only; if that was deliberate the bind above has to "
        "change with it, and this test is the pair"
    )


def test_nothing_on_the_boot_path_prints_instead_of_logging():
    """One `print` puts a plain-text line in an otherwise-JSON stream, and an ingester that
    drops malformed lines drops it silently.

    That is not hypothetical: `[seed] loaded Graphban prototype dataset` was the single
    non-JSON line left after the formatter was fixed, and it was found by reading a real
    container's logs, not by any test. The three in `security/startup.py` were worse — the
    SECURITY WARNING banners, which carried no level for a platform to alert on.

    ASSERTED AGAINST SOURCE, and here that is the right instrument rather than a compromise:
    the claim is "these modules contain no print calls", which is a fact about the file.
    Parsed rather than grepped so the word appearing in a docstring is not a failure.

    `app/cli.py` is deliberately exempt. It writes to a human's terminal, and routing an
    `api key: …` line through a JSON logger would be a regression for the person reading it.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "cli.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")

    assert not offenders, (
        "these write plain text into the log stream instead of a levelled record: "
        + ", ".join(offenders)
    )


def test_the_runbook_names_the_start_command_the_image_actually_uses():
    """`docs/deploy.md` told operators the API starts via `uvicorn --port ${PORT:-8000}`,
    which stopped being true the moment the Dockerfile moved to `app.serve` — and nothing
    would have said so. The runbook is what someone reads at 3am while a deploy is failing;
    a start command that does not exist sends them looking for a process that isn't there.

    Same class as GRPH-528: a doc that restates a fact from the tree, with no guard, drifts
    at the first change to the tree.
    """
    import pathlib

    docs = pathlib.Path(__file__).resolve().parents[2] / "docs"
    # The Railway section moved to its own file in GRPH-36, and THIS TEST IS HOW THAT WAS
    # NOTICED — it was pinned to deploy.md, the fact moved out, and the assertion fired on a
    # green-looking refactor. Left pointing at wherever the start command actually lives.
    railway = (docs / "deploy-railway.md").read_text()
    deploy = (docs / "deploy.md").read_text()

    assert "app.serve" in railway, "the runbook no longer names the image's actual start command"
    for name, body in (("deploy-railway.md", railway), ("deploy.md", deploy)):
        assert "uvicorn --port" not in body, \
            f"{name} still documents the old uvicorn CLI invocation"


# ---- alembic must not silence the app on the boot path that runs it -----------------------


def test_alembics_own_config_would_still_drop_every_info_record(capture):
    """Characterises the half that survives GRPH-525, so layer 2 cannot be read as duplicate.

    GRPH-525 already passes `disable_existing_loggers=False`, which keeps the app's loggers
    ENABLED — that was the half it was chasing, and it saves `logger.warning`. It does not
    save INFO. `fileConfig` still applies alembic.ini's `[logger_root]`, which sets
    `level = WARNING` and swaps root's handler for the plain `generic` console one.

    So on Postgres, after migrations and with GRPH-525 in place, every INFO record is still
    dropped — the per-request access log, `graphban.main`'s "credential retry: N attempt(s)",
    the seed line — and whatever survives comes out as plain text on a stream `LOG_JSON=true`
    promised would be JSON. Measured:

        start   : disabled=False formatter=['_JsonFormatter']
        layer 1 : disabled=False formatter=['Formatter']   <- level also raised to WARNING

    If alembic.ini ever stops raising the root level, this test says so and layer 2 can go.
    """
    import pathlib as _p
    from logging.config import fileConfig

    from app.observability import access_logger

    stream = capture(True)
    ini = _p.Path(__file__).resolve().parents[1] / "alembic.ini"

    fileConfig(str(ini), disable_existing_loggers=False)  # exactly GRPH-525's call

    assert not access_logger.disabled, "GRPH-525's half regressed — loggers are disabled again"

    root = logging.getLogger()
    assert root.level > logging.INFO, (
        "alembic.ini no longer raises the root level; re-check whether layer 2 is still needed"
    )
    for h in root.handlers:
        h.stream = stream
    access_logger.info("this must not survive")
    assert stream.getvalue() == "", (
        f"an INFO record survived alembic's config: {stream.getvalue()!r} — if that is now "
        "true, layer 2 in alembic/env.py is no longer load-bearing"
    )


def test_migrations_run_from_the_app_do_not_reconfigure_logging():
    """The wiring, in both halves — either alone is inert.

    Read from source rather than executed: `alembic/env.py` runs its module body on import
    and needs a live database to do so, and the branch that matters is the one taken on
    Postgres. The behavioural proof is the Postgres CI job, which is where the failure was.
    """
    import inspect
    import pathlib

    from app import migrate

    assert 'attributes["configure_logger"] = False' in inspect.getsource(migrate.run_migrations), \
        "run_migrations no longer tells env.py to leave logging alone"

    env = (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text()
    guard = 'config.attributes.get("configure_logger", True)'
    assert guard in env, "env.py calls fileConfig unconditionally again"
    assert "disable_existing_loggers=False" in env, (
        "GRPH-525's layer is gone; without it `alembic upgrade head` on the command line "
        "disables the app's loggers for the rest of that process"
    )
    assert env.index(guard) < env.index("fileConfig(config.config_file_name"), \
        "the guard no longer precedes the fileConfig call it is meant to gate"
