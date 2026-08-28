import asyncio
import pathlib
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

import logging

from app.config import settings
from app.errors import QuotaExceeded
from app.observability import RequestIdMiddleware
from app.db import SessionLocal, init_db
from app.version import __version__
from app.mcp_server import router as mcp_router
from app.routers import (
    artifacts,
    admin,
    agent,
    analytics,
    apikeys,
    assistant,
    auth,
    fleet,
    items,
    learning,
    memory,
    orgs,
    platform,
    prds,
    projects,
    public,
    reports,
    requests,
    sync,
)


logger = logging.getLogger("graphban.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.observability import configure_logging
    from app.security.startup import check_security

    configure_logging()  # structured logs + request-id stamping (AL-56)
    check_security()  # refuse/warn on a weak JWT secret before serving (AL-44)

    if settings.is_sqlite:
        # SQLite (tests / zero-infra dev): create tables directly.
        init_db()
    else:
        # Postgres: schema is owned by Alembic migrations.
        from app.migrate import run_migrations

        run_migrations()

    if settings.seed_on_start:
        from app.seed import seed

        db = SessionLocal()
        try:
            if seed(db):
                logger.info("seed: loaded Graphban prototype dataset")
        finally:
            db.close()

    # GRPH-55's deferral, evaluated (GRPH-540). It parked real scaling work behind a precise
    # trigger — first project over ~5k items — and nothing checked it, so the condition could
    # only fire if somebody re-measured on a hunch. Here rather than beside `check_security`
    # because that runs before the schema exists, and this one has to count rows.
    db = SessionLocal()
    try:
        from app.scaling import check_scaling_triggers

        check_scaling_triggers(db)
    except Exception:  # pragma: no cover - a tripwire must never keep the app down
        logger.exception("scaling trigger check failed")
    finally:
        db.close()

    # Apply any persisted platform LLM config so it drives the live providers.
    # Use the first existing project (there may be none on a freshly wiped DB).
    from sqlalchemy import select

    from app.models import Project
    from app.services.platform import apply_llm, get_config

    db = SessionLocal()
    try:
        # The CHAT provider still comes from a project, and still the alphabetically first one.
        # That is a real wart and it is not this slice's (PRD-25 S4 covers the EMBEDDER); it is
        # left visible rather than tidied silently.
        first = db.scalars(select(Project).order_by(Project.name)).first()
        if first is not None:
            apply_llm(get_config(db, first.id))

        # The EMBEDDER no longer rides on that. It used to be configured by whichever project
        # sorted first alphabetically, so on a multi-project deployment a project RENAME could
        # silently change which model produced every vector written afterwards — and nothing
        # recorded that it had. It is now the deployment's own credential, falling back to the
        # environment when none is set (PRD-25 D-c).
        # **The credential migration runs BEFORE anything serves** (GRPH-538). PRD-25 S6
        # removed resolution step 0 — the branch that read each project's legacy `providers`
        # blob — and every project configured the old way depends on this having moved its
        # configuration into a credential row first. Without it: step 0 gone, pointers
        # unwritten, every such project silently resolving to the offline stub. That is the
        # downgrade step 0 existed to prevent, arriving in the slice that removed it.
        #
        # Idempotent by construction: a project that already has a pointer is skipped, so on a
        # migrated deployment this costs one query per boot.
        #
        # It must NOT be able to stop the app starting. A deployment that cannot boot is worse
        # than one whose migration needs another attempt — and because it is idempotent, the
        # next boot simply tries again. The failure is logged loudly rather than swallowed.
        from app.services import credential_migration

        try:
            report = credential_migration.migrate(db)
            if report["credentials_created"]:
                logger.info("credential migration: %s", report)
        except Exception:  # noqa: BLE001 — see above: booting matters more
            logger.exception("credential migration failed; projects configured the old way "
                             "will resolve to the stub until it succeeds")

        from app.services.embedder import apply_embedder

        apply_embedder(db)
    finally:
        db.close()

    # The service's FIRST background task (PRD-25 S2b). Three properties it must have, each
    # of them a test rather than a hope:
    #
    #   1. It must not throw its way out. A credential probe reaches the network, and an
    #      unhandled error here would take down every self-hosted install on startup.
    #   2. It must not hold a session open. A Session held for the process lifetime pins a
    #      connection and serves increasingly stale identity-map reads; each pass opens and
    #      closes its own.
    #   3. Cancelling it must not hang shutdown.
    #
    # `create_task` rather than awaiting: the loop runs FOR the app, not before it. Awaiting
    # here would mean the first probe's timeout is added to every boot.
    retry_task = None
    if settings.credential_retry_seconds > 0:
        retry_task = asyncio.create_task(_credential_retry_loop())

    try:
        yield
    finally:
        if retry_task is not None:
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task


def _one_background_pass() -> int:
    """One pass of the background work, session included. Runs inside a worker thread.

    Owning the session here is what makes cancellation safe: nothing outside this function can
    close a session while this function is using it (GRPH-535).

    Two jobs share the pass, and the ORDER is deliberate. The retry is bounded and cheap — a
    handful of rows at most. The re-index is long, so it goes second and does exactly ONE batch
    per pass: a loop that drained the whole re-index inside a single pass would block the retry
    behind a job measured in minutes, and the two would stop being independent.
    """
    from app.services import credential_retry, reindex

    db = SessionLocal()
    try:
        did = credential_retry.run_once(db)
        try:
            did += reindex.run_batch(db)
        except Exception:  # noqa: BLE001 — a stuck re-index must not stop credential retries
            logger.warning("re-index batch failed; retries continue", exc_info=True)
        return did
    finally:
        db.close()


async def _credential_retry_loop() -> None:
    """Re-ask credentials that could not be asked, forever, without ever raising.

    The `except Exception` is deliberately broad and deliberately INSIDE the loop. A narrower
    one would let an unanticipated failure kill the task silently — the task dies, nothing
    logs, and `pending_validation` rows simply stop being retried while the console still shows
    them as scheduled. Catching everything and continuing means a persistent fault produces a
    persistent log line instead of silence.

    `CancelledError` is re-raised rather than swallowed: it is how shutdown asks this to stop,
    and catching it would hang the process on exit.
    """
    from app.services import credential_retry

    interval = settings.credential_retry_seconds
    while True:
        try:
            await asyncio.sleep(interval)
            # **The thread opens and closes its own session.** The previous shape held the
            # session out here and closed it in a `finally` — and `asyncio.to_thread` cannot
            # cancel the thread it started, so `retry_task.cancel()` during shutdown made the
            # AWAIT raise while the worker thread was still inside `run_once(db)`. The
            # `finally` then closed a session that thread was actively using.
            #
            # Worse for shutdown: `to_thread` runs on the loop's default ThreadPoolExecutor,
            # whose threads are NON-DAEMON and joined at interpreter exit. A thread left
            # working on a closed session can block the process from exiting at all — which
            # is what "Terminate orphan process (python)" looked like in CI.
            made = await asyncio.to_thread(_one_background_pass)
            if made:
                logger.info("credential retry: %d attempt(s)", made)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see the docstring: silence here is the failure
            logger.warning("credential retry pass failed; continuing", exc_info=True)


app = FastAPI(title="Graphban API", version=__version__, lifespan=lifespan)

@app.exception_handler(QuotaExceeded)
async def _quota_handler(_: Request, exc: QuotaExceeded):
    """A hosted plan limit hit via a REST route → HTTP 402 Payment Required. (The MCP
    dispatcher maps the same exception to a ``quota_exceeded`` tool error itself.)"""
    detail = str(exc) + (f" — {exc.hint}" if exc.hint else "")
    return JSONResponse(status_code=402, content={"detail": detail})


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last = outermost, so a request id is assigned before anything else runs and
# every downstream log line carries it (AL-56).
app.add_middleware(RequestIdMiddleware)

# REST API under /api; MCP endpoint at /api/mcp.
API = "/api"
app.include_router(auth.router, prefix=API)
app.include_router(projects.router, prefix=API)
app.include_router(items.router, prefix=API)
app.include_router(requests.router, prefix=API)
app.include_router(memory.router, prefix=API)
app.include_router(artifacts.router, prefix=API)
app.include_router(fleet.router, prefix=API)
app.include_router(learning.router, prefix=API)
app.include_router(apikeys.router, prefix=API)
app.include_router(agent.router, prefix=API)
app.include_router(assistant.router, prefix=API)
app.include_router(prds.router, prefix=API)
app.include_router(analytics.router, prefix=API)
app.include_router(platform.router, prefix=API)
app.include_router(public.router, prefix=API)
app.include_router(reports.router, prefix=API)
app.include_router(sync.router, prefix=API)
app.include_router(mcp_router, prefix=API)
# The Organization layer is a hosted-SaaS surface only (AL-74). It's mounted here but
# every route is gated by a hosted-only dependency (see routers/orgs.require_hosted):
# with HOSTED_MODE off, every org/invite endpoint 404s, so self-host has no usable
# org surface. Gating per-request (vs. a build-time `if`) keeps the flag authoritative
# at runtime and lets the test suite exercise the surface under a monkeypatched flag.
app.include_router(orgs.router, prefix=API)
# Operator plane (AL-91): hosted + platform-admin gated at the router level; every
# route 404s for tenants, so the surface is invisible outside the operator allowlist.
app.include_router(admin.router, prefix=API)


@app.get("/api/config")
def public_config():
    """Unauthenticated deploy flags the SPA needs before login to shape onboarding:
    whether this is a hosted (org) deployment and whether self-serve signup is open.
    Deliberately tiny — no secrets, just the two switches that change the UI."""
    return {
        "hosted_mode": settings.hosted_mode,
        "signup_mode": settings.signup_mode,
    }


@app.get("/health")
def health():
    """Liveness + release identity. Always HTTP 200 while the process is up (so the
    container healthcheck tracks the API, not the DB); `db` reports readiness, and
    `version`+`git_sha` tell you exactly what revision is running (see docs/deploy.md)."""
    db_ok = True
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — health must never raise
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "graphban-api",
        "version": __version__,
        "git_sha": settings.resolved_git_sha,
        "db": "ok" if db_ok else "down",
        # Embedding readiness (AL-248). The startup check already warns on a stub embedder
        # in hosted mode — but stdout is not a surface anyone watches, and the hosted
        # instance ran for days on stub embeddings with that warning scrolling past in the
        # logs unread. `db` is here for exactly this reason; the embedder belongs beside it.
        # Boolean only: enough to notice and go look, without advertising the stack on an
        # unauthenticated endpoint.
        #
        # Chat is deliberately absent. It is per-project BYOK resolved from the DB, so no
        # instance-wide field can describe it — `settings.chat_provider` is only a legacy
        # mirror. Per-project truth is `PlatformConfigOut.effective_chat_provider`.
        "providers": {
            "embed_ok": settings.embed_provider != "stub",
        },
    }


# ---------------------------------------------------------------------------------------
# THE SPA, SERVED BY THE API (GRPH-577, PRD-27 S1)
#
# In Docker, nginx serves the bundle and proxies `/api/` here. That is a second service to
# install and supervise, and it is where GRPH-523 came from — nginx resolved the backend
# address once at boot. A native install has no reason to pay for it: this is a Python
# process and some static files.
#
# MOUNTED LAST, deliberately. Every `/api/` route and `/health` is registered above, so the
# catch-all below cannot shadow one — an ordering bug here would turn a real endpoint into
# an HTML page and the failure would look like a frontend routing problem.
#
# CONDITIONAL ON THE BUNDLE EXISTING, rather than on a setting. The api container has no
# `web/dist`, so the Docker path is untouched BY CONSTRUCTION instead of by a flag somebody
# has to set correctly on one deployment and not the other.
_DIST = pathlib.Path(__file__).resolve().parents[2] / "web" / "dist"

#: What nginx sets with `add_header … always`. The `always` is the whole point: nginx applies
#: these to ERROR responses too, and middleware that only decorated 2xx would drop them on
#: exactly the responses an attacker can most easily provoke.
SPA_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "frame-ancestors 'self' https:",
}


def _mount_spa(application: FastAPI, dist: pathlib.Path) -> None:
    """Serve the built SPA, matching what `web/nginx.conf.template` does today.

    Kept as a function taking its directory so the behaviour is testable against a fixture
    bundle rather than only against a tree that happens to have been built — a mount that is
    only ever exercised when `web/dist` exists is one nobody tests.
    """
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.staticfiles import StaticFiles

    @application.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        for name, value in SPA_SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # `html=False` so a miss raises 404 rather than falling back to index.html. nginx spells
    # this `try_files $uri =404` and the reason is not tidiness: a missing bundle that returns
    # index.html answers 200 with HTML where the browser asked for JavaScript, which surfaces
    # as a MIME-type error pointing at the wrong thing entirely — while a stale index.html
    # naming a hashed file that no longer exists reads as a working deploy.
    application.mount("/assets", StaticFiles(directory=dist / "assets", html=False),
                      name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        """Anything not matched above is a client-side route."""
        # An unmatched `/api/*` is a MISSING ENDPOINT, and must stay JSON. Returning the SPA
        # would hand an agent an HTML page where it expected an error object, and the 200
        # would read as success.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        target = dist / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(dist / "index.html")


if _DIST.is_dir():  # pragma: no cover - exercised via `_mount_spa` against a fixture
    _mount_spa(app, _DIST)
