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
import shutil
import subprocess
from dataclasses import dataclass
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


def propose(repo: Path, branch: str, base: str, *, title: str, body: str) -> Proposed:
    """Open a draft PR for `branch` against `base`, or say why not.

    Never raises and never fails a wave. The work is committed and pushed by the time this
    runs; a PR that could not be opened is a thing for a person to finish, not a reason to
    call the wave broken.
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


def describe(branch: str, items: list[str], commit_subject: str = "") -> tuple[str, str]:
    """Title and body for work a wave produced.

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
    named = ", ".join(items)
    if commit_subject and named:
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
#: What `gh pr view --json` is asked for. `mergeCommit` is null until the PR is merged.
_PR_FIELDS = ("number,url,state,isDraft,mergeable,mergeStateStatus,headRefOid,headRefName,"
              "isCrossRepository,mergeCommit")


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


def pr_selector(item: dict) -> str:
    """What to hand `gh pr view`: the item's PR if it names one, else the PR receipt the
    supervisor wrote when it proposed the branch, else the branch itself.

    A signed-off item's `branch` is routinely empty, which is why the receipt is consulted
    before it — the URL `propose_branch` recorded is the one thing that reliably survives.
    """
    pr = item.get("pr")
    if isinstance(pr, str) and pr.strip():
        return pr.strip()
    for e in item.get("evidence") or []:
        if not isinstance(e, dict) or e.get("kind") != "url":
            continue
        url = str(e.get("url") or "").strip()
        if "/pull/" in url:
            return url
    return str(item.get("branch") or "").strip()


def view(repo: Path, selector: str) -> tuple[dict | None, str]:
    """`(pr, error)`. `pr` is None when there is no PR for `selector` or `gh` could not say.

    Two Nones with different `error`s, on purpose: "no PR found for that branch" is a fact about
    the branch and ends the attempt as `skipped`; a `gh` that could not run is a fact about
    this machine and reads the same way, but the reason must say which.
    """
    try:
        done = _gh(repo, "pr", "view", selector, "--json", _PR_FIELDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"gh could not run: {exc}"
    if done.returncode != 0:
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


def merge(repo: Path, item_id: str, selector: str, *, reviewed: str,
          green: set[str]) -> Merged:
    """Finish one item's merge, or say exactly which precondition stopped it.

    `reviewed` is the commit the sign-off attestation names; `green` is every commit CI has
    attested `suite_green` on. Both come from the ledger and neither is derived here, so a
    caller cannot pass a branch name for either — the comparison is commit to commit.

    Never raises and never fails a wave. Every path returns a `Merged` that says what it did.
    """
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
    head = str(pr.get("headRefOid") or "")
    if head != reviewed:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"PR head {head[:12] or '?'} is not the reviewed commit "
                             f"{reviewed[:12]} — the branch moved after sign-off; it needs "
                             "review again, not a merge")
    checked.append("head=reviewed")
    if reviewed not in green:
        return Merged(item=item_id, selector=selector, url=url, checked=tuple(checked),
                      reason=f"CI has not attested {CI_PREDICATE} on {reviewed[:12]}")
    checked.append(f"ci={CI_PREDICATE}")

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
