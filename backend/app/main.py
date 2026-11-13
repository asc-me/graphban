import asyncio
import pathlib
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

import logging

from app.config import settings
from app.errors import QuotaExceeded, Refused
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
    cli_pair,
    documents,
    fleet,
    harness,
    items,
    learning,
    linear,
    live,
    memory,
    orgs,
    platform,
    prds,
    projects,
    public,
    reports,
    requests,
    rules,
    slice_pull,
    specs,
    sync,
    tracker_links,
    usage,
)


logger = logging.getLogger("graphban.main")


def setup_url_banner() -> str:
    """The line a self-hoster reads in `docker compose up` (GRPH-P67 G3)."""
    import os

    url = os.environ.get("PUBLIC_WEB_URL") or "http://localhost:8080"
    return f"Graphban is ready at {url} - open this to continue setup"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.observability import configure_logging
    from app.security.startup import check_security

    configure_logging()  # structured logs + request-id stamping (AL-56)
    check_security()  # refuse/warn on a weak JWT secret before serving (AL-44)

    from app import leader

    # Once-per-deploy boot work runs under a Postgres advisory lock so N uvicorn workers
    # queue instead of racing (GRPH-1241); the later ones find it all already done.
    with leader.boot_lock():
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

    # The installed rule pack (GRPH-1329): every worker re-verifies and registers it, since
    # the library is per-process. Never fatal: a pack that no longer verifies is refused with
    # its reason (shown in the Library group) and the server serves without it.
    db = SessionLocal()
    try:
        from app.services import rule_pack

        rule_pack.reload_persisted(db)
    except Exception:  # noqa: BLE001
        logger.warning("installed rule pack reload failed", exc_info=True)
    finally:
        db.close()

    # The installed plan grant (GRPH-1343): verified here once, then only its expiry is compared.
    db = SessionLocal()
    try:
        from app.services import plan_grant

        plan_grant.load(db)
    except Exception:  # noqa: BLE001
        logger.warning("installed plan grant load failed", exc_info=True)
    finally:
        db.close()

    # Browser push key pair (GRPH-1582): generated once and kept in the database, so push
    # needs no operator setup. Never fatal; a failure leaves push unconfigured.
    db = SessionLocal()
    try:
        from app.services import push

        push.ensure_keys(db)
    except Exception:  # noqa: BLE001
        logger.warning("push key ensure failed", exc_info=True)
    finally:
        db.close()

    # LLM span retention (GRPH-225), once before serving and never fatal: janitorial
    # work on telemetry does not justify taking the box down, and a boot that dies on
    # a DELETE is exactly the "unhandled error takes down every self-host install"
    # failure the retry loop above goes out of its way to avoid.
    try:
        from app.providers import llm_meter

        llm_meter.purge_expired()
    except Exception:  # noqa: BLE001
        logger.warning("llm span retention pass failed; spans keep accumulating until "
                       "the next boot", exc_info=True)

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
            with leader.boot_lock():  # one worker migrates; the rest see it done (GRPH-1241)
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
    # Under N uvicorn workers exactly one process owns both loops (GRPH-1241).
    is_leader = leader.claim_background_leader()
    if is_leader:
        logger.info(setup_url_banner())
    if not is_leader:
        logger.info("background loops: not the leader; another worker runs them")
    retry_task = None
    if is_leader and settings.credential_retry_seconds > 0:
        logger.info("credential retry loop started")
        retry_task = asyncio.create_task(_credential_retry_loop())

    # The log-export drain (PRD-47 S15) gets its OWN task rather than becoming a third job in
    # `_one_background_pass`: a collector POST can sit for its whole timeout, and that pass
    # exists precisely so the credential retry is never blocked behind long work. Same three
    # properties as the loop above, and `0` disables it for the same reason — the suite drives
    # `logexport.drain_once` directly instead of waiting on a timer.
    export_task = None
    if is_leader and settings.log_export_seconds > 0:
        logger.info("log export loop started")
        export_task = asyncio.create_task(_log_export_loop())

    # The memory judge refresh (GRPH-1242) gets its own task too: the memory pages read
    # stored verdicts only, so this is the one place a judge is asked about them, and a slow
    # decider must not hold up credential retries. Same three properties; `0` disables it.
    # Leader-only like the loops above; the pass's own advisory lock is the second guard.
    judge_task = None
    if is_leader and settings.memory_judge_refresh_seconds > 0:
        judge_task = asyncio.create_task(_memory_judge_loop())

    # Auto-update (GRPH-1299): its own task, leader-only, `0` disables. Each tick opens and
    # closes its own session; the claims inside `auto_update.tick` are conditional UPDATEs,
    # so a second process that also believed it led could not start a second install.
    update_task = None
    if is_leader and settings.auto_update_tick_seconds > 0:
        update_task = asyncio.create_task(_auto_update_loop())

    # Plan grant fetch/refresh (GRPH-1344): leader-only, `0` disables. The first pass runs at
    # once (the startup fetch); an unlinked box makes no call.
    grant_task = None
    if is_leader and settings.plan_grant_check_seconds > 0:
        grant_task = asyncio.create_task(_plan_grant_loop())

    # Question grace sweep (GRPH-1527): leader-only, `0` disables.
    sweep_task = None
    if is_leader and settings.question_sweep_seconds > 0:
        sweep_task = asyncio.create_task(_question_sweep_loop())

    try:
        yield
    finally:
        for task in (retry_task, export_task, judge_task, update_task, grant_task, sweep_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        leader.release_background_leader()


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


def _plan_grant_pass(startup: bool) -> None:
    from app.services import plan_grant

    db = SessionLocal()
    try:
        plan_grant.refresh_if_due(db, startup=startup)
    finally:
        db.close()


def _question_sweep_pass() -> None:
    from app.services import waits as waits_svc

    db = SessionLocal()
    try:
        waits_svc.sweep_questions(db)
    finally:
        db.close()


async def _question_sweep_loop() -> None:
    """Hand overdue decision waits to a person. Same properties as `_plan_grant_loop`."""
    while True:
        try:
            await asyncio.to_thread(_question_sweep_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("question sweep pass failed", exc_info=True)
        await asyncio.sleep(settings.question_sweep_seconds)


async def _plan_grant_loop() -> None:
    """Fetch the plan grant at startup, then re-check on a timer. Same properties as
    `_credential_retry_loop`: broad `except` inside, own session per pass, cancel re-raised."""
    startup = True
    while True:
        try:
            await asyncio.to_thread(_plan_grant_pass, startup)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("plan grant fetch pass failed", exc_info=True)
        startup = False
        await asyncio.sleep(settings.plan_grant_check_seconds)


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


async def _log_export_loop() -> None:
    """Hand records to the OTLP collector forever, without ever raising (PRD-47 S15).

    The same three properties as `_credential_retry_loop`, for the same reasons: a collector
    that is down must not take the API process with it, a broad `except` INSIDE the loop turns
    an unanticipated fault into a repeated log line instead of a silent death, and
    `CancelledError` is re-raised so shutdown is not hung by it.

    `mark_exporter_running` / `mark_exporter_stopped` bracket the loop because the status strip
    reports a queue depth ONLY while this is alive. The flag is set in the `finally`, so a
    cancelled or crashed loop stops claiming to be draining — an exporter that died and left
    `queue depth: 0` on the panel is the exact absence-reads-as-clean this item is about.
    """
    from app.services import logexport

    interval = settings.log_export_seconds
    logexport.mark_exporter_running()
    try:
        while True:
            try:
                await asyncio.sleep(interval)
                moved = await asyncio.to_thread(_one_log_export_pass)
                if moved:
                    logger.info("log export: %d record(s) sent", moved)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — silence here is the failure
                logger.warning("log export pass failed; continuing", exc_info=True)
    finally:
        logexport.mark_exporter_stopped()


def _one_log_export_pass() -> int:
    """One export drain, session included. Runs inside a worker thread.

    Owns its session for the same reason `_one_background_pass` does: `asyncio.to_thread`
    cannot cancel the thread it started, so a session held outside could be closed underneath a
    drain that is still using it (GRPH-535).
    """
    from app.services import logexport

    db = SessionLocal()
    try:
        return logexport.run_once(db)
    finally:
        db.close()


async def _memory_judge_loop() -> None:
    """Judge memory rows off the request path, forever, without ever raising (GRPH-1242).

    The same three properties as `_credential_retry_loop`. Only the background leader starts
    it (GRPH-1241), and `memory.run_judge_refresh` also takes its own advisory lock, so a pass
    is single-runner even if two processes ever both believe they lead."""
    interval = settings.memory_judge_refresh_seconds
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_one_memory_judge_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — silence here is the failure
            logger.warning("memory judge refresh failed; continuing", exc_info=True)


def _one_memory_judge_pass() -> int:
    """One judge refresh, session included. Runs inside a worker thread; owns its session
    for the reason `_one_background_pass` does (GRPH-535)."""
    from app.services import memory as mem_svc

    db = SessionLocal()
    try:
        return mem_svc.run_judge_refresh(db)
    finally:
        db.close()


async def _auto_update_loop() -> None:
    """Run the auto-update tick forever without ever raising (GRPH-1299)."""
    interval = settings.auto_update_tick_seconds
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(_one_auto_update_pass)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — silence here is the failure
            logger.warning("auto-update pass failed; continuing", exc_info=True)


def _one_auto_update_pass() -> dict:
    """One tick, session included. Runs inside a worker thread (GRPH-535)."""
    from app.services import auto_update

    db = SessionLocal()
    try:
        return auto_update.tick(db)
    finally:
        db.close()


app = FastAPI(title="Graphban API", version=__version__, lifespan=lifespan)

@app.exception_handler(QuotaExceeded)
async def _quota_handler(_: Request, exc: QuotaExceeded):
    """A hosted plan limit hit via a REST route → HTTP 402 Payment Required. (The MCP
    dispatcher maps the same exception to a ``quota_exceeded`` tool error itself.)"""
    detail = str(exc) + (f" — {exc.hint}" if exc.hint else "")
    return JSONResponse(status_code=402, content={"detail": detail})


@app.exception_handler(Refused)
async def _refused_handler(request: Request, exc: Refused):
    """The one handler that records every REST refusal (GRPH-1158). Recording never breaks
    the response: `events.record` swallows its own failures."""
    from app.services import events as events_svc

    params = request.path_params
    # The matched route's own template (`path_format`), never rebuilt by substituting
    # parameter values back out of the concrete path, which broke when a value equalled a
    # literal segment or contained `/` (GRPH-1197). The route omits the include prefix, so
    # that is recovered as whatever the concrete path carries in front of the route's own
    # rendering. No matched route falls back to the concrete path.
    route = request.scope.get("route")
    template = getattr(route, "path_format", None)
    if template is None:
        template = request.url.path
    else:
        concrete = request.scope["path"]
        try:
            rendered = template.format(**params)
        except (KeyError, IndexError, ValueError):
            rendered = ""
        if rendered and concrete.endswith(rendered):
            template = concrete[: len(concrete) - len(rendered)] + template
    where = f"{request.method} {template}"
    target_type, target_id = "", ""
    if params.get("prd_id"):
        target_type, target_id = "prd", params["prd_id"]
    elif params.get("item_id"):
        target_type, target_id = "item", params["item_id"]
    if exc.target:
        target_type, target_id = exc.target
    db = SessionLocal()
    try:
        events_svc.record_refusal(
            db, rule_id=exc.rule_id, surface="rest", where=where, status=exc.status_code,
            detail=str(exc.detail),
            actor=getattr(request.state, "actor", ("system", "", "")),
            target_type=target_type, target_id=target_id, meta=exc.meta,
            project_id=params.get("project_id"))
    finally:
        db.close()
    body: dict = {"detail": exc.detail, "rule_id": exc.rule_id}
    if exc.hint:
        body["hint"] = exc.hint
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


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
app.include_router(cli_pair.router, prefix=API)
from app.routers import playbooks as playbooks_routes  # noqa: E402
app.include_router(playbooks_routes.router, prefix=API)
from app.routers import setup as setup_routes  # noqa: E402
app.include_router(setup_routes.router, prefix=API)
from app.routers import machines as machines_routes  # noqa: E402
app.include_router(machines_routes.router, prefix=API)
from app.routers import me_alerts as me_alerts_routes  # noqa: E402
app.include_router(me_alerts_routes.router, prefix=API)
app.include_router(projects.router, prefix=API)
app.include_router(items.router, prefix=API)
app.include_router(requests.router, prefix=API)
app.include_router(memory.router, prefix=API)
app.include_router(artifacts.router, prefix=API)
app.include_router(fleet.router, prefix=API)
app.include_router(harness.router, prefix=API)
app.include_router(live.router, prefix=API)
app.include_router(learning.router, prefix=API)
app.include_router(apikeys.router, prefix=API)
app.include_router(agent.router, prefix=API)
app.include_router(assistant.router, prefix=API)
app.include_router(prds.router, prefix=API)
app.include_router(analytics.router, prefix=API)
app.include_router(usage.router, prefix=API)
app.include_router(platform.router, prefix=API)
app.include_router(rules.router, prefix=API)
# Deployment settings with no project to be scoped by (PRD-47 S15). Aliased because the
# module name `settings` is already this process's configuration object, and shadowing it
# here would turn every `settings.foo` below into an AttributeError at import time.
from app.routers import settings as settings_routes
app.include_router(settings_routes.router, prefix=API)
app.include_router(public.router, prefix=API)
app.include_router(reports.router, prefix=API)
app.include_router(sync.router, prefix=API)
app.include_router(documents.router, prefix=API)
app.include_router(specs.router, prefix=API)
app.include_router(linear.router, prefix=API)
app.include_router(slice_pull.router, prefix=API)
app.include_router(mcp_router, prefix=API)
# The Organization layer is a hosted-SaaS surface only (AL-74). It's mounted here but
# every route is gated by a hosted-only dependency (see routers/orgs.require_hosted):
# with HOSTED_MODE off, every org/invite endpoint 404s, so self-host has no usable
# org surface. Gating per-request (vs. a build-time `if`) keeps the flag authoritative
# at runtime and lets the test suite exercise the surface under a monkeypatched flag.
app.include_router(orgs.router, prefix=API)
app.include_router(tracker_links.router, prefix=API)
# Operator plane (AL-91): hosted + platform-admin gated at the router level; every
# route 404s for tenants, so the surface is invisible outside the operator allowlist.
app.include_router(admin.router, prefix=API)
# PRD-43 D8: Host-header routing for public surfaces ({org}.graphban.dev/{path}/{surface}).
# Mounted at root level (no prefix), BEFORE the SPA catch-all. Only activates when
# hosted_mode is on and the Host header matches a known org subdomain.
from app.routers.public import host_router
app.include_router(host_router)


@app.get("/api/config")
def public_config():
    """Unauthenticated deploy flags the SPA needs before login to shape onboarding:
    whether this is a hosted (org) deployment and whether self-serve signup is open.
    Deliberately tiny — no secrets, just the switches that change the UI.

    `has_users` (GRPH-1581) lets the login page open on "Create your account" for the first
    visitor to a box with no users. A bare boolean: it says whether ANY account exists, never
    whose, so it is no account-enumeration oracle. A DB failure reads as True (the ordinary
    Sign in page) rather than raising."""
    from app.models import User

    try:
        with SessionLocal() as s:
            has_users = s.query(User.id).first() is not None
    except Exception:  # noqa: BLE001 — config must never raise
        has_users = True
    return {
        "hosted_mode": settings.hosted_mode,
        "signup_mode": settings.signup_mode,
        "has_users": has_users,
    }


from app.services import agent_calls as agent_calls_svc  # noqa: E402 — PRD-34 D18


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
        # The observed feed's retention sweep (PRD-34 D18). Runs on the write path and
        # swallows nothing silently: a count here is a table growing past retention.
        "agent_calls_sweep_failed": agent_calls_svc.SWEEP_FAILED,
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

#: nginx `location /assets/` and `location /` — asserted against the template in tests.
SPA_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
SPA_SHELL_CACHE_CONTROL = "no-cache"


def _safe_dist_file(dist: pathlib.Path, relative: str) -> pathlib.Path | None:
    """Resolve `relative` under `dist` and return the file, or None if outside or missing.

    nginx `root` + `try_files` never escapes the bundle directory. Without this check,
    `dist / "../.env"` resolves to a sibling file and the catch-all serves it (GRPH-577).
    """
    if not relative or relative.endswith("/"):
        return None
    if relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
        return None
    root = dist.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


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
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers.setdefault("Cache-Control", SPA_ASSET_CACHE_CONTROL)
        elif not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", SPA_SHELL_CACHE_CONTROL)
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
        from urllib.parse import unquote

        # An unmatched `/api/*` is a MISSING ENDPOINT, and must stay JSON. Returning the SPA
        # would hand an agent an HTML page where it expected an error object, and the 200
        # would read as success.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:
            relative = unquote(full_path)
            if relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            target = _safe_dist_file(dist, relative)
            if target is not None:
                return FileResponse(
                    target,
                    headers={"Cache-Control": SPA_SHELL_CACHE_CONTROL},
                )
        return FileResponse(
            dist / "index.html",
            headers={"Cache-Control": SPA_SHELL_CACHE_CONTROL},
        )


if _DIST.is_dir():  # pragma: no cover - exercised via `_mount_spa` against a fixture
    _mount_spa(app, _DIST)
