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
