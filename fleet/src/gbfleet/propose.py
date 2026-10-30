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


def existing(repo: Path, branch: str) -> str:
    """The URL of a PR already open for this branch, or "".

    Asked FIRST, every time. `gh pr create` on a branch that already has one fails, and a
    failure there would read as "could not propose" for work that was proposed perfectly well
    an hour ago — the wave would report a problem that is not one.
    """
    done = _gh(repo, "pr", "view", branch, "--json", "url")
    if done.returncode != 0:
        return ""
    try:
        return str(json.loads(done.stdout or "{}").get("url") or "")
    except ValueError:
        return ""


def existing_any(repo: Path, branch: str) -> dict:
    """The newest PR for this branch in any state, or {}.

    `gh pr view <branch>` only finds OPEN PRs. After a bounce the PR is often closed, and
    creating a sibling for the same head is how GRPH-1031's reported waves grew #639 then
    #643 for one item. A closed or merged PR is still this branch's PR: update it, or say
    why not — do not open another.
    """
    done = _gh(repo, "pr", "list", "--head", branch, "--state", "all",
               "--json", "url,state,number", "--limit", "20")
    if done.returncode != 0:
        return {}
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return {}
    if not isinstance(rows, list):
        return {}
    found = [r for r in rows if isinstance(r, dict) and r.get("url")]
    if not found:
        return {}
    order = {"OPEN": 0, "CLOSED": 1, "MERGED": 2}
    found.sort(key=lambda r: order.get(str(r.get("state") or "").upper(), 9))
    row = found[0]
    return {
        "url": str(row.get("url") or ""),
        "state": str(row.get("state") or "").upper(),
        "number": row.get("number"),
    }


def _reopen(repo: Path, number) -> bool:
    """Reopen a closed PR. True when gh accepted it."""
    if not number:
        return False
    done = _gh(repo, "pr", "reopen", str(number))
    return done.returncode == 0


def propose(repo: Path, branch: str, base: str, *, title: str, body: str) -> Proposed:
    """Open a draft PR for `branch` against `base`, or say why not.

    Never raises and never fails a wave. The work is committed and pushed by the time this
    runs; a PR that could not be opened is a thing for a person to finish, not a reason to
    call the wave broken.

    A branch whose head already has a PR — open, closed, or merged — is not given a
    sibling (GRPH-1031). Open: the push already updated it. Closed: reopen, or say why
    not. Merged: say so; another PR for the same head is the defect.
    """
    if not find():
        return Proposed(branch=branch, skipped=True,
                        reason=f"gh is not installed, so nothing proposed {branch}. {INSTALL}")
    if not base:
        return Proposed(branch=branch, skipped=True,
                        reason="no base branch to propose against")
    try:
        already = existing(repo, branch)
        if already:
            return Proposed(branch=branch, url=already, skipped=True,
                            reason="already proposed")
        prior = existing_any(repo, branch)
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
        done = _gh(repo, "pr", "create", "--draft", "--head", branch,
                   "--base", base.split("/", 1)[-1], "--title", title, "--body", body)
    except (OSError, subprocess.SubprocessError) as exc:
        return Proposed(branch=branch, reason=f"gh could not run: {exc}")
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return Proposed(branch=branch,
                        reason=f"gh pr create failed: {tail[-1][:200] if tail else done.returncode}")
    url = next((line.strip() for line in (done.stdout or "").splitlines()
                if line.strip().startswith("http")), "")
    return Proposed(branch=branch, url=url, ok=bool(url),
                    reason="" if url else "gh reported success without a URL")


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


def replay_onto(repo: Path, base: str, commits: list[str]) -> tuple[str, str]:
    """Replay `commits` onto `base`. `(sha, "")`, or `("", why)` naming the commit that
    would not apply (GRPH-1086).

    Done in a throwaway worktree so the caller's checkout stays where it is. A commit
    whose parent is the current tip fast-forwards, which keeps the original sha for a
    prefix of the branch. Anything else is cherry-picked, so the result contains those
    commits and not the other item's. A patch that needs the other item's tree does not
    apply; that is the refusal, and the branch is left uncut.
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
                return "", f"commit {sha[:12]} depends on an earlier item's commit"
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
_PR_FIELDS = ("number,url,state,isDraft,mergeable,mergeStateStatus,headRefOid,headRefName,"
              "baseRefName,isCrossRepository,mergeCommit")


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
    """
    pr = item.get("pr")
    if isinstance(pr, str) and pr.strip():
        return pr.strip()
    branch = str(item.get("branch") or "").strip()
    latest = ""
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "url":
            continue
        url = str(e.get("url") or "").strip()
        if "/pull/" not in url:
            continue
        latest = url
        if branch and _receipt_branch(str(e.get("detail") or "")) == branch:
            return url
    return latest or branch


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


def merge(repo: Path, item_id: str, selector: str, *, reviewed: str,
          green: set[str], paths: list[str] | tuple[str, ...] | None = None,
          release: str = "", scans: list[ScanVerdict] | tuple[ScanVerdict, ...] = (),
          siblings: list[str] | tuple[str, ...] = ()) -> Merged:
    """Finish one item's merge, or say exactly which precondition stopped it.

    `reviewed` is the commit the sign-off attestation names; `green` is every commit CI has
    attested `suite_green` on. Both come from the ledger and neither is derived here, so a
    caller cannot pass a branch name for either — the comparison is commit to commit.

    `paths` are the item's own files. A head that moved only because main was merged in —
    those paths byte-identical to `reviewed` — is not a new review (GRPH-1032). Empty `paths`
    cannot prove that, so the head must still match.

    `release` is the project's release ref ("" when unset) and `scans` the item's
    `scans_clean` verdicts in ledger order, both read by the caller (GRPH-P57 D6). A PR
    whose base is the release ref also needs the latest verdict on the ACTUAL head to pass —
    never the reviewed commit, and with no GRPH-1032 exemption: merging main brings in the
    code the scan exists to check. Any other base, or an unset ref, skips it. Nothing here
    calls `scan_status` or reads Stack.

    `siblings` are the item's other PRs (`pr_receipts`). When the selected PR's head is not
    the reviewed commit, exactly one OPEN sibling at that commit is merged instead; none or
    several is a hold naming every PR checked and its head (GRPH-1219). After a merge, any
    other sibling still open is closed as superseded — best effort, never a failed merge.

    Never raises and never fails a wave. Every path returns a `Merged` that says what it did,
    and names the release ref it used.
    """
    got = _merge(repo, item_id, selector, reviewed=reviewed, green=green, paths=paths,
                 release=release, scans=scans, siblings=tuple(siblings or ()))
    if got.ok and siblings:
        got = replace(got, closed=_close_superseded(repo, got, siblings))
    return replace(got, release=release_phrase(release))


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
                siblings) -> tuple[str, str]:
    """`(sibling, hold)`: the one other open PR of the item whose head is `reviewed`, or the
    reason none can stand in, naming every PR checked with its head (GRPH-1219).

    Wave p61b held four signed-off, green PRs with "the branch moved after sign-off" when the
    selector had simply picked the item's other PR; the hold named only that one."""
    seen = [f"{_pr_ref({'url': url}, selector)} head {head[:12] or '?'}"]
    hits: list[str] = []
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
        if state == "OPEN" and same_commit(other, reviewed):
            hits.append(s)
    if len(hits) == 1:
        return hits[0], ""
    checked_prs = "; ".join(seen)
    if hits:
        return "", (f"no single PR to merge: {len(hits)} open PRs of {item_id} are at the "
                    f"reviewed commit {reviewed[:12]} (checked {checked_prs})")
    return "", (f"PR head {head[:12] or '?'} is not the reviewed commit {reviewed[:12]}, and "
                f"no other open PR of {item_id} is (checked {checked_prs}) — the branch "
                "moved after sign-off or the reviewed PR is gone; it needs review again, "
                "not a merge")


def _merge(repo: Path, item_id: str, selector: str, *, reviewed: str, green: set[str],
           paths, release: str, scans, siblings: tuple[str, ...] = ()) -> Merged:
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
    if pr is None:
        return Merged(item=item_id, selector=selector, skipped=True, reason=err)
    url = str(pr.get("url") or "")
    state = str(pr.get("state") or "").upper()
    if state == "MERGED":
        # Merged by somebody, or by the auto-merge this armed on an earlier tick. Either
        # way the trunk now carries the work and the ledger should say so.
        commit = _merge_commit(pr)
        return Merged(item=item_id, selector=selector, url=url, commit=commit,
                      ok=bool(commit), checked=("state=MERGED",),
                      reason="" if commit else "merged, but gh reported no merge commit")
    if state != "OPEN":
        return Merged(item=item_id, selector=selector, url=url, skipped=True,
                      reason=f"PR is {state or 'in an unknown state'}, not open")
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
    from .worktree import paths_unchanged

    head = str(pr.get("headRefOid") or "")
    if not same_commit(head, reviewed):
        if paths_unchanged(repo, reviewed, head, paths):
            checked.append("paths-unchanged")
        elif siblings:
            # GRPH-1219: the selector may have picked the item's other PR. One open sibling
            # at the reviewed commit is the PR the sign-off vouches for; merge that one, and
            # say which and why. The recursion gets no siblings, so it cannot loop.
            sibling, hold = _sibling_at(repo, item_id, selector, url, head, reviewed,
                                        siblings)
            if not sibling:
                return Merged(item=item_id, selector=selector, url=url,
                              checked=tuple(checked), reason=hold)
            got = _merge(repo, item_id, sibling, reviewed=reviewed, green=green,
                         paths=paths, release=release, scans=scans)
            why = (f"sibling={sibling} (selected {_pr_ref(pr, selector)} head "
                   f"{head[:12] or '?'} is not the reviewed commit)")
            return replace(got, checked=tuple(checked) + (why,) + got.checked)
        else:
            return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                          reason=f"PR head {head[:12] or '?'} is not the reviewed commit "
                                 f"{reviewed[:12]} — the branch moved after sign-off; it needs "
                                 "review again, not a merge")
    else:
        checked.append("head=reviewed")
    # CI runs on the commit the forge would merge. When the head is still the reviewed
    # commit, that is `reviewed`. When the head moved and this item's paths did not
    # (GRPH-1032), the suite that matters is the one on `head`: the reviewed commit can
    # stay red forever while the follow-up commit is the one CI attested.
    vouched = head if "paths-unchanged" in checked else reviewed
    if not any(same_commit(vouched, g) for g in green):
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"CI has not attested {CI_PREDICATE} on {vouched[:12]}")
    checked.append(f"ci={CI_PREDICATE}")

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

    # `--auto` is the honest verb for a supervisor: it says "merge when your requirements
    # are met", and `gh` merges immediately when a PR is already CLEAN. Squash, so the trunk
    # carries one commit per item and that commit is what the receipt names.
    try:
        done = _gh(repo, "pr", "merge", selector, "--squash", "--auto", timeout=120.0)
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
