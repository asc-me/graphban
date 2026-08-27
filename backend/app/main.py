import asyncio
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


def _one_retry_pass() -> int:
    """One retry pass, session included. Runs entirely inside a worker thread.

    Owning the session here is what makes cancellation safe: nothing outside this function
    can close a session while this function is using it.
    """
    from app.services import credential_retry

    db = SessionLocal()
    try:
        return credential_retry.run_once(db)
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
            made = await asyncio.to_thread(_one_retry_pass)
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
