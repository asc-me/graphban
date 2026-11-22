"""Pull GitHub check-runs onto a box the internet cannot reach (GRPH-1822).

A ledger behind NAT never receives the push CI uses to post `suite_green`, so
an item with a linked pull request can never reach `done`. This loop reads
`GET /repos/{owner}/{repo}/commits/{sha}/check-runs` and writes the attestation
itself. Nothing accepts an inbound callback, and no key is minted: the `gate`
scope is enforced at the HTTP boundary, and this process is that authority.

Zero check-runs is not a green suite. A missing token, a 401 and a 404 are
not silence — `last_error` is what `/health` reports, and `refusal_clause` is
what the done gate appends. A head is polled at most every 60 seconds, less
often when `ci_pull_seconds` is higher, and not at all once a pass or a fail
is on the item.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Item
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
#: None: no pass has finished. "": the last pass read what it needed (or had
#: nothing to read). A string: the last pass could not, and why. Empty must not
#: be the only answer, because "no error" and "nobody looked" are different.
_last_error: str | None = None
_backoff_until = 0.0
_next_poll: dict[str, float] = {}
_observations: dict[str, str] = {}


def last_error() -> str | None:
    """The loop's last error. None if it has not completed a pass."""
    with _lock:
        return _last_error


def refusal_clause(commits: list[str]) -> str:
    """What the done gate should add when this loop has looked at `commits` and
    written no `suite_green`. Empty when it has not looked — the old sentence
    stays the whole refusal."""
    wanted = [(c or "").strip() for c in commits if (c or "").strip()]
    if not wanted:
        return ""
    with _lock:
        snapshot = list(_observations.items())
    for key, text in snapshot:
        sha = key.rsplit("@", 1)[-1]
        if any(items_svc.same_commit(sha, c) for c in wanted):
            return text
    return ""


def reset_state() -> None:
    """Forget poll times, observations and the last error. Tests, and a fresh process."""
    global _last_error, _backoff_until
    with _lock:
        _last_error = None
        _backoff_until = 0.0
        _next_poll.clear()
        _observations.clear()


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
        _set_last_error(f"the box could not read GitHub checks: {type(exc).__name__}")


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
            _blame_all(candidates, "the box could not read GitHub checks: no token is set")
            _set_last_error("the box could not read GitHub checks: no token is set")
        else:
            _set_last_error("")
        return
    if not candidates:
        _set_last_error("")
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
            _blame_groups(targets, fetched.problem)
            problems.append(fetched.problem)
            if fetched.stop or fetched.backoff:
                break
            continue
        verdict = judge(fetched.runs or [])
        if verdict.kind in ("pass", "fail"):
            for item, exact in groups[key]:
                _write(db, item, exact, verdict)
            _clear_observation(key)
        else:
            _blame_groups({key: groups[key]}, verdict.sentence)
    if looked:
        _set_last_error(problems[0] if problems else "")


class _Fetched:
    def __init__(self, *, runs: list | None = None, problem: str = "",
                 backoff: float = 0.0, stop: bool = False):
        self.runs = runs
        self.problem = problem
        self.backoff = backoff
        self.stop = stop


def _fetch(owner: str, repo: str, sha: str, token: str) -> _Fetched:
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
            return _Fetched(problem=f"the box could not read GitHub checks: GitHub answered {status}")
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


class _Verdict:
    def __init__(self, kind: str, detail: str = "", run_ref: str = "", sentence: str = ""):
        self.kind = kind
        self.detail = detail
        self.run_ref = run_ref
        self.sentence = sentence


def judge(runs: list) -> _Verdict:
    """What one head's check-runs say.

    `absent` is zero runs. That is not a pass: a repository with no checks
    configured would otherwise sail through the done gate. A run that has not
    completed — or a completed run with no conclusion — blocks a verdict even
    when a sibling has already failed, because the acceptance rule is that a
    still-running head writes nothing. The failure is recorded on the poll
    after every check has finished.
    """
    if not runs:
        return _Verdict("absent", sentence="no check-runs were found")
    if not all(_settled(run) for run in runs):
        return _Verdict("wait", sentence="GitHub checks for this head are still running")
    failing = [run for run in runs if _conclusion(run) in FAIL_CONCLUSIONS]
    if failing:
        detail = "failing check: " + ", ".join(
            f"{_name(run)} ({_conclusion(run)})" for run in failing)
        ref = next((str(run.get("html_url") or "") for run in failing
                    if str(run.get("html_url") or "")), "")
        return _Verdict("fail", detail=detail, run_ref=ref)
    ref = str((runs[0] or {}).get("html_url") or "") if isinstance(runs[0], dict) else ""
    return _Verdict("pass", detail=f"all {len(runs)} check-runs completed and none failed",
                    run_ref=ref)


def _settled(run) -> bool:
    if not isinstance(run, dict):
        return False
    return str(run.get("status") or "").lower() == "completed" and bool(_conclusion(run))


def _conclusion(run) -> str:
    return str(run.get("conclusion") or "").lower()


def _name(run) -> str:
    return str(run.get("name") or "check")


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


def _blame_all(candidates, sentence: str) -> None:
    groups: dict[str, list] = {}
    for item, exact, owner, repo in candidates:
        key = f"{owner.lower()}/{repo.lower()}@{exact.lower()}"
        groups.setdefault(key, []).append((item, exact))
    _blame_groups(groups, sentence)


def _blame_groups(groups: dict, sentence: str) -> None:
    with _lock:
        for key in groups:
            _observations[key] = sentence


def _clear_observation(key: str) -> None:
    with _lock:
        _observations.pop(key, None)


def _set_last_error(text: str) -> None:
    global _last_error
    with _lock:
        _last_error = text


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
