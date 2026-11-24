"""Pull GitHub CI results onto a box the internet cannot reach (GRPH-1822).

A ledger behind NAT never receives the push CI uses to post `suite_green`, so
an item with a linked pull request can never reach `done`. This loop reads
`GET /repos/{owner}/{repo}/commits/{sha}/check-runs` and writes the attestation
itself. Nothing accepts an inbound callback, and no key is minted: the `gate`
scope is enforced at the HTTP boundary, and this process is that authority.

Check-runs is not a route every token can read. GitHub offers no Checks
permission on a fine-grained personal access token, so one answers 403 with
`x-accepted-github-permissions: checks=read` no matter what it was granted, and
the only token that reads it is a classic one with `repo` — write access to
every private repo the owner has (GRPH-1921). So a 403 that is not a rate limit
falls through to `GET /actions/runs?head_sha={sha}` and each run's `/jobs`,
which Actions: Read does cover. Both routes write the same attestation under the
same adapter; a 403 on both names the missing permission rather than going quiet.

Zero check-runs is not a green suite. A missing token, a 401 and a 404 are
not silence — `last_error` is what `/health` reports, and `refusal_clause` is
what the done gate appends. A head is polled at most every 60 seconds, less
often when `ci_pull_seconds` is higher, and not at all once a pass or a fail
is on the item.

The loop runs in the leader process. `/health` and the done refusal run on
whichever worker the request hits, and a module global is true in one process
of N (`leader.leader_lock_held` can say "not this worker" and cannot carry the
sentence). The sentences live in `ci_pull_notes`. Poll timing stays in this
process: only the leader polls, and a successor that takes the lock starts
from a fresh clock.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CiPullNote, Item, utcnow
from app.services import items as items_svc

logger = logging.getLogger("graphban.ci_pull")

ADAPTER = "github-checks"
#: Planner's floor. `ci_pull_seconds` may only make the loop slower or turn it off.
POLL_FLOOR_SECONDS = 60
MAX_PAGES = 10
_PAGE_SIZE = 100
_FETCH_TIMEOUT = 10.0
_GH_PR = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/\d+",
    re.IGNORECASE,
)
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
#: Conclusions that are a finished red run. `startup_failure` is GitHub's name for
#: a job that died before it could report `failure`; leaving it out would record
#: that job as a pass.
FAIL_CONCLUSIONS = frozenset({
    "failure", "cancelled", "timed_out", "action_required", "startup_failure",
})

_lock = threading.Lock()
#: Poll clock for THIS process. The leader is the only writer; a new leader
#: starts from zero, which polls early rather than waiting out a dead clock.
_backoff_until = 0.0
_next_poll: dict[str, float] = {}
#: One row, not a sentence about a head. Missing row: no pass has finished.
#: A row whose body is "" : the last pass read what it needed. Those two are
#: different, and an empty string must not be the only answer.
LAST_ERROR_KEY = "__last_error__"
_UNREADABLE = "the box could not read the stored CI pull observation"


def last_error() -> str | None:
    """The loop's last error, as stored for every worker.

    None if no pass has finished. "" if the last pass had nothing it could not
    read. A string is why it could not. This process's memory is not the answer:
    a non-leader never ran the loop.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(CiPullNote, LAST_ERROR_KEY)
    except Exception:  # noqa: BLE001 — health must get a sentence, not a crash
        logger.warning("ci pull observation unreadable", exc_info=True)
        return _UNREADABLE
    finally:
        db.close()
    if row is None:
        return None
    return row.body


def refusal_clause(commits: list[str], db: Session | None = None) -> str:
    """What the done gate should add when this loop has looked at `commits` and
    written no `suite_green`. Empty when it has not looked — the old sentence
    stays the whole refusal.

    `db` is the caller's session. The note was committed by the leader, so any
    worker's session sees it; reading on the caller's connection is what keeps
    a SQLite write transaction from locking the lookup out.
    """
    wanted = [(c or "").strip() for c in commits if (c or "").strip()]
    if not wanted:
        return ""
    own = db is None
    if own:
        from app.db import SessionLocal
        db = SessionLocal()
    try:
        rows = db.scalars(
            select(CiPullNote).where(CiPullNote.key != LAST_ERROR_KEY)).all()
    except Exception:  # noqa: BLE001 — silence here is the old missing sentence
        logger.warning("ci pull observation unreadable", exc_info=True)
        return _UNREADABLE
    finally:
        if own and db is not None:
            db.close()
    for row in rows:
        sha = row.key.rsplit("@", 1)[-1]
        if any(items_svc.same_commit(sha, c) for c in wanted):
            return row.body
    return ""


def reset_state() -> None:
    """Forget poll times and the stored sentences. Tests, and a fresh process."""
    global _backoff_until
    with _lock:
        _backoff_until = 0.0
        _next_poll.clear()
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.execute(delete(CiPullNote))
        db.commit()
    finally:
        db.close()


def run_once(db: Session, *, now: float | None = None) -> None:
    """One pass. Never raises: a GitHub fault must not kill the loop.

    `now` is a monotonic clock in seconds. Tests pass it; production uses
    `time.monotonic`, the same clock the poll floor and the backoff are stored on.
    """
    clock = time.monotonic() if now is None else now
    try:
        _poll(db, clock)
    except Exception as exc:  # noqa: BLE001 — silence here is the failure
        logger.warning("ci pull pass failed", exc_info=True)
        try:
            _set_last_error(db, f"the box could not read GitHub checks: {type(exc).__name__}",
                            recover=True)
        except Exception:  # noqa: BLE001 — the pass already failed; storing it must not raise
            logger.warning("ci pull could not store the error", exc_info=True)


def _poll_gap() -> float:
    """Seconds between polls of one head. Never under the floor; `0` still means
    the loop is off, which the caller in `main` honours before it gets here."""
    try:
        configured = float(settings.ci_pull_seconds or 0)
    except (TypeError, ValueError):
        configured = 0.0
    return float(POLL_FLOOR_SECONDS if configured <= 0 else max(POLL_FLOOR_SECONDS, configured))


def _poll(db: Session, clock: float) -> None:
    if clock < _read_backoff():
        return
    candidates = _candidates(db)
    token = (settings.github_token or "").strip()
    if not token:
        # Unauthenticated REST is 60 calls/hour shared with the update check.
        # Falling through to it would spend that budget and, on a private repo,
        # look the same as a green silence. No token is a stated reason.
        if candidates:
            _blame_all(db, candidates, "the box could not read GitHub checks: no token is set")
            _set_last_error(db, "the box could not read GitHub checks: no token is set")
        else:
            _set_last_error(db, "")
        return
    if not candidates:
        _set_last_error(db, "")
        return

    groups: dict[str, list[tuple[Item, str]]] = {}
    order: list[str] = []
    repos: dict[str, tuple[str, str, str]] = {}
    for item, exact, owner, repo in candidates:
        key = f"{owner.lower()}/{repo.lower()}@{exact.lower()}"
        if key not in groups:
            groups[key] = []
            order.append(key)
            repos[key] = (owner, repo, exact.lower())
        groups[key].append((item, exact))

    problems: list[str] = []
    looked = False
    for key in order:
        if not _due(key, clock):
            continue
        looked = True
        owner, repo, sha = repos[key]
        fetched = _fetch(owner, repo, sha, token)
        _mark_polled(key, clock)
        if fetched.backoff:
            _set_backoff(clock + fetched.backoff)
        if fetched.problem:
            # A 401 is the token, so every head still waiting gets the same
            # sentence. Heads already attested in this pass are left alone.
            if fetched.stop:
                seen = False
                targets = {}
                for later in order:
                    if later == key:
                        seen = True
                    if seen:
                        targets[later] = groups[later]
            else:
                targets = {key: groups[key]}
            _blame_groups(db, targets, fetched.problem)
            problems.append(fetched.problem)
            if fetched.stop or fetched.backoff:
                break
            continue
        verdict = judge(fetched.runs or [], noun=fetched.noun, one=fetched.one,
                        run_ref=fetched.run_ref)
        if verdict.kind in ("pass", "fail"):
            for item, exact in groups[key]:
                _write(db, item, exact, verdict)
            _clear_observation(db, key)
        else:
            _blame_groups(db, {key: groups[key]}, verdict.sentence)
    if looked:
        _set_last_error(db, problems[0] if problems else "")


class _Fetched:
    def __init__(self, *, runs: list | None = None, problem: str = "",
                 backoff: float = 0.0, stop: bool = False, forbidden: bool = False,
                 noun: str = "check-runs", one: str = "check", run_ref: str = ""):
        self.runs = runs
        self.problem = problem
        self.backoff = backoff
        self.stop = stop
        #: A 403 that is not a rate limit: the token cannot read THIS route, so
        #: the other one is worth trying. Any other status is the answer.
        self.forbidden = forbidden
        #: What the entries in `runs` are. The Actions route reads jobs, and a
        #: sentence that calls a job a check-run sends the owner to the wrong
        #: permission to fix.
        self.noun = noun
        self.one = one
        self.run_ref = run_ref


#: The one thing a fine-grained token has for a 403 on BOTH routes is a missing
#: permission, so the sentence names what to grant instead of reporting two
#: opaque statuses the owner would have to decode on api.github.com.
_BOTH_REFUSED = (
    "the box could not read GitHub checks: the token can read neither check-runs "
    "nor Actions runs (a fine-grained token needs Actions: Read on the repository)")


def _fetch(owner: str, repo: str, sha: str, token: str) -> _Fetched:
    """One head, over whichever route this token can read.

    Check-runs first, and Actions only when check-runs answers a 403 that is not
    a rate limit. Check-runs is the richer answer — it carries checks posted by
    other apps, not only this repository's own workflows — and a classic `repo`
    token reads it, so nothing that works today changes route. A fine-grained
    token has no Checks permission it could be granted, so its 403 means "wrong
    route" and not "something is wrong"; Actions: Read is the route that answers.
    A 403 that falls through is not a problem to report, so it never reaches
    `last_error` while Actions answers.
    """
    checks = _fetch_check_runs(owner, repo, sha, token)
    if not checks.forbidden:
        return checks
    actions = _fetch_actions(owner, repo, sha, token)
    if actions.problem:
        return _Fetched(problem=_BOTH_REFUSED if actions.forbidden else actions.problem,
                        backoff=actions.backoff, stop=actions.stop)
    return actions


def _fetch_check_runs(owner: str, repo: str, sha: str, token: str) -> _Fetched:
    collected: list = []
    total: int | None = None
    last_headers: dict = {}
    for page in range(1, MAX_PAGES + 1):
        url = (f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs"
               f"?per_page={_PAGE_SIZE}&page={page}")
        try:
            resp = httpx.get(url, headers=_headers(token), timeout=_FETCH_TIMEOUT)
        except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
            return _Fetched(problem=f"the box could not read GitHub checks: {type(exc).__name__}")
        headers = _header_map(resp)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 401:
            return _Fetched(problem="the box could not read GitHub checks: GitHub answered 401",
                            stop=True)
        if status == 404:
            return _Fetched(problem="the box could not read GitHub checks: GitHub answered 404")
        if status in (403, 429):
            wait = _backoff_seconds(headers)
            if status == 429 or wait:
                return _Fetched(
                    problem="the box could not read GitHub checks: rate limited",
                    backoff=wait or 60.0)
            return _Fetched(
                problem=f"the box could not read GitHub checks: GitHub answered {status}",
                forbidden=status == 403)
        if status != 200:
            return _Fetched(problem=f"the box could not read GitHub checks: GitHub answered {status}")
        try:
            body = resp.json()
        except (ValueError, TypeError):
            return _Fetched(problem="the box could not read GitHub checks: bad response")
        if not isinstance(body, dict):
            return _Fetched(problem="the box could not read GitHub checks: bad response")
        batch = body.get("check_runs")
        if not isinstance(batch, list):
            return _Fetched(problem="the box could not read GitHub checks: bad response")
        if total is None and isinstance(body.get("total_count"), int) \
                and not isinstance(body.get("total_count"), bool):
            total = body["total_count"]
        collected.extend(batch)
        last_headers = headers
        if total is not None and len(collected) >= total:
            break
        if not batch:
            break
        if total is None and len(batch) < _PAGE_SIZE:
            break
    else:
        if (total is not None and len(collected) < total) or (
                total is None and len(collected) >= _PAGE_SIZE * MAX_PAGES):
            return _Fetched(
                problem="the box could not read GitHub checks: the check-run list was truncated")
    # A 200 that exhausts the budget is still an answer. The backoff only stops
    # the NEXT head, so this page is not thrown away.
    return _Fetched(runs=collected, backoff=_backoff_seconds(last_headers))


def _send(url: str, token: str):
    """One authenticated GET: `((body, headers), None)` on a 200 carrying a JSON
    object, else `(None, _Fetched)` holding the sentence for what GitHub answered."""
    try:
        resp = httpx.get(url, headers=_headers(token), timeout=_FETCH_TIMEOUT)
    except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
        return None, _Fetched(
            problem=f"the box could not read GitHub checks: {type(exc).__name__}")
    headers = _header_map(resp)
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 200:
        try:
            body = resp.json()
        except (ValueError, TypeError):
            return None, _Fetched(problem="the box could not read GitHub checks: bad response")
        if not isinstance(body, dict):
            return None, _Fetched(problem="the box could not read GitHub checks: bad response")
        return (body, headers), None
    if status == 401:
        return None, _Fetched(
            problem="the box could not read GitHub checks: GitHub answered 401", stop=True)
    if status == 404:
        return None, _Fetched(problem="the box could not read GitHub checks: GitHub answered 404")
    if status in (403, 429):
        wait = _backoff_seconds(headers)
        if status == 429 or wait:
            return None, _Fetched(
                problem="the box could not read GitHub checks: rate limited",
                backoff=wait or 60.0)
    return None, _Fetched(
        problem=f"the box could not read GitHub checks: GitHub answered {status}",
        forbidden=status == 403)


def _fetch_actions(owner: str, repo: str, sha: str, token: str) -> _Fetched:
    """The Actions route: the head's workflow runs, then each run's jobs.

    A job carries the same `status`, `conclusion`, `name` and `html_url` a
    check-run does, so `judge` reads the two alike — `startup_failure` included,
    which GitHub uses for a job that died before it could report `failure`.
    """
    base = f"https://api.github.com/repos/{owner}/{repo}/actions"
    query = f"head_sha={sha}&per_page={_PAGE_SIZE}"
    runs: list = []
    url = f"{base}/runs?{query}"
    for page in range(2, MAX_PAGES + 2):
        got, fault = _send(url, token)
        if fault is not None:
            return fault
        body, _ = got
        batch = body.get("workflow_runs")
        if not isinstance(batch, list):
            return _Fetched(problem="the box could not read GitHub checks: bad response")
        runs.extend(batch)
        total = body.get("total_count")
        if not batch:
            break
        if isinstance(total, int) and not isinstance(total, bool):
            # A short page is not the end while `total_count` says there is more,
            # exactly as the check-runs pagination reads it.
            if len(runs) >= total:
                break
        elif len(batch) < _PAGE_SIZE:
            break
        url = f"{base}/runs?{query}&page={page}"
    else:
        # Judging a page-worth of a longer list could miss the run that failed.
        return _Fetched(
            problem="the box could not read GitHub checks: the workflow-run list was truncated")
    if not runs:
        # Not "no jobs": nothing ran at this head at all. The owner reads this
        # sentence on the done refusal, and the two have different fixes.
        return _Fetched(runs=[], noun="workflow runs")

    entries: list = []
    ref = ""
    for run in runs:
        if not isinstance(run, dict):
            continue
        batch: list = []
        run_id = run.get("id")
        if run_id is not None:
            got, fault = _send(f"{base}/runs/{run_id}/jobs?per_page={_PAGE_SIZE}", token)
            if fault is not None:
                return fault
            body, _ = got
            jobs = body.get("jobs")
            if not isinstance(jobs, list):
                return _Fetched(problem="the box could not read GitHub checks: bad response")
            total_jobs = body.get("total_count")
            if isinstance(total_jobs, int) and not isinstance(total_jobs, bool) \
                    and len(jobs) < total_jobs:
                return _Fetched(
                    problem="the box could not read GitHub checks: the job list was truncated")
            batch = jobs
        if not batch:
            # A run with no job yet — queued, or an id GitHub did not return — is
            # still running. Judging the run itself keeps the head from reading
            # as an empty, and therefore green, list of jobs.
            entries.append(run)
            continue
        entries.extend(batch)
        if not ref and any(_conclusion(job) in FAIL_CONCLUSIONS for job in batch):
            ref = str(run.get("html_url") or "")
    if not ref:
        first = next((run for run in runs if isinstance(run, dict)), {})
        ref = str(first.get("html_url") or "")
    return _Fetched(runs=entries, noun="jobs", one="job", run_ref=ref)


class _Verdict:
    def __init__(self, kind: str, detail: str = "", run_ref: str = "", sentence: str = ""):
        self.kind = kind
        self.detail = detail
        self.run_ref = run_ref
        self.sentence = sentence


def judge(runs: list, noun: str = "check-runs", one: str = "check",
          run_ref: str = "") -> _Verdict:
    """What one head's entries say. Both routes are judged here, alike.

    `noun` and `one` name what the entries are — check-runs, or Actions jobs —
    because the sentences they build are what an owner reads on a done refusal,
    and "no check-runs were found" would send a fine-grained token's owner back
    to a permission that does not exist. `run_ref` overrides the URL derived from
    the entries: the Actions route sets it to the workflow run, since a job's own
    `html_url` is a fragment of it.

    `absent` is zero entries. That is not a pass: a repository with no checks
    configured would otherwise sail through the done gate. An entry that has not
    completed — or a completed one with no conclusion — blocks a verdict even
    when a sibling has already failed, because the acceptance rule is that a
    still-running head writes nothing. The failure is recorded on the poll
    after every entry has finished.
    """
    if not runs:
        return _Verdict("absent", sentence=f"no {noun} were found")
    if not all(_settled(run) for run in runs):
        return _Verdict("wait", sentence="GitHub checks for this head are still running")
    failing = [run for run in runs if _conclusion(run) in FAIL_CONCLUSIONS]
    if failing:
        detail = f"failing {one}: " + ", ".join(
            f"{_name(run, one)} ({_conclusion(run)})" for run in failing)
        ref = run_ref or next((str(run.get("html_url") or "") for run in failing
                               if str(run.get("html_url") or "")), "")
        return _Verdict("fail", detail=detail, run_ref=ref)
    ref = run_ref or (
        str((runs[0] or {}).get("html_url") or "") if isinstance(runs[0], dict) else "")
    return _Verdict("pass", detail=f"all {len(runs)} {noun} completed and none failed",
                    run_ref=ref)


def _settled(run) -> bool:
    if not isinstance(run, dict):
        return False
    return str(run.get("status") or "").lower() == "completed" and bool(_conclusion(run))


def _conclusion(run) -> str:
    return str(run.get("conclusion") or "").lower()


def _name(run, default: str = "check") -> str:
    return str(run.get("name") or default)


def _candidates(db: Session) -> list[tuple[Item, str, str, str]]:
    """Review items with a linked GitHub PR, a recorded head, and no `suite_green` yet."""
    rows = db.scalars(select(Item).where(Item.status == "review")).all()
    out = []
    for item in rows:
        url = _pull_url(item)
        match = _GH_PR.search(url or "")
        if match is None:
            continue
        sha = _head(item)
        if not sha or _answered(item, sha):
            continue
        out.append((item, sha, match.group(1), match.group(2)))
    return out


def _pull_url(item) -> str:
    recorded = items_svc.recorded_pr_url(item)
    if _GH_PR.search(recorded or ""):
        return recorded
    for entry in item.evidence or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        if items_svc.is_pr_url(url) and _GH_PR.search(url):
            return url
    return ""


def _head(item) -> str:
    recorded = (item.head_commit or "").strip()
    if _SHA.fullmatch(recorded):
        return recorded
    for commit in items_svc.pr_commits(item.evidence):
        if _SHA.fullmatch(commit):
            return commit
    return ""


def _answered(item, sha: str) -> bool:
    """A pass or a fail at this head. Either one is the answer; stop polling."""
    for receipt in items_svc.attestation_receipts(item.evidence):
        if str(receipt.get("commit") or "") != sha:
            continue
        for pred in receipt.get("predicates") or []:
            if isinstance(pred, dict) and pred.get("name") == items_svc.CI_PREDICATE:
                return True
    return False


def _write(db: Session, item: Item, sha: str, verdict: _Verdict) -> None:
    if _answered(item, sha):
        return
    # Direct service write, not a minted gate key and not an endpoint. The
    # boundary that demands `gate` is what stops an agent from forging this;
    # the process that enforces the gate is allowed to record what it read.
    items_svc.update_item(db, item.id, evidence=[{
        "kind": "attestation",
        "adapter": ADAPTER,
        "commit": sha,
        "run_ref": verdict.run_ref,
        "predicates": [{
            "name": items_svc.CI_PREDICATE,
            "passed": verdict.kind == "pass",
            "detail": verdict.detail,
        }],
    }])


def _headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "graphban-api",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _header_map(resp) -> dict:
    raw = getattr(resp, "headers", None) or {}
    try:
        pairs = dict(raw).items()
    except (TypeError, ValueError):
        pairs = []
    return {str(key).lower(): str(value) for key, value in pairs}


def _backoff_seconds(headers: dict) -> float:
    """Seconds to wait, from Retry-After or a spent `X-RateLimit-Remaining`. 0 if neither."""
    try:
        retry = float(headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        retry = 0.0
    if retry > 0:
        return retry
    if str(headers.get("x-ratelimit-remaining") or "") != "0":
        return 0.0
    try:
        reset = float(headers.get("x-ratelimit-reset") or 0)
    except (TypeError, ValueError):
        return 0.0
    delay = reset - time.time()
    return delay if delay > 0 else 0.0


def _blame_all(db: Session, candidates, sentence: str) -> None:
    groups: dict[str, list] = {}
    for item, exact, owner, repo in candidates:
        key = f"{owner.lower()}/{repo.lower()}@{exact.lower()}"
        groups.setdefault(key, []).append((item, exact))
    _blame_groups(db, groups, sentence)


def _blame_groups(db: Session, groups: dict, sentence: str) -> None:
    for key in groups:
        _upsert(db, key, sentence)
    db.commit()


def _clear_observation(db: Session, key: str) -> None:
    row = db.get(CiPullNote, key)
    if row is not None:
        db.delete(row)
        db.commit()


def _set_last_error(db: Session, text: str, *, recover: bool = False) -> None:
    if recover:
        db.rollback()
    _upsert(db, LAST_ERROR_KEY, text)
    db.commit()


def _upsert(db: Session, key: str, body: str) -> None:
    row = db.get(CiPullNote, key)
    if row is None:
        db.add(CiPullNote(key=key, body=body, updated_at=utcnow()))
    else:
        row.body = body
        row.updated_at = utcnow()


def _read_backoff() -> float:
    with _lock:
        return _backoff_until


def _set_backoff(until: float) -> None:
    global _backoff_until
    with _lock:
        _backoff_until = max(_backoff_until, until)


def _due(key: str, clock: float) -> bool:
    with _lock:
        if clock < _backoff_until:
            return False
        return clock >= _next_poll.get(key, 0.0)


def _mark_polled(key: str, clock: float) -> None:
    with _lock:
        _next_poll[key] = clock + _poll_gap()
