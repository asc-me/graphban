"""CI as an attestation adapter (GRPH-551).

`fleet.sign_off` was the only adapter, so a project with no running reviewer accumulates
items in `review` that nothing can complete. This is the cheap one — and the second, which
is the smallest number that proves the port is not shaped around a single implementation.

The end-to-end path needs GitHub Actions, a configured secret and a reachable Graphban, so
it cannot run here. Everything below is what CAN be checked: which items a message refers
to, what the receipt claims, what happens when the secret is missing, and — the one that
matters most — that the attestation cannot be reached unless CI actually passed.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import attest_ci  # noqa: E402

WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def _gate_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["ci"]["steps"]


# ---- the structural guarantee --------------------------------------------------------

def test_the_attestation_cannot_be_reached_unless_ci_passed():
    """THE test. An attestation minted from a masked failure certifies work nobody checked,
    and the gate downstream has no way to tell.

    The guarantee is positional, not conditional: steps stop at the first failure, so a step
    placed after the check that exits 1 on a bad result is unreachable when the result is
    bad. If someone reorders these, the protection is gone and nothing else would say so.
    """
    steps = _gate_steps()
    names = [s.get("name", "") for s in steps]
    check = next(i for i, s in enumerate(steps)
                 if "exit 1" in (s.get("run") or ""))
    attest = next(i for i, s in enumerate(steps)
                  if "attest_ci.py" in (s.get("run") or ""))

    assert attest > check, (
        f"the attestation step runs at position {attest}, before or at the result check at "
        f"{check} — it would attest a failing run. Order: {names}")


def test_the_attestation_lives_in_the_gate_job_not_a_job_of_its_own():
    """A separate job would have to appear in `ci.needs` to satisfy
    `test_ci_gate_covers_every_job`, and a job that runs after the gate cannot be one of its
    dependencies. Adding an exclusion would weaken that guard to fit this feature."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert not [n for n in jobs if "attest" in n.lower()], \
        "the attestation became its own job; it belongs in the gate job's steps"


# ---- which items a run vouches for ---------------------------------------------------

def test_it_finds_the_items_a_pull_request_names():
    assert attest_ci.item_keys("Fixes GRPH-541 and AL-262") == ["GRPH-541", "AL-262"]


def test_a_version_string_is_not_an_item_key():
    """`\\b[A-Z]+-\\d+\\b` is the tempting pattern and it matches `UTF-8`, `SHA-256`,
    `RFC-2119`. A PR mentioning no item would then attest something that does not exist —
    or worse, something unrelated that does."""
    noise = "encoded UTF-8, hashed SHA-256, per RFC-2119, using AES-256 and ISO-8601"

    assert attest_ci.item_keys(noise) == [], \
        f"a version string was read as an item key: {attest_ci.item_keys(noise)}"


def test_the_same_item_named_twice_is_attested_once():
    keys = attest_ci.item_keys("GRPH-1 in the title", "GRPH-1 in the branch")

    assert keys == ["GRPH-1"]


# ---- what the receipt claims ---------------------------------------------------------

def test_the_receipt_names_the_one_thing_ci_checked():
    """An adapter claiming more than it checked is how a gate quietly starts accepting less
    than it appears to. The gate records WHICH predicates ran precisely so a later reader
    can see that this completion rests on a test run and nothing else."""
    receipt = attest_ci.attestation(commit="a" * 40, branch="feat/x", run_url="https://ci/1")

    assert receipt["kind"] == "attestation"
    assert receipt["adapter"] == "github-actions"
    assert [p["name"] for p in receipt["predicates"]] == ["suite_green"]
    assert receipt["run_ref"] == "https://ci/1"


def test_the_receipt_satisfies_the_gate_it_is_written_for():
    """The two halves are built in different files and could drift apart. If the shape this
    emits ever stops being one `items.has_valid_attestation` accepts, CI would attest every
    green run and every completion would still be refused."""
    from app.services import items as items_svc

    stored = items_svc.normalize_evidence(
        [attest_ci.attestation(commit="b" * 40, branch="main")])

    assert items_svc.has_valid_attestation(stored, commit="b" * 40), \
        "CI's receipt does not satisfy the completion gate"


# ---- the refusals --------------------------------------------------------------------

def test_a_missing_secret_skips_loudly_and_does_not_fail_the_build(monkeypatch, capsys):
    """Failing every PR because a repository secret is unset would be worse than the
    problem. Skipping in silence would be the ships-inert failure this whole PRD is about.
    So it skips, exits 0, and says exactly what is missing and what the consequence is."""
    monkeypatch.delenv("GRAPHBAN_URL", raising=False)
    monkeypatch.delenv("GRAPHBAN_GATE_KEY", raising=False)

    rc = attest_ci.main(["--commit", "c" * 40, "--text", "GRPH-1"])
    out = capsys.readouterr().out

    assert rc == 0, "a missing secret failed the build"
    assert "GRAPHBAN_URL" in out and "GRAPHBAN_GATE_KEY" in out, \
        "the skip does not name what is missing"
    assert "cannot reach `done`" in out, \
        "the skip does not say what it costs, so nobody has a reason to configure it"


def test_a_run_naming_no_item_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")

    assert attest_ci.main(["--commit", "d" * 40, "--text", "no keys here"]) == 0
    assert "nothing to attest" in capsys.readouterr().out


def test_a_refused_attestation_fails_the_step(monkeypatch, capsys):
    """Swallowing the error would make a misconfigured key look identical to a successful
    attestation — the item stays uncompletable and the operator goes looking at the gate,
    which is working correctly."""
    def refuse(url, key, item_key, receipt, timeout=15.0):
        raise RuntimeError(f"{item_key}: graphban refused the attestation: unauthorized")

    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(attest_ci, "post", refuse)

    rc = attest_ci.main(["--commit", "e" * 40, "--text", "GRPH-1"])

    assert rc == 1, "a refused attestation reported success"
    assert "::error" in capsys.readouterr().out


def test_every_item_is_attempted_before_anything_is_reported(monkeypatch, capsys):
    """Stopping at the first failure would leave a multi-item PR half attested with no
    record of which half."""
    tried = []

    def flaky(url, key, item_key, receipt, timeout=15.0):
        tried.append(item_key)
        if item_key == "GRPH-1":
            raise RuntimeError("nope")

    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(attest_ci, "post", flaky)

    rc = attest_ci.main(["--commit", "f" * 40, "--text", "GRPH-1 and GRPH-2"])

    assert tried == ["GRPH-1", "GRPH-2"], f"it gave up after the first failure: {tried}"
    assert rc == 1


# ---- a PR body is not a shell script -------------------------------------------------

UNTRUSTED = ("github.event", "github.head_ref")


def test_no_workflow_interpolates_untrusted_input_into_a_shell_script():
    """Script injection, and the reason it is asserted rather than remembered.

    GitHub Actions substitutes a `${{ }}` expression into the script TEXT before any shell
    sees it. A pull request title, body or branch name is writable by whoever opened the
    pull request — so interpolating one into `run:` makes it shell source. The gate job
    holds `GRAPHBAN_GATE_KEY`, so a payload would run with the credential that certifies
    completion, and exfiltrating it is one `curl` away.

    This is not a theoretical class. The first version of the attestation step interpolated
    the PR body directly; the PR that exposed it was an ordinary one whose markdown carried
    backticks and quotes, and CI reported a shell syntax error — the harmless end of exactly
    the same defect.

    The fix is to pass values through `env`, where they are handed to the process rather
    than pasted into the program. `needs.*.result` stays permitted: it is a fixed set of
    runner-produced words, not anything a contributor can write.
    """
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows"
    offenders = []
    for wf in sorted(root.glob("*.yml")):
        spec = yaml.safe_load(wf.read_text())
        for job_name, job in (spec.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                for expr in re.findall(r"\$\{\{([^}]*)\}\}", step.get("run") or ""):
                    if any(u in expr for u in UNTRUSTED):
                        offenders.append(
                            f"{wf.name}:{job_name}: `${{{{{expr.strip()}}}}}` in a run: block")

    assert not offenders, (
        "attacker-controllable values are interpolated into shell scripts: "
        + "; ".join(offenders)
        + " — pass them through `env:` and read them as shell variables instead"
    )


def test_the_attestation_step_still_receives_what_it_needs():
    """The control. Deleting the interpolation entirely would satisfy the test above and
    leave the step attesting nothing, with no item keys and no commit to bind to."""
    steps = _gate_steps()
    step = next(s for s in steps if "attest_ci.py" in (s.get("run") or ""))
    env = step.get("env") or {}

    assert {"HEAD_SHA", "HEAD_REF", "PR_TITLE", "PR_BODY"} <= set(env), \
        f"the step lost the inputs it needs to identify and bind an attestation: {sorted(env)}"
    for var in ("HEAD_SHA", "HEAD_REF", "PR_TITLE", "PR_BODY"):
        assert f"${var}" in step["run"], f"{var} is set but never read"
