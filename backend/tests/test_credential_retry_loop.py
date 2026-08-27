"""The service's first background task, and the three ways it could take the app down.

This file is separate from `test_credential_retry.py` because it turns the loop back ON.
The suite runs with `CREDENTIAL_RETRY_SECONDS=0` — a timer firing mid-test turns an unrelated
assertion into a flake — so the tests that are ABOUT the task have to re-enable it explicitly
and drive a real app through a real lifespan.

**`test_the_app_serves_even_when_every_pass_raises` is the load-bearing one.** Everything else
here passes against a loop with no error handling at all: it only fails when the loop is
actually made to throw, which is the case a self-hosted install hits at 3am and nobody is
watching. "We catch exceptions" is a sentence; this is the evidence.
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

#: Real wall-clock waiting is required. The loop sleeps BEFORE its first pass, and a burst of
#: TestClient GETs completes in about five milliseconds — the first version of these tests
#: spun 40 requests inside one 0.05s interval and concluded the loop never ran.
DEADLINE = 5.0


def _wait_until(predicate, client=None) -> bool:
    began = time.monotonic()
    while time.monotonic() - began < DEADLINE:
        if predicate():
            return True
        if client is not None:
            client.get("/health")
        time.sleep(0.02)
    return predicate()


def _app_with_loop(monkeypatch, interval=0.05):
    """A fresh app whose lifespan starts the retry task. Reimports nothing — the setting is
    read inside `lifespan`, so patching it is enough."""
    from app import main
    from app.config import settings

    monkeypatch.setattr(settings, "credential_retry_seconds", interval)
    return main.app


# ---- the one that matters -----------------------------------------------------------------


def test_the_app_serves_even_when_every_pass_raises(monkeypatch, _clean_database):
    """THE POINT. A loop that throws must not reach `lifespan`.

    A background task whose exception escapes takes down every self-hosted install on startup,
    and it would do so on the deployments least able to diagnose it. The failure is forced on
    EVERY pass, not once, so a loop that dies quietly after its first error also fails here —
    that variant is arguably worse, because the app keeps serving while `pending_validation`
    rows silently stop being retried.
    """
    from app.services import credential_retry as cr

    calls = {"n": 0}

    def always_explodes(*a, **k):
        calls["n"] += 1
        raise RuntimeError("the database went away")

    monkeypatch.setattr(cr, "run_once", always_explodes)
    app = _app_with_loop(monkeypatch)

    with TestClient(app) as client:
        ran_twice = _wait_until(lambda: calls["n"] >= 2, client)
        r = client.get("/health")

        assert r.status_code == 200, "the app stopped serving after the loop raised"

    assert ran_twice and calls["n"] >= 2, (
        f"the loop ran {calls['n']} time(s) — it died after its first error instead of "
        "continuing, so retries would silently stop while the console still showed them scheduled"
    )


# ---- lifecycle ------------------------------------------------------------------------------


def test_shutdown_does_not_hang_on_the_task(monkeypatch, _clean_database):
    """Cancelling must actually end it. A loop that swallows `CancelledError` would keep the
    process alive on exit — and on a container that is a failed deploy, not a slow one.

    **Measured directly rather than wrapped in `asyncio.wait_for(asyncio.to_thread(...))`.**
    That construction looked safer and was worse: `to_thread` cannot cancel the thread it
    started, so a timeout abandoned a still-running thread instead of stopping it — and those
    threads are NON-DAEMON, joined at interpreter exit. The "timeout" moved the hang from this
    test to process shutdown, where it appeared as an xdist worker that never exited and a CI
    job stuck at 90% with no failing test to point at.

    Timing the block is enough: if shutdown blocks, the elapsed assertion is what fails, and
    nothing is left behind to strand the worker.
    """
    app = _app_with_loop(monkeypatch, interval=30.0)

    began = time.monotonic()
    with TestClient(app) as client:
        client.get("/health")
    elapsed = time.monotonic() - began

    assert elapsed < 15.0, (
        f"startup+shutdown took {elapsed:.1f}s against a 30s loop interval — cancellation is "
        "waiting out the sleep instead of interrupting it"
    )


def test_the_loop_is_not_started_when_disabled(monkeypatch, _clean_database):
    """`0` means off, and off has to mean no task — this is what the whole suite relies on."""
    from app.services import credential_retry as cr

    calls = {"n": 0}
    monkeypatch.setattr(cr, "run_once", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    app = _app_with_loop(monkeypatch, interval=0)

    with TestClient(app) as client:
        began = time.monotonic()
        while time.monotonic() - began < 1.0:
            client.get("/health")
            time.sleep(0.02)

    assert calls["n"] == 0, "the loop ran while disabled"


def test_the_loop_does_the_work(monkeypatch, _clean_database):
    """The counterpart to every negative test above: with the loop on and a due row present,
    an attempt actually happens. Without this, a loop that never fires would pass the other
    three tests in this file."""
    from app.services import credential_retry as cr

    calls = {"n": 0}
    real = cr.run_once
    monkeypatch.setattr(cr, "run_once",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1])
    app = _app_with_loop(monkeypatch)

    with TestClient(app) as client:
        _wait_until(lambda: calls["n"] >= 1, client)

    assert calls["n"] >= 1, "the loop never ran a pass"


def test_each_pass_opens_and_closes_its_own_session(monkeypatch, _clean_database):
    """A Session held for the process lifetime pins a connection and serves increasingly stale
    identity-map reads — the loop would keep seeing a credential's state as it was at boot."""
    import app.main as main

    opened = {"n": 0, "closed": 0}
    real_sessionmaker = main.SessionLocal

    class Tracking:
        def __call__(self, *a, **k):
            opened["n"] += 1
            s = real_sessionmaker(*a, **k)
            real_close = s.close

            def close():
                opened["closed"] += 1
                return real_close()

            s.close = close
            return s

    monkeypatch.setattr(main, "SessionLocal", Tracking())
    app = _app_with_loop(monkeypatch)

    with TestClient(app) as client:  # noqa: F841 — the context is what runs the event loop
        # TWO corrections, both found by sabotage surviving:
        #
        # 1. `lifespan` opens two sessions of its own (seeding, then apply_llm), so counting
        #    from zero passed before the loop had run at all — it measured startup.
        # 2. `/health` opens a session too. Driving the wait with requests counted 84 of them
        #    against the loop's 1, so a loop holding a single session forever still "passed".
        #
        # So: baseline AFTER startup, and wait WITHOUT making requests. The task runs on
        # TestClient's event-loop thread and needs no traffic to make progress.
        base_open, base_closed = opened["n"], opened["closed"]
        began = time.monotonic()
        while time.monotonic() - began < DEADLINE and opened["n"] - base_open < 3:
            time.sleep(0.02)

    loop_opened = opened["n"] - base_open
    loop_closed = opened["closed"] - base_closed

    assert loop_opened >= 3, (
        f"the loop opened {loop_opened} session(s) across several passes — it is reusing one, "
        "which pins a connection and serves an identity map that never refreshes"
    )
    assert loop_closed >= loop_opened - 1, (
        f"the loop opened {loop_opened} session(s) and closed {loop_closed} — it is holding "
        "one open across passes, pinning a connection and serving stale reads"
    )
