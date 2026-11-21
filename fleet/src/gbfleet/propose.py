"""Propose a reaped branch for merge (GRPH-804).

The other half of "done does not mean merged". GRPH-798 made the supervisor HOLD an item whose
dependency's commit is not in the base; this is what makes that hold clear on its own, because
nothing in the loop ever proposed the merge.

Reported from super-arc: SA-417 reached `done` with its commit only on `origin/gb/p11-m1-4`,
while SA-416 — which only reached `review` — did have a PR. **The more complete item is the one
that went missing**, which is the wrong way round and is what makes this worth automating
rather than remembering.

A DRAFT, deliberately. The supervisor knows the work exists and is pushed; it does not know
whether anybody wants it reviewed, and opening a review request nobody asked for is a claim on
a person's attention. A draft proposes without demanding.

Shells out to `gh` rather than speaking to an API: `gbfleet` holds no forge credential and
should not start. `gh` is the operator's own login, so the PR is opened as the human who ran
the wave — which is also the honest attribution.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

INSTALL = "brew install gh   # or see https://cli.github.com"


@dataclass(frozen=True)
class Proposed:
    """What became of the attempt to propose a branch.

    `skipped` is a real outcome and is not `ok`, the same distinction `Pushed` makes and for
    the same reason: a branch that already has a PR and a branch nobody could propose must not
    look alike.
    """

    branch: str
    url: str = ""
    ok: bool = False
    skipped: bool = False
    reason: str = ""


def find() -> str:
    return shutil.which("gh") or ""


def _gh(repo: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True,
                          timeout=timeout)


def _gh_text(done: subprocess.CompletedProcess) -> str:
    return f"{done.stderr or ''}\n{done.stdout or ''}"


def _graphql_rate_limited(done: subprocess.CompletedProcess) -> bool:
    """True when `gh` died on a GraphQL rate-limit line.

    `gh pr create` and the lookups before it speak GraphQL. A secondary limit there leaves
    the REST quota intact (GRPH-1752). A nonzero exit with that text is not "no such PR":
    reading it as empty is how a branch that already has a PR gets a sibling, and how a
    create that only the GraphQL side refused never gets opened.
    """
    if done.returncode == 0:
        return False
    for line in _gh_text(done).splitlines():
        low = line.lower()
        if "graphql" in low and "rate limit" in low:
            return True
    return False


def _rate_limit_line(done: subprocess.CompletedProcess) -> str:
    for line in _gh_text(done).splitlines():
        if "rate limit" in line.lower():
            return line.strip()[:200]
    return "GraphQL rate limit"


def _limited(branch: str, what: str, line: str) -> Proposed:
    """A lookup that died on the rate limit. Not ok, and not a reason to open a PR."""
    return Proposed(branch=branch, reason=f"gh {what} rate limited: {line}")


def existing(repo: Path, branch: str) -> str:
    """The URL of a PR already open for this branch, or "".

    Asked FIRST, every time. `gh pr create` on a branch that already has one fails, and a
    failure there would read as "could not propose" for work that was proposed perfectly well
    an hour ago — the wave would report a problem that is not one.
    """
    return _existing(repo, branch)[0]


def _existing(repo: Path, branch: str) -> tuple[str, str]:
    """`(url, rate-limit line)`. The line is set only for a GraphQL rate limit.

    Any other failure is `("", "")` — the "no open PR" `existing` has always returned.
    `propose` must not treat the rate-limit line as that (GRPH-1752).
    """
    done = _gh(repo, "pr", "view", branch, "--json", "url")
    if _graphql_rate_limited(done):
        return "", _rate_limit_line(done)
    if done.returncode != 0:
        return "", ""
    try:
        return str(json.loads(done.stdout or "{}").get("url") or ""), ""
    except ValueError:
        return "", ""


#: A check-rollup entry that counts as passing. Anything else (pending, failed, cancelled)
#: is not green, and a PR with no checks at all is not green either (GRPH-1391).
_PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}


#: A check-rollup entry still running. Anything neither passing nor this is a failure.
_RUNNING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING", "REQUESTED"}


def is_actions_job(check: dict) -> bool:
    """True when this rollup entry is a GitHub Actions job (GRPH-1757).

    `gh pr view --json statusCheckRollup` (cli/cli `api/export_pr.go`) writes a CheckRun
    with `workflowName` taken from the Actions workflow, and a StatusContext with
    `context`/`state`/`targetUrl` and no `workflowName` key. A third-party check run
    still has the key, and the value is empty. An entry that only has `name` — the shape
    this repo's own tests and older callers use for an Actions job — has no
    `workflowName` key at all, so a missing key stays an Actions job. An empty string is
    the third-party check run, unless its URL is an Actions job page.
    """
    if not isinstance(check, dict):
        return False
    if "workflowName" in check:
        if str(check.get("workflowName") or "").strip():
            return True
        url = str(check.get("detailsUrl") or "")
        return "/actions/runs/" in url or "/actions/jobs/" in url
    if str(check.get("__typename") or "") == "StatusContext":
        return False
    if check.get("context") and not check.get("name"):
        return False
    return bool(check.get("name"))


def _check_verdict(check: dict) -> str:
    return str(check.get("conclusion") or check.get("state") or "").upper()


def _check_name(check: dict) -> str:
    return str(check.get("name") or check.get("context") or "unknown check")


def _branch_path(base: str) -> str:
    """Percent-encode a branch name for one `gh api` path segment.

    Unreserved characters (RFC 3986) stay as themselves. A slash in
    `release/2026.11.30` becomes `%2F`, which is what the rules route expects.
    The encoding lives here: this module may not import a network library, and
    the path parser that would do it sits in one (`test_only_the_named_doors_make_requests`).
    """
    out: list[str] = []
    for byte in base.encode("utf-8"):
        if (48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122
                or byte in b"-._~"):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def required_contexts(repo: Path, base: str) -> set[str] | None:
    """Status-check contexts the base branch requires, or None when the read failed.

    An empty set means the rules endpoint answered and none are configured. None means
    nobody could look — a 404, a non-JSON body, a missing base — and must not be read as
    "nothing is required". `gh api repos/{owner}/{repo}/rules/branches/{base}` is the
    aggregated rules view (classic protection and rulesets). `base` is percent-encoded
    so a slash in the branch name stays one path segment.
    """
    if not base or not find():
        return None
    try:
        done = _gh(repo, "api", f"repos/{{owner}}/{{repo}}/rules/branches/{_branch_path(base)}")
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        rules = json.loads(done.stdout or "")
    except ValueError:
        return None
    if not isinstance(rules, list):
        return None
    names: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        checks = params.get("required_status_checks") if isinstance(params, dict) else None
        if not isinstance(checks, list):
            continue
        for entry in checks:
            if isinstance(entry, str) and entry:
                names.add(entry)
            elif isinstance(entry, dict) and entry.get("context"):
                names.add(str(entry["context"]))
    return names


def ci_rollup(repo: Path, branch: str) -> tuple[str, str, str]:
    """(state, head, failing_job) for `branch`'s PR — the one reading of its check rollup.

    state is `green` (every check passed), `pending` (no repo-CI failure, some still
    running), `red` (an Actions job failed, or a required check failed — `failing_job`
    names it), `external` (every Actions job passed and the only failures are third-party
    statuses or other non-required checks — `failing_job` names those; GRPH-1757), or
    `unknown`: no `gh`, no PR, an unreadable answer, or a PR with NO checks. No checks is
    unknown rather than pending (GRPH-1392): a repo without CI would otherwise wait on it
    forever.

    `external` is not green. `ci_green` stays false while a third-party check is red.
    `ci_rollup.required_note` is "" after a read that could see the required set, and
    `required checks unread` when that read failed — an empty note is not "we looked and
    none are required" unless the rules call itself succeeded.
    """
    ci_rollup.required_note = ""
    if not branch or not find():
        return "unknown", "", ""
    try:
        done = _gh(repo, "pr", "view", branch, "--json",
                   "headRefOid,baseRefName,statusCheckRollup")
    except (OSError, subprocess.SubprocessError):
        return "unknown", "", ""
    if done.returncode != 0:
        return "unknown", "", ""
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return "unknown", "", ""
    head = str(data.get("headRefOid") or "")
    rollup = data.get("statusCheckRollup") or []
    if not head or not isinstance(rollup, list) or not rollup:
        return "unknown", head, ""
    actions_failed: list[str] = []
    actions_running = False
    external_failed: list[str] = []
    external_running = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        verdict = _check_verdict(check)
        name = _check_name(check)
        if is_actions_job(check):
            if verdict in _PASSING:
                continue
            if verdict in _RUNNING:
                actions_running = True
                continue
            actions_failed.append(name)
            continue
        if verdict in _PASSING:
            continue
        if verdict in _RUNNING:
            external_running = True
            continue
        external_failed.append(name)
    if actions_failed:
        return "red", head, actions_failed[0]
    required: set[str] | None = None
    if external_failed:
        required = required_contexts(repo, str(data.get("baseRefName") or ""))
        if required is None:
            # Looked and could not tell. Do not promote the external failure into repo CI:
            # that is the bounce GRPH-1757 removes. The note says the set was unread.
            ci_rollup.required_note = "required checks unread"
        else:
            required_hit = [name for name in external_failed if name in required]
            if required_hit:
                return "red", head, required_hit[0]
    if actions_running or (external_running and not external_failed):
        return "pending", head, ""
    if external_failed:
        return "external", head, ", ".join(external_failed)
    return "green", head, ""


ci_rollup.required_note = ""


def ci_green(repo: Path, branch: str, head: str) -> bool:
    """True when `branch` has a PR whose head is `head` and every check on it passed.

    The full-suite receipt: CI already carries the SQLite and Postgres shards. Fails closed:
    no `gh`, no PR, a different head, no checks, or any check not passing all answer False.
    """
    if not head:
        return False
    state, at, _ = ci_rollup(repo, branch)
    return state == "green" and at == head


def existing_any(repo: Path, branch: str) -> dict:
    """The newest PR for this branch in any state, or {}.

    `gh pr view <branch>` only finds OPEN PRs. After a bounce the PR is often closed, and
    creating a sibling for the same head is how GRPH-1031's reported waves grew #639 then
    #643 for one item. A closed or merged PR is still this branch's PR: update it, or say
    why not — do not open another.
    """
    return _existing_any(repo, branch)[0]


def _existing_any(repo: Path, branch: str) -> tuple[dict, str]:
    """`(pr, rate-limit line)`. The line is set only for a GraphQL rate limit (GRPH-1752).

    Any other failure is `({}, "")`, which is what `existing_any` has always returned
    when `gh` did not exit zero. `propose` must not open a PR on the rate-limit line.
    """
    done = _gh(repo, "pr", "list", "--head", branch, "--state", "all",
               "--json", "url,state,number", "--limit", "20")
    if _graphql_rate_limited(done):
        return {}, _rate_limit_line(done)
    if done.returncode != 0:
        return {}, ""
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return {}, ""
    if not isinstance(rows, list):
        return {}, ""
    found = [r for r in rows if isinstance(r, dict) and r.get("url")]
    if not found:
        return {}, ""
    order = {"OPEN": 0, "CLOSED": 1, "MERGED": 2}
    found.sort(key=lambda r: order.get(str(r.get("state") or "").upper(), 9))
    row = found[0]
    return {
        "url": str(row.get("url") or ""),
        "state": str(row.get("state") or "").upper(),
        "number": row.get("number"),
    }, ""


def _listed(repo: Path, state: str, fields: str) -> list[dict]:
    """`gh pr list --state <state>` rows, or [] when it cannot be read."""
    return _listed_checked(repo, state, fields)[0]


def _listed_checked(repo: Path, state: str, fields: str) -> tuple[list[dict], str]:
    """`(rows, rate-limit line)`. [] on any other unreadable answer (GRPH-1752).

    A listing that cannot be read still answers [] to callers of `open_prs` /
    `merged_prs`. The line is how `propose` tells "GitHub said nothing" from "GitHub
    refused to say", and only the second one stops a create.
    """
    done = _gh(repo, "pr", "list", "--state", state, "--limit", "200", "--json", fields)
    if _graphql_rate_limited(done):
        return [], _rate_limit_line(done)
    if done.returncode != 0:
        return [], ""
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return [], ""
    return ([r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []), ""


def open_prs(repo: Path) -> list[dict]:
    """Every open PR with what is needed to tell whose it is, or [] when it cannot be read."""
    return _listed(repo, "open", "number,url,headRefName,headRefOid,isDraft,title,body")


def merged_prs(repo: Path) -> list[dict]:
    """Every MERGED PR with the head that landed, or [] when it cannot be read (GRPH-1709).

    Keyed on the COMMIT where `existing_any` is keyed on the BRANCH. That is the difference
    this exists for: `existing` is OPEN-only and `existing_any` answers {} for ANY `gh` that
    did not exit zero — which is what one rate limit shared by every drain on the account
    produces — so both can say "nothing here" about a head that landed an hour ago. A
    listing that cannot be read answers [] rather than "nothing merged", so this only ever
    narrows what `propose` is willing to open; it never widens it.
    """
    return _listed(repo, "merged", "number,url,headRefName,headRefOid")


def _pr_number(url: str) -> int:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _ownership_lines(body: str) -> list[str]:
    """`Items:` / `Attests:` lines. Empty when the body does not declare ownership."""
    return [line for line in str(body or "").splitlines()
            if line.startswith(("Items:", "Attests:"))]


def _carries_item(pr: dict, item: str) -> bool:
    """Whether the PR is `item`'s.

    An `Items:` or `Attests:` line is the declared ownership, and it outranks the title
    (GRPH-1452). A merge title "Merge origin/main (A) into B" names A without being A's
    PR; closing it when A merges drops B's only open PR. The title counts only when the
    body has no such line. A passing mention elsewhere in the body is not ownership.
    """
    lines = _ownership_lines(str(pr.get("body") or ""))
    if lines:
        return any(_names(line, item) for line in lines)
    return _names(str(pr.get("title") or ""), item)


def _oid(value: str) -> str:
    """A hex commit id, or "". Anything else must not be interpolated into a compare URL."""
    text = str(value or "").strip().lower()
    if len(text) < COMMIT_MIN or len(text) > 40:
        return ""
    if not all(c in "0123456789abcdef" for c in text):
        return ""
    return text


def _keeper_head(repo: Path, number: int) -> str:
    """The keeper PR's head commit, or "" when it cannot be read.

    Unreadable is not "this other branch is contained". Callers treat "" as "could not
    look", and only a compared `ahead_by` above zero refuses the close.
    """
    if not number:
        return ""
    try:
        done = _gh(repo, "pr", "view", str(number), "--json", "headRefOid")
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return _oid(str(data.get("headRefOid") or ""))


def _commits_outside_keeper(repo: Path, other_oid: str, keeper_oid: str) -> bool:
    """True only when `other_oid` has commits `keeper_oid` does not (GRPH-1452).

    False when the other head is the keeper, when it is contained (`ahead_by` 0), or
    when the compare could not be read. The last of those is "nobody looked", so it does
    not by itself spare the PR — an older draft whose `Items:` line names the merged
    item is still closed, which is the case the compare could not decide.
    """
    other, keeper = _oid(other_oid), _oid(keeper_oid)
    if not other or not keeper or same_commit(other, keeper):
        return False
    try:
        done = _gh(repo, "api",
                   f"repos/{{owner}}/{{repo}}/compare/{keeper}...{other}")
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    ahead = data.get("ahead_by")
    return isinstance(ahead, int) and not isinstance(ahead, bool) and ahead > 0


def close_item_prs(repo: Path, item: str, keeper_url: str, *, comment: str,
                   older_drafts_only: bool = False) -> tuple[str, ...]:
    """Close every other open `gb/*` PR of `item` with `comment` (GRPH-1396).

    `older_drafts_only` is the newer-attempt case: only drafts opened before the keeper are
    superseded. After a merge every other open PR of the item is, whatever its age. Best
    effort: a PR that cannot be closed stays open and nothing raises.

    A PR whose declared items do not include `item` stays open (GRPH-1452): the title is
    not ownership once the body has an `Items:` or `Attests:` line. So does one whose
    branch has commits the keeper does not — closing that throws away work the merge
    never carried. A compare that cannot be read does not invent that fact.
    """
    keeper = _pr_number(keeper_url)
    closed: list[str] = []
    keeper_head = ""
    looked = False
    try:
        rows = open_prs(repo)
        for pr in rows:
            url = str(pr.get("url") or "")
            if not url or url == keeper_url or not _carries_item(pr, item):
                continue
            if not str(pr.get("headRefName") or "").startswith("gb/"):
                continue
            number = pr.get("number") or _pr_number(url)
            if older_drafts_only and not (pr.get("isDraft") and keeper
                                          and int(number or 0) < keeper):
                continue
            if not looked:
                looked = True
                keeper_head = _keeper_head(repo, keeper)
            other = str(pr.get("headRefOid") or "")
            if keeper_head and _commits_outside_keeper(repo, other, keeper_head):
                continue
            done = _gh(repo, "pr", "close", str(number), "--comment", comment)
            if done.returncode == 0:
                closed.append(f"#{number}")
    except (OSError, subprocess.SubprocessError):
        pass
    return tuple(closed)


def _reopen(repo: Path, number) -> bool:
    """Reopen a closed PR. True when gh accepted it."""
    if not number:
        return False
    done = _gh(repo, "pr", "reopen", str(number))
    return done.returncode == 0


def _html_url(done: subprocess.CompletedProcess) -> str:
    """The `html_url` of a REST pull-request response, or ""."""
    try:
        data = json.loads(done.stdout or "")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("html_url") or "")


def _opened(repo: Path, branch: str, url: str,
            items: tuple[str, ...] | list[str]) -> Proposed:
    """A PR exists at `url`. Older drafts of the same item are superseded (GRPH-1396)."""
    by = f"#{_pr_number(url)}" if _pr_number(url) else url
    for item in items:
        close_item_prs(repo, item, url, comment=f"Superseded by {by}",
                       older_drafts_only=True)
    return Proposed(branch=branch, url=url, ok=True)


def propose(repo: Path, branch: str, base: str, *, title: str, body: str,
            head: str = "", items: tuple[str, ...] | list[str] = ()) -> Proposed:
    """Open a draft PR for `branch` against `base`, or say why not.

    Never raises and never fails a wave. The work is committed and pushed by the time this
    runs; a PR that could not be opened is a thing for a person to finish, not a reason to
    call the wave broken.

    A branch whose head already has a PR — open, closed, or merged — is not given a
    sibling (GRPH-1031). Open: the push already updated it. Closed: reopen, or say why
    not. Merged: say so; another PR for the same head is the defect. The merged half is asked
    twice, by branch and then by COMMIT (GRPH-1709): a branch-keyed answer is "" whenever
    `gh` did not exit zero, and which head landed is a fact about a commit, not about a ref.

    A GraphQL rate limit is not "no PR" (GRPH-1752). A lookup that dies on that text
    returns not-ok and does not create. `gh pr create` dying on it opens the same draft
    through the REST pulls endpoint. Any other create failure is reported as before.
    """
    if not find():
        return Proposed(branch=branch, skipped=True,
                        reason=f"gh is not installed, so nothing proposed {branch}. {INSTALL}")
    if not base:
        return Proposed(branch=branch, skipped=True,
                        reason="no base branch to propose against")
    try:
        already, limited = _existing(repo, branch)
        if limited:
            return _limited(branch, "pr view", limited)
        if already:
            return Proposed(branch=branch, url=already, skipped=True,
                            reason="already proposed")
        prior, limited = _existing_any(repo, branch)
        if limited:
            return _limited(branch, "pr list", limited)
        if prior:
            state = prior.get("state") or ""
            url = prior.get("url") or ""
            number = prior.get("number")
            if state == "MERGED":
                return Proposed(
                    branch=branch, url=url, skipped=True,
                    reason=f"head already merged as PR #{number}; not opening a sibling",
                )
            if state == "CLOSED":
                # GRPH-1452: a PR closed as superseded, whose branch has since moved,
                # is reopened here. Leaving it closed is "already has closed PR; not
                # opening a sibling", and the item then sits in review with no PR.
                if _reopen(repo, number):
                    return Proposed(
                        branch=branch, url=url, ok=True,
                        reason=f"reopened existing PR #{number}",
                    )
                return Proposed(
                    branch=branch, url=url, skipped=True,
                    reason=f"branch already has closed PR #{number}; not opening a sibling",
                )
            if url:
                return Proposed(branch=branch, url=url, skipped=True,
                                reason="already proposed")
        if head:
            # GRPH-1396: another branch whose open PR already carries this commit (a salvage
            # of a stopped child, a re-reap). A second PR for the same commit is the defect.
            opened, limited = _listed_checked(
                repo, "open", "number,url,headRefName,headRefOid,isDraft,title,body")
            if limited:
                return _limited(branch, "pr list", limited)
            for pr in opened:
                if (pr.get("headRefName") != branch and pr.get("url")
                        and same_commit(str(pr.get("headRefOid") or ""), head)):
                    return Proposed(
                        branch=branch, url=str(pr["url"]), skipped=True,
                        reason=f"commit already carried by open PR #{pr.get('number')} "
                               f"({pr.get('headRefName')}); not opening a second")
            # GRPH-1709: or a PR that already MERGED it — this branch's own included, which
            # the lookups above answer by ref and so can miss. Reaping gb/p86-56 after #1846
            # had landed opened #1847 for one and the same head, and the merge loop went on
            # proposing that duplicate on every later cycle.
            landed, limited = _listed_checked(
                repo, "merged", "number,url,headRefName,headRefOid")
            if limited:
                return _limited(branch, "pr list", limited)
            for pr in landed:
                if (pr.get("url")
                        and same_commit(str(pr.get("headRefOid") or ""), head)):
                    return Proposed(
                        branch=branch, url=str(pr["url"]), skipped=True,
                        reason=f"head already merged as PR #{pr.get('number')} "
                               f"({pr.get('headRefName')}); not opening a second")
        done = _gh(repo, "pr", "create", "--draft", "--head", branch,
                   "--base", base.split("/", 1)[-1], "--title", title, "--body", body)
    except (OSError, subprocess.SubprocessError) as exc:
        return Proposed(branch=branch, reason=f"gh could not run: {exc}")
    if done.returncode != 0:
        if _graphql_rate_limited(done):
            # GraphQL is spent; REST is a different quota. The draft is the same request
            # `gh pr create --draft` would have sent (GRPH-1752). `-F` so draft is a
            # boolean, not the string "true".
            try:
                rest = _gh(repo, "api", "-X", "POST", "repos/{owner}/{repo}/pulls",
                           "-f", f"title={title}",
                           "-f", f"head={branch}",
                           "-f", f"base={base.split('/', 1)[-1]}",
                           "-f", f"body={body}",
                           "-F", "draft=true")
            except (OSError, subprocess.SubprocessError) as exc:
                return Proposed(branch=branch, reason=f"gh could not run: {exc}")
            if rest.returncode == 0:
                url = _html_url(rest)
                if url:
                    return _opened(repo, branch, url, items)
                return Proposed(
                    branch=branch,
                    reason="gh pr create rate limited; REST create reported success without a URL")
            tail = (rest.stderr or rest.stdout or "").strip().splitlines()
            detail = tail[-1][:200] if tail else rest.returncode
            return Proposed(
                branch=branch,
                reason=f"gh pr create rate limited; REST create failed: {detail}")
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return Proposed(branch=branch,
                        reason=f"gh pr create failed: {tail[-1][:200] if tail else done.returncode}")
    url = next((line.strip() for line in (done.stdout or "").splitlines()
                if line.strip().startswith("http")), "")
    if url:
        # GRPH-1396: this attempt replaces older drafts of the same item.
        return _opened(repo, branch, url, items)
    return Proposed(branch=branch, reason="gh reported success without a URL")


def subject(repo: Path, branch: str, base: str) -> str:
    """The last commit subject on `branch` beyond `base` — what the WORKER called its work.

    Preferred over anything this module can compose, because the worker knows what it did and
    the supervisor does not. Empty when it cannot be read, which the caller treats as "fall
    back", never as an empty title.
    """
    if not base:
        return ""
    done = subprocess.run(["git", "log", "-1", "--format=%s", f"{base}..{branch}"],
                          cwd=str(repo), capture_output=True, text=True)
    return (done.stdout or "").strip().splitlines()[0].strip() if done.returncode == 0 and done.stdout.strip() else ""


def title_subject(repo: Path, branch: str, base: str) -> str:
    """The subject to TITLE a PR with: the branch's FIRST non-merge commit beyond `base`.

    The newest commit is the smallest follow-up fix on a branch with a bounce or a merge
    of main, and the squash commit would land on main named after it (GRPH-1419). The
    oldest non-merge commit is the build. Empty when unreadable; the caller falls back.
    """
    if not base:
        return ""
    done = subprocess.run(["git", "log", "--reverse", "--no-merges", "--format=%s",
                           f"{base}..{branch}"], cwd=str(repo), capture_output=True, text=True)
    if done.returncode != 0:
        return ""
    for line in (done.stdout or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _names(subject: str, item: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(item)}(?![A-Za-z0-9_])", subject or "") is not None


def split_by_item(repo: Path, branch: str, base: str,
                  items: list[str]) -> tuple[list[tuple[str, list[str]]], str]:
    """The commits that belong to each item, oldest first: `[(item, [sha, ...])]` in
    the order the items first appear, or `([], why)` when they cannot be separated
    (GRPH-948, GRPH-1086).

    Read from the worker's own commit subjects. A commit naming one of `items` belongs
    to it. A commit naming none, or naming more than one, cannot be put on one item's
    PR — the reason names that commit. Commits for one item need not be contiguous:
    each item is cut from the base, not stacked on the previous item, so an earlier
    item's commit is not an ancestor of the next item's branch.
    """
    if not base:
        return [], "there is no base to split it against"
    done = subprocess.run(["git", "log", "--reverse", "--format=%H%x09%s", f"{base}..{branch}"],
                          cwd=str(repo), capture_output=True, text=True)
    if done.returncode != 0:
        return [], "its commits could not be read"
    blocks: list[list] = []  # [item, [sha, ...]]
    for line in (done.stdout or "").splitlines():
        sha, _, subj = line.partition("\t")
        named = [i for i in items if _names(subj, i)]
        if len(named) > 1:
            return [], f"commit {sha[:12]} names {', '.join(named)}"
        if not named:
            # Not the previous item's. Folding it in is how a commit that names nothing
            # rides into a PR and gets attested as that item (GRPH-1086).
            return [], f"commit {sha[:12]} names no item"
        item = named[0]
        owned = next((b for b in blocks if b[0] == item), None)
        if owned is None:
            blocks.append([item, [sha]])
        else:
            owned[1].append(sha)
    missing = [i for i in items if not any(b[0] == i for b in blocks)]
    if missing:
        return [], f"no commit names {', '.join(missing)}"
    return [(b[0], list(b[1])) for b in blocks], ""


def _patch_does_not_apply(proc: subprocess.CompletedProcess) -> bool:
    """Whether git refused because the patch does not apply (GRPH-1093).

    A conflict says CONFLICT, or that the commit could not be applied. A missing
    committer identity also leaves CHERRY_PICK_HEAD — the patch is applied and the
    commit is what fails — so that marker is not a conflict. Treating it as one
    reports every other failure as a dependency.
    """
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return "CONFLICT" in text or "could not apply" in text


def _git_last_line(proc: subprocess.CompletedProcess) -> str:
    """The last non-empty line git printed, stderr before stdout."""
    for blob in (proc.stderr, proc.stdout):
        lines = [ln.strip() for ln in (blob or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:160]
    return str(proc.returncode)


def replay_onto(repo: Path, base: str, commits: list[str], *,
                item: str = "") -> tuple[str, str]:
    """Replay `commits` onto `base`. `(sha, "")`, or `("", why)` (GRPH-1086, GRPH-1093).

    Done in a throwaway worktree so the caller's checkout stays where it is. A commit
    whose parent is the current tip fast-forwards, which keeps the original sha for a
    prefix of the branch. Anything else is cherry-picked, so the result contains those
    commits and not the other item's. A patch that needs the other item's tree does not
    apply; that is the refusal, and the branch is left uncut.

    Any other failure — no committer identity, a signing hook, a lock — is git's own
    last line, `could not cut <item>: …`. Reporting it as a dependency points at the
    history when the cause is the environment.
    """
    if not commits:
        return "", "there are no commits to cut"
    base_read = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
        cwd=str(repo), capture_output=True, text=True)
    if base_read.returncode != 0 or not (base_read.stdout or "").strip():
        return "", "there is no base to cut it from"
    base_sha = base_read.stdout.strip()
    parent = Path(tempfile.mkdtemp(prefix="gb-split-"))
    wt = parent / "wt"
    env = {**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"}
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(wt), base_sha],
            cwd=str(repo), capture_output=True, text=True, env=env)
        if added.returncode != 0:
            tail = (added.stderr or added.stdout or "").strip().splitlines()
            return "", (f"could not cut from {base_sha[:12]}: "
                        f"{tail[-1][:160] if tail else added.returncode}")
        for sha in commits:
            picked = subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "cherry-pick", "--ff", sha],
                cwd=str(wt), capture_output=True, text=True, env=env)
            if picked.returncode != 0:
                subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(wt),
                               capture_output=True, text=True, env=env)
                if _patch_does_not_apply(picked):
                    return "", f"commit {sha[:12]} depends on an earlier item's commit"
                who = item or sha[:12]
                return "", f"could not cut {who}: {_git_last_line(picked)}"
        tip = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(wt), capture_output=True, text=True)
        if tip.returncode != 0 or not (tip.stdout or "").strip():
            return "", "the cut branch has no commit"
        return tip.stdout.strip(), ""
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=str(repo), capture_output=True, text=True)
        shutil.rmtree(parent, ignore_errors=True)


def describe(branch: str, items: list[str], commit_subject: str = "", *,
             attest: bool = False) -> tuple[str, str]:
    """Title and body for work a wave produced — for ONE item, or none (GRPH-948).

    THE WORKER'S OWN SUBJECT LEADS (GRPH-817). The first version always composed
    `<items> (from <branch>)`, which put a branch name in front of a reviewer where a
    sentence about the work should be — and read as inconsistent beside the PRs children
    opened for themselves with `gh`, which use the commit subject. Two openers, two styles,
    on the same wave.

    The item id stays, prefixed: it is the one thing the subject usually omits and the one a
    reviewer needs to find the evidence. Branch goes to the body, where it belongs.

    Still nothing invented. The supervisor did not do the work and cannot summarise it; a
    generated paragraph claiming to is the kind of confident filler a reviewer learns to skip,
    and then skips on the PR that needed reading.
    """
    if len(items) > 1:
        # GRPH-948. A title naming several items is a PR carrying several items — the reported
        # one was "SA-583, SA-581, SA-580: …", 19 files, merged before review. The caller
        # splits the branch first (`split_by_item`) or does not propose it.
        raise ValueError(f"a PR carries one item, not {len(items)}: {list(items)}")
    named = items[0] if items else ""
    if commit_subject and named and _names(commit_subject, named):
        # Already named by the worker; prefixing it again read `SA-580: SA-580: …`.
        title = commit_subject
    elif commit_subject and named:
        title = f"{named}: {commit_subject}"
    elif commit_subject:
        title = commit_subject
    else:
        title = f"{named or branch} (from {branch})"
    body = (
        f"Opened by `gbfleet` after reaping `{branch}`.\n\n"
        + ("Items: " + ", ".join(items) + "\n\n" if items else "")
        + "A **draft**: the work is committed and pushed, and whether it is ready for review "
          "is a judgement the supervisor did not make. The ledger holds the evidence — read "
          "the item before this description, which is generated and knows nothing.\n"
    )
    # Only when the caller asks. A single-item branch keeps the body it had: CI still
    # finds the id in the prose. A split PR must say `Attests:` so a body that mentions
    # the branch, or the other item, cannot attest both (GRPH-1086). `attest` stays off
    # the single-item path.
    if attest and named:
        body += f"Attests: {named}\n"
    return title, body


# ---- finishing the merge after sign-off (GRPH-846) --------------------------------------------
#
# `done` is a ledger state, not a git state. After `sign_off` the PR sits as a draft until a
# person merges it, and every item `until` HOLDS on that PR (GRPH-798) is idle fleet time
# spent waiting for a click — the one step in the loop with no model and no timer behind it.
#
# OPT-IN, and every precondition is stated rather than assumed, because this is the one act
# in the package that changes the trunk. Branch protection stays the backstop; nothing here
# bypasses it, and a merge the forge refuses is `skipped`, never `ok`.

#: The adapter `sign_off` writes its attestation under (`backend/app/services/fleet.py`).
SIGN_OFF_ADAPTER = "fleet.sign_off"
#: The predicate CI attests (`scripts/attest_ci.py`). Named exactly: an adapter claiming
#: more than it checked is how a gate quietly starts accepting less than it appears to.
CI_PREDICATE = "suite_green"
#: The predicate CI attests after running the named security tools at a PR head (GRPH-P57
#: D2). Required only on a merge into the release ref, and only against the ACTUAL head.
SCANS_PREDICATE = "scans_clean"
#: What `gh pr view --json` is asked for. `mergeCommit` is null until the PR is merged.
#: `title` and `body` tell a branch lookup apart from this item's PR: `gh pr view
#: <branch>` returns whichever PR has that head, and on a shared branch that is
#: another item's merge (GRPH-1023). The head name is the lookup, not proof.
_PR_FIELDS = ("number,url,title,body,state,isDraft,mergeable,mergeStateStatus,"
              "headRefOid,headRefName,baseRefName,isCrossRepository,mergeCommit")


@dataclass(frozen=True)
class Merged:
    """What became of the attempt to finish one item's merge.

    Four outcomes, and they are not two. `ok` is a merge commit on the trunk. `pending` is
    auto-merge armed on a PR the forge will merge when its own requirements are met — real
    progress, not a merge. `skipped` is a merge this process cannot perform (no `gh`, branch
    protection, a fork, no PR at all), the same distinction `Proposed` makes and for the
    same reason. The rest is a PRECONDITION MISS: the item was left alone, and `reason`
    names which check failed — a head that moved after review is the case this exists to
    catch, and it must read differently from "could not merge".
    """

    item: str
    selector: str = ""
    url: str = ""
    commit: str = ""
    ok: bool = False
    pending: bool = False
    skipped: bool = False
    reason: str = ""
    #: The preconditions that HELD, in the order they were asked. Reported beside the miss
    #: so "head moved" cannot be mistaken for "nothing was checked".
    checked: tuple[str, ...] = ()
    #: Which release ref this attempt used, as a phrase for the MERGE line (GRPH-P57 D1):
    #: "release ref main (project setting)" or "release ref unset, scans_clean not
    #: required". Every line names it, so drift between boxes shows in the log.
    release: str = ""
    #: Other open PRs of the item closed as "Superseded by #n" after this merge (GRPH-1219).
    closed: tuple[str, ...] = ()
    #: Files a rebase onto the base could not reconcile (GRPH-1389). Non-empty means the item
    #: is to be bounced, naming these, rather than left as a PR that can never run CI.
    conflicts: tuple[str, ...] = ()
    #: The PR head is a different commit from the sign-off, and the item's own files changed.
    #: The Merger bounces it. Leaving it `done` is how an unreviewed head gets merged.
    rereview: bool = False

    @property
    def final(self) -> bool:
        """True when re-asking cannot change the answer this wave: merged, or unperformable."""
        return self.ok or self.skipped


def reviewed_commit(item: dict) -> str:
    """The commit the LAST `fleet.sign_off` attestation vouches for, or "".

    The latest, because evidence appends: an item bounced and re-signed carries both, and
    the merge must compare against the review that stands. A branch name is never the
    answer here — a head that moved after review has the same branch name and a different
    commit, which is exactly the case the check exists to catch.
    """
    found = ""
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "attestation":
            continue
        if str(e.get("adapter") or "") != SIGN_OFF_ADAPTER:
            continue
        commit = str(e.get("commit") or "").strip()
        if commit:
            found = commit
    return found


def green_commits(item: dict) -> set[str]:
    """Every commit some attestation reports `suite_green` PASSED on.

    A failing predicate on the same receipt does not count — the same rule
    `valid_attestations` applies on the server, restated here because this reader has no
    server to ask and must not be laxer than the gate it stands beside.
    """
    out: set[str] = set()
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "attestation":
            continue
        commit = str(e.get("commit") or "").strip()
        preds = e.get("predicates") or []
        if not commit or not isinstance(preds, list):
            continue
        rows = [p for p in preds if isinstance(p, dict)]
        if not rows or any(not p.get("passed") for p in rows):
            continue
        if any(str(p.get("name") or "") == CI_PREDICATE for p in rows):
            out.add(commit)
    return out


def red_commits(item: dict) -> set[str]:
    """Every commit some attestation reports `suite_green` FAILED on (GRPH-1415).

    The counterpart of `green_commits`: a red CI run at the vouched commit blocks the merge in
    every `build.test_scope` mode, so local receipts can stand in for a MISSING attestation but
    never overrule a present failure. A commit that ALSO has a passing `suite_green` (a rerun
    at the same commit that went green) is not red: one green run was always enough."""
    out: set[str] = set()
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "attestation":
            continue
        commit = str(e.get("commit") or "").strip()
        preds = e.get("predicates") or []
        if not commit or not isinstance(preds, list):
            continue
        if any(isinstance(p, dict) and str(p.get("name") or "") == CI_PREDICATE
               and not p.get("passed") for p in preds):
            out.add(commit)
    return out - green_commits(item)


#: The engines a full local run must have covered for `agent_full` to stand in for CI.
LOCAL_ENGINES = ("postgres", "sqlite")


def _full_passing(e: object) -> bool:
    return (isinstance(e, dict) and e.get("kind") == "test"
            and str(e.get("scope") or "").lower() == "full"
            and int(e.get("failed") or 0) == 0 and int(e.get("passed") or 0) > 0)


def any_full_local_receipt(item: dict) -> bool:
    """True when the item carries at least one passing `scope=full` `test` receipt, on any
    engine — the cheap check before reading which engines the project needs (GRPH-1429)."""
    return any(_full_passing(e) for e in item.get("evidence") or [])


def full_local_receipts(item: dict, engines: tuple[str, ...] | list[str] | None = None) -> bool:
    """True when the item carries a passing `test` receipt with `scope=full` on EACH of
    `engines` (GRPH-1415; default Postgres and SQLite, else the project's `engines`, GRPH-1429).
    A partial run, a failing run, or a missing engine is not it."""
    need = tuple(e.lower() for e in engines) if engines else LOCAL_ENGINES
    seen: set[str] = set()
    for e in item.get("evidence") or []:
        if not _full_passing(e):
            continue
        engine = str(e.get("engine") or "").lower()
        for name in need:
            if name in engine:
                seen.add(name)
    return all(n in seen for n in need)


@dataclass(frozen=True)
class ScanVerdict:
    """One `scans_clean` predicate as the ledger recorded it: the commit its attestation
    names, whether it passed, and the predicate's detail (kind, tool, prefix on a failure)."""

    commit: str
    passed: bool
    detail: str = ""


def scans_clean_verdicts(item: dict) -> list[ScanVerdict]:
    """Every `scans_clean` predicate on the item, in the order the ledger returns evidence.

    That order is the server's insertion order — evidence is an append-only list
    (`items.append_evidence`) — and it is the only order this trusts: the latest verdict
    for a head wins, so a rerun's pass unblocks a merge and a later failure blocks it. A
    runner timestamp is never consulted. Deliberately NOT `green_commits`' shape: a union of
    passes would read pass-then-fail on one head as passed (GRPH-P57 D6).

    Only the `scans_clean` predicate is read, not the receipt's other predicates: whether
    the scan passed is that predicate's answer alone.
    """
    out: list[ScanVerdict] = []
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "attestation":
            continue
        commit = str(e.get("commit") or "").strip()
        preds = e.get("predicates") or []
        if not commit or not isinstance(preds, list):
            continue
        for p in preds:
            if isinstance(p, dict) and str(p.get("name") or "") == SCANS_PREDICATE:
                out.append(ScanVerdict(commit=commit, passed=p.get("passed") is True,
                                       detail=str(p.get("detail") or "").strip()))
    return out


def release_ref(roster: dict | None) -> str:
    """The project's release ref from a `fleet_status` payload, or "" when it is unset.

    Only `source == "project"` names a ref (GRPH-1109). `unmeasured`, `unknown`, and a
    server that predates the field are all "unset": nothing here infers `main`, the remote's
    default, or `gitops.base_branch` — an unset ref means the rule does not exist.
    """
    got = (roster or {}).get("release_ref") if isinstance(roster, dict) else None
    if not isinstance(got, dict) or got.get("source") != "project":
        return ""
    return str(got.get("ref") or "").strip()


MERGE_METHODS = ("squash", "merge", "rebase")


def merge_method(ctx: dict | None) -> str:
    """The `gitops.merge_method` from a `get_context` payload (GRPH-1428) — read where the
    base branch is read, so a linked box gets the cloud's rule, not a stale local row.

    Anything unreadable — a server that predates the field, an unknown word — is `squash`,
    which is what the supervisor always did. It never picks a flag the project did not name."""
    gitops = ctx.get("gitops") if isinstance(ctx, dict) else None
    got = gitops.get("merge_method") if isinstance(gitops, dict) else None
    method = str(got.get("value") or "").strip() if isinstance(got, dict) else ""
    return method if method in MERGE_METHODS else "squash"


def release_phrase(ref: str) -> str:
    """How every MERGE line names the release ref it used (GRPH-P57 D1)."""
    if not ref:
        return f"release ref unset, {SCANS_PREDICATE} not required"
    return f"release ref {ref} (project setting)"


def _scans_refusal(head: str, ref: str, scans) -> str:
    """Why `head` may not merge into the release ref, or "" when it may.

    Keyed strictly by commit, and the LATEST verdict for the head wins. An attestation on
    an older commit neither passes nor blocks this head; the latest older pass is named
    only so the reader knows CI has run, just not here.
    """
    mine = [v for v in scans or () if same_commit(head, v.commit)]
    if mine:
        last = mine[-1]
        if last.passed:
            return ""
        return (f"{SCANS_PREDICATE} failed on {head[:12]} (release ref {ref}): "
                f"{last.detail or 'the predicate recorded no detail'}")
    older = [v for v in scans or () if v.passed]
    tail = f"; latest pass names {older[-1].commit[:12]}" if older else ""
    return f"CI has not attested {SCANS_PREDICATE} on {head[:12] or '?'} (release ref {ref}){tail}"


def _receipt_branch(detail: str) -> str:
    """The branch a `pr_opened` receipt names, or "". The server writes the detail as
    "draft PR opened by gbfleet for <branch>" (services/fleet.py `_observation_receipt`)."""
    head, sep, tail = (detail or "").rpartition(" for ")
    if not sep or "PR opened" not in head:
        return ""
    return tail.strip().strip("`")


def pr_selector(item: dict) -> str:
    """What to hand `gh pr view`: the item's PR if it names one, else the PR receipt the
    supervisor wrote when it proposed the item's branch, else the LATEST PR receipt, else
    the branch itself.

    A signed-off item's `branch` is routinely empty, which is why the receipt is consulted
    before it — the URL `propose_branch` recorded is the one thing that reliably survives.

    Evidence only appends. An item reworked on a new branch gets a second receipt for its
    new PR while the first still names the old one, and taking the first match checked the
    stale PR forever (GRPH-1218). So the receipt for `branch` wins, and without one the
    newest receipt does.

    One branch can also carry several receipts: a bounce reuses the branch and a fresh PR
    replaces the closed one. The LAST receipt for `branch` is the live PR; the first named
    a closed PR and held every dependent forever (GRPH-1687).
    """
    pr = item.get("pr")
    if isinstance(pr, str) and pr.strip():
        return pr.strip()
    branch = str(item.get("branch") or "").strip()
    latest = ""
    for_branch = ""
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "url":
            continue
        url = str(e.get("url") or "").strip()
        if "/pull/" not in url:
            continue
        latest = url
        if branch and _receipt_branch(str(e.get("detail") or "")) == branch:
            for_branch = url
    return for_branch or latest or branch


def pr_receipts(item: dict) -> list[str]:
    """Every PR the item names — its `pr` field, then each `/pull/` receipt — once each, in
    ledger order. What `merge` checks when the PR `pr_selector` chose is not at the reviewed
    commit, and what it closes as superseded once one of them lands (GRPH-1219)."""
    urls: list[str] = []
    pr = item.get("pr")
    if isinstance(pr, str) and pr.strip():
        urls.append(pr.strip())
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "url":
            continue
        url = str(e.get("url") or "").strip()
        if "/pull/" in url and url not in urls:
            urls.append(url)
    return urls


#: The `detail` prefix of the receipt `supervisor.Merger._record` writes when a squash lands.
#: Spelled a second time here because fleet is a separate distribution and cannot import the
#: server's `MERGE_RECEIPT_PREFIX`. Both copies are pinned by a test that writes the literal
#: rather than the constant — backend/tests/test_merge_receipt.py on the server's side,
#: fleet/tests/test_merged_head_is_not_reproposed.py on this one — so a drift reddens one.
MERGE_RECEIPT_PREFIX = "merged by gbfleet"


def merge_receipt(item: dict) -> str:
    """What this item's recorded merge names — its PR URL, else the merge commit — or ""
    (GRPH-1709).

    The Merger writes a `url` row carrying the MERGE COMMIT, and its presence means the merge
    landed AND was written down. Reading it is what stops the loop going back to GitHub about
    an item that is finished: the calls it would make share one rate limit with every other
    drain on the account, and a `gh` that fails under that limit reads as "no PR", which is
    how a merged item got a second one. Evidence only appends, so the last such row is the
    merge; an item with none answers "", which is "not recorded", never "not merged".
    """
    found = ""
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "url":
            continue
        commit = str(e.get("commit") or "").strip()
        if not commit:
            continue
        if not str(e.get("detail") or "").startswith(MERGE_RECEIPT_PREFIX):
            continue
        found = str(e.get("url") or "").strip() or commit
    return found


def _pr_ref(pr: dict | None, fallback: str) -> str:
    """`#9` when the forge gave a number or the URL ends in one, else `fallback`."""
    number = (pr or {}).get("number")
    if isinstance(number, int):
        return f"#{number}"
    m = re.search(r"/pull/(\d+)", str((pr or {}).get("url") or fallback or ""))
    return f"#{m.group(1)}" if m else (fallback or "?")


def _url_for_ref(repo: Path, selector: str) -> str:
    """Any-state PR URL for a branch ref, or "".

    `gh pr view <branch>` only finds OPEN PRs. After a GitHub merge the PR is MERGED and that
    lookup used to read as "no PR", so merged-dependency detection fell through to matching
    a commit subject `{TAG}-{N}:` — which a default "Merge pull request #…" never is
    (GRPH-1032). URLs and numbers are already selectors; do not re-list those.
    """
    s = (selector or "").strip()
    if not s or s.startswith("http") or "/pull/" in s or s.lstrip("#").isdigit():
        return ""
    return str(existing_any(repo, s).get("url") or "")


def view(repo: Path, selector: str) -> tuple[dict | None, str]:
    """`(pr, error)`. `pr` is None when there is no PR for `selector` or `gh` could not say.

    Two Nones with different `error`s, on purpose: "no PR found for that branch" is a fact about
    the branch and ends the attempt as `skipped`; a `gh` that could not run is a fact about
    this machine and reads the same way, but the reason must say which.

    A branch whose PR is MERGED or CLOSED still resolves: `pr view` is OPEN-only, so a miss
    is retried against `pr list --state all` (GRPH-1032).
    """
    try:
        done = _gh(repo, "pr", "view", selector, "--json", _PR_FIELDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"gh could not run: {exc}"
    if done.returncode != 0:
        other = _url_for_ref(repo, selector)
        if other:
            return view(repo, other)
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return None, f"no PR for {selector}: {tail[-1][:200] if tail else done.returncode}"
    try:
        payload = json.loads(done.stdout or "{}")
    except ValueError:
        return None, "gh pr view returned something that was not JSON"
    return (payload if isinstance(payload, dict) else None), ""


def _merge_commit(pr: dict) -> str:
    got = pr.get("mergeCommit")
    return str(got.get("oid") or "") if isinstance(got, dict) else ""


#: The shortest commit id `same_commit` will treat as naming one commit. Git's own default
#: abbreviation is 7; anything shorter is a prefix that could name several commits, and a
#: check that exists to refuse the wrong commit must not accept a fragment that matches many.
COMMIT_MIN = 7


def same_commit(a: str, b: str) -> bool:
    """Whether two commit ids name the same commit, when one may be abbreviated.

    The first version compared with `!=`. A reviewer's sign-off carries whatever the reviewer
    wrote — `git rev-parse --short` gives 7 to 12 characters — while the forge reports the
    full 40, so on 2026-09-23 a signed-off, CI-green, mergeable PR was refused with
    "PR head d565a6835398 is not the reviewed commit d565a683": the same commit, twice. The
    comparison is prefix-based in either direction, on hex only, never below `COMMIT_MIN`,
    and never on an empty side — an empty reviewed commit is "nothing to compare", handled
    before this is asked, and must not become a match for everything.
    """
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if len(a) < COMMIT_MIN or len(b) < COMMIT_MIN:
        return False
    if not all(c in "0123456789abcdef" for c in a + b):
        return False
    return a.startswith(b) or b.startswith(a)


_VERSIONS = "backend/alembic/versions"
_REV = re.compile(r"""^revision(?:\s*:\s*[^=]+)?\s*=\s*['"]([^'"]+)['"]""", re.M)
_DOWN = re.compile(r"""^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$""", re.M)


def heads_of(files: dict[str, str]) -> list[str]:
    """Alembic heads of {path: source}: revisions no other revision names as its parent."""
    revs: set[str] = set()
    parents: set[str] = set()
    for src in files.values():
        m = _REV.search(src)
        if not m:
            continue
        revs.add(m.group(1))
        d = _DOWN.search(src)
        if d:
            parents.update(re.findall(r"""['"]([^'"]+)['"]""", d.group(1)))
    return sorted(revs - parents)


def merged_alembic_heads(repo: Path, base: str, head: str) -> list[str] | None:
    """Alembic heads of the tree merging `head` into `base` would produce; None when git
    could not say (then the check is reported as unchecked, never as passed)."""
    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)

    if not Path(repo).is_dir():
        return None
    try:
        tree = git("merge-tree", "--write-tree", base, head)
        if tree.returncode != 0 or not tree.stdout.strip():
            return None
        tree_id = tree.stdout.strip().splitlines()[0]
        names = git("ls-tree", "-r", "--name-only", tree_id, "--", _VERSIONS + "/")
        if names.returncode != 0:
            return None
        files: dict[str, str] = {}
        for name in names.stdout.split():
            if not name.endswith(".py"):
                continue
            blob = git("show", f"{tree_id}:{name}")
            if blob.returncode == 0:
                files[name] = blob.stdout
        return heads_of(files) if files else []
    except (OSError, subprocess.SubprocessError):
        return None


def rebase_conflicts(repo: Path, base: str, head: str) -> list[str]:
    """Files a merge of `head` onto `base` would conflict on (empty when none or unknown)."""
    if not Path(repo).is_dir():
        return []
    try:
        out = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", base, head],
                             cwd=str(repo), capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode == 0:
        return []
    lines = out.stdout.splitlines()[1:]
    names = []
    for ln in lines:
        if not ln.strip():
            break
        names.append(ln.strip())
    return names


def _update_branch(repo: Path, item_id: str, selector: str, url: str, base_ref: str, head: str,
                   checked: list[str], method: str = "squash") -> Merged:
    """A PR behind its base is rebased and left for CI to run at the new head (GRPH-1389).

    Merging waits for green at the NEW head: the next tick finds a head that moved with the
    item's own files unchanged, and CI has not attested it yet. A rebase the forge cannot do
    is a conflict, and the item is bounced naming the files instead of left as a PR that can
    never run CI."""
    base = f"origin/{base_ref}" if base_ref else "origin/HEAD"
    try:
        if Path(repo).is_dir():
            subprocess.run(["git", "fetch", "origin", base_ref] if base_ref else ["git", "fetch"],
                           cwd=str(repo), capture_output=True, text=True, timeout=120)
        # `--rebase` under squash/rebase (today's behaviour); a merge update under merge.
        update = ["pr", "update-branch", selector] + ([] if method == "merge" else ["--rebase"])
        done = _gh(repo, *update, timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"gh could not run: {exc}")
    if done.returncode != 0:
        files = rebase_conflicts(repo, base, head)
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        if files or any("conflict" in t.lower() for t in tail):
            return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                          conflicts=tuple(files) or ("(files not reported)",),
                          reason="rebase onto the base conflicts in "
                                 + (", ".join(files) or "files the forge did not name"))
        return Merged(item=item_id, selector=selector, url=url, skipped=True,
                      checked=tuple(checked),
                      reason=f"gh pr update-branch failed: {tail[-1][:200] if tail else done.returncode}")
    return Merged(item=item_id, selector=selector, url=url, pending=True,
                  checked=tuple(checked) + ("rebased",),
                  reason="PR was behind its base and has been rebased; waiting for CI "
                         "to attest the new head before merging")


def merge(repo: Path, item_id: str, selector: str, *, reviewed: str,
          green: set[str], paths: list[str] | tuple[str, ...] | None = None,
          release: str = "", method: str = "squash",
          scans: list[ScanVerdict] | tuple[ScanVerdict, ...] = (),
          siblings: list[str] | tuple[str, ...] = (),
          covering=(), local_full: bool = False,
          red: set[str] | frozenset[str] = frozenset(),
          branch: str = "") -> Merged:
    """Finish one item's merge, or say exactly which precondition stopped it.

    `reviewed` is the commit the sign-off attestation names; `green` is every commit CI has
    attested `suite_green` on. Both come from the ledger and neither is derived here, so a
    caller cannot pass a branch name for either — the comparison is commit to commit.

    `paths` are the item's own files. A head that moved only because main was merged in —
    those paths byte-identical to `reviewed` — is not a new review (GRPH-1032). Empty `paths`
    cannot prove that, so the head must still match.

    A reviewed commit that the remote branch does not contain, but this clone does, and
    that fast-forwards the PR head, is pushed and then held for CI on that commit
    (GRPH-1412). One that is present locally and is not a descendant of the PR head is
    refused, naming both commits. A head that already contains the reviewed commit is
    the older "branch moved after sign-off" refusal — pushing would rewind it.

    `release` is the project's release ref ("" when unset) and `scans` the item's
    `scans_clean` verdicts in ledger order, both read by the caller (GRPH-P57 D6). A PR
    whose base is the release ref also needs the latest verdict on the ACTUAL head to pass —
    never the reviewed commit, and with no GRPH-1032 exemption: merging main brings in the
    code the scan exists to check. Any other base, or an unset ref, skips it. Nothing here
    calls `scan_status` or reads Stack.

    `siblings` are the item's other PRs (`pr_receipts`). When the selected PR's head is not
    the reviewed commit, the one sibling at that commit (already MERGED first, else OPEN)
    is merged or recorded instead, also when the selected PR is closed; none or several is
    a hold naming every PR checked and its head (GRPH-1219). After a merge, any other
    sibling still open is closed as superseded — best effort, never a failed merge.

    `covering` is the touchpoints of every OTHER item on this branch that is signed off at the
    reviewed commit (GRPH-1408). A PR's changed files must all sit inside `paths` plus
    `covering`; a file in neither is work nobody reviewed under this sign-off, and the merge is
    refused naming it. Empty `paths` declares nothing to compare against, so it does not gate.
    It may be a zero-argument callable: it is then called only when some changed file lies
    outside `paths`, so the common merge does not pay for the lookup.

    `local_full` is True only when the project's `build.test_scope` is `agent_full` AND the item
    carries full local receipts on both engines (`full_local_receipts`) — the caller decides
    both (GRPH-1415). Then a MISSING `suite_green` no longer blocks the merge. `red` is every
    commit with a FAILING `suite_green`; one at the vouched commit blocks in every mode.

    `branch` is the item's branch. When `selector` is a URL or number `view` cannot resolve,
    the PR for `branch` is used instead of giving up (GRPH-1348). Empty, or equal to
    `selector`, leaves the lookup `view` already does for a branch name.

    Never raises and never fails a wave. Every path returns a `Merged` that says what it did,
    and names the release ref it used.
    """
    got = _merge(repo, item_id, selector, reviewed=reviewed, green=green, paths=paths,
                 release=release, method=method, scans=scans, siblings=tuple(siblings or ()),
                 covering=covering if callable(covering) else tuple(covering or ()),
                 local_full=local_full, red=frozenset(red), branch=branch)
    if got.ok and siblings:
        got = replace(got, closed=_close_superseded(repo, got, siblings))
    return replace(got, release=release_phrase(release))


def close_after_merge(repo: Path, got: "Merged") -> "Merged":
    """GRPH-1396: close every other open `gb/*` PR of the merged item, named by `got.url`,
    whether or not an item receipt lists it. Best effort; returns `got` with `closed` extended."""
    if not (got.ok and got.url):
        return got
    more = close_item_prs(repo, got.item, got.url,
                          comment=f"Superseded by {_pr_ref(None, got.url)}")
    return replace(got, closed=got.closed + tuple(m for m in more if m not in got.closed))


def _close_superseded(repo: Path, got: Merged, siblings) -> tuple[str, ...]:
    """Close each other OPEN PR in `siblings` with "Superseded by #n". Best effort: a PR
    that cannot be read or closed is left open, and the merge stays a merge (GRPH-1219)."""
    by = _pr_ref(None, got.url)
    closed: list[str] = []
    for s in siblings:
        if s in (got.url, got.selector):
            continue
        try:
            pr, _ = view(repo, s)
            if pr is None or str(pr.get("state") or "").upper() != "OPEN":
                continue
            if str(pr.get("url") or "") == got.url:
                continue
            done = _gh(repo, "pr", "close", s, "--comment", f"Superseded by {by}")
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            closed.append(_pr_ref(pr, s))
    return tuple(closed)


def _sibling_at(repo: Path, item_id: str, selector: str, url: str, head: str, reviewed: str,
                siblings) -> tuple[str, str, bool]:
    """`(sibling, hold)`: the one other PR of the item whose head is `reviewed` — merged
    first, else open — or the reason none can stand in, naming every PR checked with its
    head (GRPH-1219).

    Wave p61b held four signed-off, green PRs with "the branch moved after sign-off" when the
    selector had simply picked the item's other PR; the hold named only that one."""
    seen = [f"{_pr_ref({'url': url}, selector)} head {head[:12] or '?'}"]
    hits: list[str] = []
    merged: list[str] = []
    for s in siblings:
        if s in (selector, url):
            continue
        pr, err = view(repo, s)
        if pr is None:
            seen.append(f"{s} unreadable ({err})")
            continue
        if str(pr.get("url") or "") == url:
            continue
        other = str(pr.get("headRefOid") or "")
        state = str(pr.get("state") or "").upper()
        seen.append(f"{_pr_ref(pr, s)} head {other[:12] or '?'}"
                    + ("" if state == "OPEN" else f" ({state.lower() or 'unknown state'})"))
        if state in ("OPEN", "MERGED") and same_commit(other, reviewed):
            (merged if state == "MERGED" else hits).append(s)
    # A sibling the forge already merged at the reviewed commit is the answer, not a hold:
    # its own auto-merge armed on an earlier tick landed, and the ledger still needs it.
    if len(merged) == 1:
        return merged[0], "", False
    if not merged and len(hits) == 1:
        return hits[0], "", False
    hits += merged
    checked_prs = "; ".join(seen)
    if hits:
        return "", (f"no single PR to merge: {len(hits)} PRs of {item_id} are at the "
                    f"reviewed commit {reviewed[:12]} (checked {checked_prs})"), False
    return "", (f"PR head {head[:12] or '?'} is not the reviewed commit {reviewed[:12]}, and "
                f"no other open or merged PR of {item_id} is (checked {checked_prs}) — the branch "
                "moved after sign-off or the reviewed PR is gone; it needs review again, "
                "not a merge"), True


def _via_sibling(repo: Path, item_id: str, selector: str, pr: dict, *, reviewed: str,
                 green: set[str], paths, release: str, scans, siblings: tuple[str, ...],
                 method: str = "squash", covering=(), local_full: bool = False,
                 red: frozenset[str] = frozenset(),
                 checked: list[str], miss: Merged | None = None) -> Merged:
    """Merge the item's sibling PR at `reviewed` in place of the selected `pr`, or hold
    naming every PR checked (`miss` instead, when given). The recursion gets no siblings,
    so it cannot loop (GRPH-1219)."""
    url = str(pr.get("url") or "")
    head = str(pr.get("headRefOid") or "")
    sibling, hold, rereview = _sibling_at(repo, item_id, selector, url, head, reviewed, siblings)
    if not sibling:
        if miss is not None:
            return miss
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      rereview=rereview, reason=hold)
    got = _merge(repo, item_id, sibling, reviewed=reviewed, green=green, paths=paths,
                 covering=covering, local_full=local_full, red=red,
                 release=release, scans=scans, method=method)
    # The selected PR is the one we are leaving. Say so whether its head moved or it is
    # simply closed: a closed PR at the reviewed commit still yields to the open sibling.
    why = (f"sibling={sibling} (selected {_pr_ref(pr, selector)} head {head[:12] or '?'}"
           + (" is closed)" if same_commit(head, reviewed)
              else " is not the reviewed commit)"))
    return replace(got, checked=tuple(checked) + (why,) + got.checked)


def _outside_files(repo: Path, selector: str, paths, covering) -> tuple[list[str], str]:
    """`(files, error)`: the PR's changed files covered by neither `paths` nor `covering`.

    No declared `paths` means nothing to compare against: `([], "")`, as `touchpoints.undeclared`.
    A diff that cannot be read is an error, not a pass."""
    from . import touchpoints as tp_mod

    if not (paths or []):
        return [], ""
    try:
        done = _gh(repo, "pr", "diff", selector, "--name-only")
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"could not read the PR's changed files: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return [], f"could not read the PR's changed files: {tail[-1][:200] if tail else done.returncode}"
    files = [f.strip() for f in (done.stdout or "").splitlines() if f.strip()]
    own = [p for p in paths if isinstance(p, str) and p.strip()]
    outside = tp_mod.undeclared(files, own)
    if not outside:
        return [], ""
    others = covering() if callable(covering) else covering
    declared = own + [p for p in (others or []) if isinstance(p, str) and p.strip()]
    return tp_mod.undeclared(outside, declared), ""


def _commit_here(repo: Path, sha: str) -> bool:
    """True when this clone can resolve `sha` to a commit. A missing object is not a
    failed ancestry check — those two answers must not share a branch."""
    if not (sha or "").strip() or not Path(repo).is_dir():
        return False
    from .worktree import _commit_sha

    try:
        return bool(_commit_sha(repo, sha))
    except (OSError, subprocess.SubprocessError):
        return False


def _push_unpushed_descendant(repo: Path, item_id: str, selector: str, url: str, pr: dict,
                              head: str, reviewed: str) -> tuple[str, Merged | None]:
    """`("pushed", None)` when `reviewed` was pushed onto the PR branch (GRPH-1412).

    `("stop", merged)` when the commit is in this clone, missing from the PR head, and
    either is not a fast-forward or the push did not land. `("skip", None)` when this
    is not that case: the commit is not here, or the PR head already contains it.

    The caller keeps going after a push and still requires CI on `reviewed`. Pushing
    the signed-off commit is what makes that attestation possible; it is not one.
    """
    if not _commit_here(repo, reviewed) or not _commit_here(repo, head):
        return "skip", None
    from .worktree import is_ancestor, push_commit

    # Already on the remote branch. Pushing `reviewed` would move the tip backwards
    # when the head has moved past the sign-off, which is a new review, not a publish.
    if is_ancestor(repo, reviewed, head):
        return "skip", None
    if not is_ancestor(repo, head, reviewed):
        return "stop", Merged(
            item=item_id, selector=selector, url=url, rereview=True,
            reason=f"reviewed commit {reviewed[:12]} is not a descendant of PR head "
                   f"{head[:12] or '?'} — not a fast-forward, so it was not pushed")
    branch = str(pr.get("headRefName") or "").strip()
    if not branch:
        return "stop", Merged(
            item=item_id, selector=selector, url=url,
            reason=f"reviewed commit {reviewed[:12]} fast-forwards PR head {head[:12] or '?'} "
                   "but the PR names no branch to push it to")
    pushed = push_commit(repo, reviewed, branch)
    if not pushed.ok:
        return "stop", Merged(
            item=item_id, selector=selector, url=url,
            reason=f"reviewed commit {reviewed[:12]} fast-forwards PR head {head[:12] or '?'} "
                   f"but was not pushed: {pushed.reason}")
    return "pushed", None


def _merge(repo: Path, item_id: str, selector: str, *, reviewed: str, green: set[str],
           paths, release: str, scans, siblings: tuple[str, ...] = (),
           method: str = "squash", covering=(), local_full: bool = False,
           red: frozenset[str] = frozenset(), branch: str = "") -> Merged:
    checked: list[str] = []
    if not find():
        return Merged(item=item_id, selector=selector, skipped=True,
                      reason=f"gh is not installed, so nothing merged {item_id}. {INSTALL}")
    if not reviewed:
        return Merged(item=item_id, selector=selector,
                      reason=f"no {SIGN_OFF_ADAPTER} attestation names a commit — nothing to "
                             "compare the PR head against")
    if not selector:
        return Merged(item=item_id, skipped=True,
                      reason="the item names no PR and no branch")

    pr, err = view(repo, selector)
    # GRPH-1348. `view` retries a branch selector against `pr list`. A recorded URL that
    # 404s does not: the item's `pr` field points at a PR that is gone while the branch
    # still has one. Look the branch up before giving up. The selector stays the URL when
    # the branch has nothing, so the reason still names what was asked.
    if pr is None and branch and branch != selector:
        found = existing_any(repo, branch)
        url = str(found.get("url") or "")
        if url:
            pr, err = view(repo, url)
            if pr is not None:
                selector = url
    if pr is None:
        return Merged(item=item_id, selector=selector, skipped=True, reason=err)
    url = str(pr.get("url") or "")
    state = str(pr.get("state") or "").upper()
    from .worktree import paths_unchanged

    if state == "MERGED":
        # Merged by somebody, or by the auto-merge this armed on an earlier tick.
        # A squash rewrites the merge commit, so the commit the sign-off can cover is the
        # PR head, not the squash SHA. Recording every MERGED PR is how #1505 landed: the
        # head had moved after sign-off, a person merged that head, and the next tick wrote
        # a merge receipt for a commit the review never saw.
        commit = _merge_commit(pr)
        head = str(pr.get("headRefOid") or "")
        if same_commit(head, reviewed) or paths_unchanged(repo, reviewed, head, paths):
            checked_merged = ["state=MERGED"]
            if head and not same_commit(head, reviewed):
                checked_merged.append("paths-unchanged")
            return Merged(item=item_id, selector=selector, url=url, commit=commit,
                          ok=bool(commit), checked=tuple(checked_merged),
                          reason="" if commit else "merged, but gh reported no merge commit")
        uncovered = (f"PR merged at head {head[:12] or '?'} which is not the reviewed "
                     f"commit {reviewed[:12]} — the sign-off does not cover it")
        if siblings:
            # A sibling still at the reviewed commit can be merged or recorded. Anything
            # else already landed at the wrong head: do not bounce it back onto the trunk.
            got = _via_sibling(repo, item_id, selector, pr, reviewed=reviewed, green=green,
                               paths=paths, release=release, scans=scans, method=method,
                               siblings=siblings, covering=covering, checked=checked,
                               local_full=local_full, red=red)
            if got.ok or got.pending:
                return got
            return replace(got, rereview=False, skipped=True, ok=False, reason=uncovered)
        return Merged(item=item_id, selector=selector, url=url, commit=commit, skipped=True,
                      checked=("state=MERGED",), reason=uncovered)
    if state != "OPEN":
        not_open = Merged(item=item_id, selector=selector, url=url, skipped=True,
                          reason=f"PR is {state or 'in an unknown state'}, not open")
        # GRPH-1219, GRPH-1577. A closed selection is the superseded PR — the first URL in
        # the evidence, or a `pr` field left on an abandoned branch — even when that closed
        # head happens to equal the reviewed commit and an open sibling is the one still to
        # merge. The first URL is never the merge target by itself. Without a sibling at the
        # reviewed commit, the skip stands (`miss`).
        if siblings:
            return _via_sibling(repo, item_id, selector, pr, reviewed=reviewed, green=green,
                                paths=paths, release=release, scans=scans, method=method,
                                siblings=siblings, covering=covering, checked=checked, miss=not_open,
                                local_full=local_full, red=red)
        return not_open
    checked.append("open")
    if pr.get("isCrossRepository"):
        return Merged(item=item_id, selector=selector, url=url, skipped=True,
                      reason="PR comes from a fork; the supervisor merges only branches of "
                             "this repository", checked=tuple(checked))
    checked.append("same-repo")

    # THE CHECK THIS EXISTS FOR. Commit against commit, never branch against branch: a push
    # after review keeps the branch name and changes the head, and a comparison on names
    # would merge code nobody reviewed under a sign-off that vouches for something else.
    #
    # GRPH-1032: merging main into a signed branch moves the head while leaving the item's
    # own files byte-identical. That is not a new review — the attested tree is still there.
    # A real edit of those paths still needs review again. Empty `paths` cannot prove the
    # first case, so it stays the refusal.
    #
    # GRPH-1412: a rebuild signed off at a commit this clone has and the remote branch
    # does not. When that commit fast-forwards the PR head, push it and wait for CI on
    # it. Refusing here is what left the PR open forever after the item was already done.
    head = str(pr.get("headRefOid") or "")
    if not same_commit(head, reviewed):
        if paths_unchanged(repo, reviewed, head, paths):
            checked.append("paths-unchanged")
        else:
            # GRPH-1219 first. A sibling already at the reviewed commit is the PR the
            # sign-off vouches for; publishing onto this branch would move a different ref.
            # The recursion gets no siblings, so it cannot loop. "no other … PR" is the
            # hold that means none of them is at the commit — the unpushed case.
            sibling_hold = None
            if siblings:
                sibling_hold = _via_sibling(
                    repo, item_id, selector, pr, reviewed=reviewed, green=green,
                    paths=paths, release=release, scans=scans, method=method,
                    siblings=siblings, covering=covering, checked=checked,
                    local_full=local_full, red=red)
                unmatched = (not sibling_hold.ok and not sibling_hold.pending
                             and "no other open or merged PR" in sibling_hold.reason)
                if not unmatched:
                    return sibling_hold
            kind, stopped = _push_unpushed_descendant(
                repo, item_id, selector, url, pr, head, reviewed)
            if kind == "pushed":
                # The remote tip is the signed-off commit now. CI still has to attest
                # it; the checks below are that wait, and the merge when it has.
                head = reviewed
                checked.append("pushed-reviewed")
            elif stopped is not None:
                return replace(stopped, checked=tuple(checked))
            elif sibling_hold is not None:
                return sibling_hold
            else:
                return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                              rereview=True,
                              reason=f"PR head {head[:12] or '?'} is not the reviewed commit "
                                     f"{reviewed[:12]} — the branch moved after sign-off; it needs "
                                     "review again, not a merge")
    if same_commit(head, reviewed):
        checked.append("head=reviewed")
    # CI runs on the commit the forge would merge. When the head is still the reviewed
    # commit, that is `reviewed`. When the head moved and this item's paths did not
    # (GRPH-1032), the suite that matters is the one on `head`: the reviewed commit can
    # stay red forever while the follow-up commit is the one CI attested.
    vouched = head if "paths-unchanged" in checked else reviewed
    # Green first: CI reruns at the same commit (`attempts/N`), and one passing run there was
    # always enough to merge. A flaky red attempt a later green rerun overturned must not hold
    # the PR for ever — that was the regression this order fixes (GRPH-1415 bounce).
    if any(same_commit(vouched, g) for g in green):
        checked.append(f"ci={CI_PREDICATE}")
    elif any(same_commit(vouched, r) for r in red):
        # A failing run with no passing one is CI's answer, in every mode; local receipts
        # never overrule it.
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"CI attested {CI_PREDICATE} FAILED on {vouched[:12]}")
    elif local_full:
        # GRPH-1415: `agent_full` — the builder's full runs on both engines stand in for a
        # CI attestation that never came, as the server's done gate does.
        checked.append("local-full-receipts(agent_full)")
    else:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"CI has not attested {CI_PREDICATE} on {vouched[:12]}")

    # GRPH-1408. The sign-off vouches for THIS item's files. A PR that also carries another
    # item's unreviewed work (#1388 landed GRPH-1359 under GRPH-1399's sign-off) is not
    # covered by it. Read from the forge's own diff, which is against the base's merge-base.
    outside, why_out = _outside_files(repo, selector, paths, covering)
    if why_out:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=why_out)
    if outside:
        shown = ", ".join(outside[:8]) + (f" (+{len(outside) - 8} more)" if len(outside) > 8 else "")
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"PR changes {len(outside)} file(s) outside {item_id}'s touchpoints "
                             f"and every other signed-off item's on this branch: {shown} — "
                             "work nobody signed off; review it, not merge it")
    if paths:
        checked.append("files-within-touchpoints")

    # GRPH-P57 D6. Against `head`, not `reviewed`: the paths-unchanged pass above vouches
    # for the item's own files, and says nothing about what main brought in with it. A PR
    # whose base the forge did not report is treated as into the release ref — not knowing
    # is not an exemption.
    base_ref = str(pr.get("baseRefName") or "").strip()
    if release and (not base_ref or base_ref == release):
        refused = _scans_refusal(head, release, scans)
        if refused:
            return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                          reason=refused)
        checked.append(f"{SCANS_PREDICATE}=head")

    if pr.get("isDraft"):
        try:
            done = _gh(repo, "pr", "ready", selector)
        except (OSError, subprocess.SubprocessError) as exc:
            return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                          reason=f"gh could not run: {exc}")
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()
            return Merged(item=item_id, selector=selector, url=url, skipped=True,
                          checked=tuple(checked),
                          reason=f"gh pr ready failed: {tail[-1][:200] if tail else done.returncode}")
        checked.append("marked-ready")
        # Re-read: the forge recomputes mergeability once a draft is ready, and the answer
        # a moment ago was about a draft.
        pr, err = view(repo, selector)
        if pr is None:
            return Merged(item=item_id, selector=selector, url=url, skipped=True,
                          checked=tuple(checked), reason=err)

    mergeable = str(pr.get("mergeable") or "").upper()
    status = str(pr.get("mergeStateStatus") or "").upper()
    base_ref = str(pr.get("baseRefName") or "").strip()
    if status == "BEHIND":
        return _update_branch(repo, item_id, selector, url, base_ref, head, checked,
                              method=method)
    if mergeable == "CONFLICTING" or status == "DIRTY":
        files = rebase_conflicts(repo, f"origin/{base_ref}" if base_ref else "origin/HEAD", head)
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      conflicts=tuple(files) or ("(files not reported)",),
                      reason="conflicts with the base in " + (", ".join(files) or
                                                              "files the forge did not name"))
    if mergeable != "MERGEABLE" or status != "CLEAN":
        if status == "BLOCKED":
            # Branch protection wants something this process is not: an approval, a check
            # it does not run. That is the backstop working, and it is reported as such.
            return Merged(item=item_id, selector=selector, url=url, skipped=True,
                          checked=tuple(checked),
                          reason="branch protection blocks it (mergeStateStatus BLOCKED)")
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"not mergeable yet: mergeable={mergeable or '?'}, "
                             f"mergeStateStatus={status or '?'}")
    checked.append("mergeable=CLEAN")

    # Backstop: two migrations both merge cleanly and leave two heads, which git cannot see.
    heads = merged_alembic_heads(repo, f"origin/{base_ref}" if base_ref else "origin/HEAD", head)
    if heads is not None and len(heads) > 1:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"merging would leave {len(heads)} alembic heads "
                             f"({', '.join(heads)}); refused — rebase and re-chain the migration")
    # No versions directory in the merged tree is `none`: a check that looked at nothing must
    # not read as a pass (GRPH-1423).
    checked.append("alembic-heads=unchecked" if heads is None
                   else "alembic-heads=none" if not heads else "alembic-heads=1")

    # `--auto` is the honest verb for a supervisor: it says "merge when your requirements
    # are met", and `gh` merges immediately when a PR is already CLEAN. The flag is the
    # project's `gitops.merge_method` (GRPH-1428), `--squash` when unset; whichever it is, the
    # PR head being merged is the reviewed commit checked above.
    try:
        done = _gh(repo, "pr", "merge", selector,
                    f"--{method if method in MERGE_METHODS else 'squash'}", "--auto", timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"gh could not run: {exc}")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return Merged(item=item_id, selector=selector, url=url, skipped=True,
                      checked=tuple(checked),
                      reason=f"gh pr merge failed: {tail[-1][:200] if tail else done.returncode}")
    after, err = view(repo, selector)
    if after is not None and str(after.get("state") or "").upper() == "MERGED":
        commit = _merge_commit(after)
        return Merged(item=item_id, selector=selector, url=url, commit=commit, ok=bool(commit),
                      checked=tuple(checked) + ("merged",),
                      reason="" if commit else "merged, but gh reported no merge commit")
    return Merged(item=item_id, selector=selector, url=url, pending=True,
                  checked=tuple(checked) + ("auto-merge-armed",),
                  reason="auto-merge enabled; the forge merges when its requirements are met"
                         + (f" ({err})" if err else ""))
