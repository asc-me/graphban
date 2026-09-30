"""GRPH-984 — is the gbfleet running here the gbfleet in the checkout it supervises?

**Accept:** doctor compares the INSTALLED package against the checkout by content and reports
PASS, FAIL, or UNKNOWN — with "could not compare" as its own outcome, never as agreement.

**What went wrong.** `--version` cannot answer this and reads as though it can. GRPH-974's
commit instruction merged without a version bump, so `fleet/pyproject.toml` and the installed
wheel both said `0.9.0` while differing in behaviour:

    $ gbfleet --version                      ->  gbfleet 0.9.0
    $ grep version fleet/pyproject.toml      ->  version = "0.9.0"
    $ from gbfleet.seat import COMMIT          ->  ImportError

`uv tool install` had no reason to refresh, a whole wave ran on the old instruction text, and
the near-miss was writing that wave up as field validation of a fix that was not running.

Compared by CONTENT, because the version string is the thing that lied.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from gbfleet import doctor as doctor_mod
from gbfleet.doctor import FAIL, PASS, UNKNOWN, Report, check_installed_matches_checkout


def _fake_checkout(root: Path, *, from_installed: Path) -> Path:
    """A repo-shaped tree whose fleet/src/gbfleet is a copy of the installed package."""
    dest = root / "fleet" / "src" / "gbfleet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(from_installed, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return root


def _installed() -> Path:
    return Path(doctor_mod.__file__).resolve().parent


def _only(report: Report):
    rows = [f for f in report.findings if f.name == "installed matches checkout"]
    assert len(rows) == 1, report.findings
    return rows[0]


def test_a_checkout_that_matches_the_installed_package_passes(tmp_path: Path):
    """The ordinary case: whatever installed it, the code is the same code."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    report = Report()

    check_installed_matches_checkout(report, repo)

    assert _only(report).status == PASS


def test_a_checkout_that_differs_fails_and_says_so(tmp_path: Path):
    """THE ONE THAT MATTERS. One changed line is enough — this is exactly the shape of the
    GRPH-974 miss, where a merged fix was absent from the wheel and both said 0.9.0."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    seat = repo / "fleet" / "src" / "gbfleet" / "seat.py"
    seat.write_text(seat.read_text() + "\n# a fix that merged without a version bump\n")
    report = Report()

    check_installed_matches_checkout(report, repo)

    row = _only(report)
    assert row.status == FAIL
    assert "DIFFERS" in row.detail
    # The remedy must name the command that actually works. `uv tool upgrade` is a no-op on a
    # pinned install and exits saying "Nothing to upgrade", which reads as confirmation.
    assert "graphban-fleet@latest" in row.remedy
    assert "upgrade" not in row.remedy.split("(")[0]


def test_an_editable_install_is_the_same_tree_and_says_that(tmp_path: Path):
    """An editable install resolves INTO the checkout, so the two are one object. Reported as
    the same tree rather than treated as a special case that skips the check."""
    installed = _installed()
    # The repo whose fleet/src/gbfleet IS the installed path.
    repo = installed.parent.parent.parent
    report = Report()

    check_installed_matches_checkout(report, repo)

    row = _only(report)
    assert row.status == PASS
    assert "editable" in row.detail


# ---- the third outcome, which is the point of the item -----------------------------------

def test_no_repository_is_unknown_not_agreement(tmp_path: Path):
    """UNKNOWN, never PASS. A green tick on the one situation this exists to catch is worse
    than no check, because it is what an operator relies on instead of looking."""
    report = Report()

    check_installed_matches_checkout(report, None)

    row = _only(report)
    assert row.status == UNKNOWN
    assert row.status != PASS


def test_a_directory_that_is_not_a_checkout_is_unknown(tmp_path: Path):
    """A path with no `fleet/src/gbfleet` cannot be compared. Saying so is the answer; calling
    it a match would mean every wrong `--repo` reported agreement."""
    report = Report()

    check_installed_matches_checkout(report, tmp_path)

    row = _only(report)
    assert row.status == UNKNOWN
    assert "not a gbfleet checkout" in row.detail


def test_an_unreadable_tree_is_unknown(tmp_path: Path, monkeypatch):
    """The digest itself can fail — permissions, a vanished file mid-walk. That is still not
    agreement."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    monkeypatch.setattr(doctor_mod, "_package_digest", lambda root: None)
    report = Report()

    check_installed_matches_checkout(report, repo)

    row = _only(report)
    assert row.status == UNKNOWN
    assert "could not read" in row.detail


def test_an_empty_package_tree_is_unknown_not_a_match(tmp_path: Path):
    """Two empty digests must not compare equal. A tree with no `.py` files at all is
    unreadable, not identical — the absence-reads-as-clean shape, in the digest."""
    dest = tmp_path / "repo" / "fleet" / "src" / "gbfleet"
    dest.mkdir(parents=True)
    report = Report()

    check_installed_matches_checkout(report, tmp_path / "repo")

    row = _only(report)
    assert row.status == UNKNOWN
    assert doctor_mod._package_digest(dest) is None


def test_package_data_counts_as_a_difference(tmp_path: Path):
    """`matrix.toml` ships INSIDE the package and decides model and tier selection. A digest of
    `*.py` alone read a change to it as a match — and `aac27865`, the qwen-code model row, was
    one of the three commits that motivated the 0.10.0 release this check exists because of. So
    the first version would have passed the exact drift it was written for."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    toml = repo / "fleet" / "src" / "gbfleet" / "matrix.toml"
    assert toml.exists(), "matrix.toml must be in the package for this to mean anything"
    toml.write_text(toml.read_text() + '\n# a model row added without a version bump\n')
    report = Report()

    check_installed_matches_checkout(report, repo)

    assert _only(report).status == FAIL


def test_compiled_artefacts_are_not_a_difference(tmp_path: Path):
    """The other half. An installed tree has `__pycache__` and a fresh checkout does not, so
    counting derived files would report every install as drifted and the check would be ignored
    within a day."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    cache = repo / "fleet" / "src" / "gbfleet" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "doctor.cpython-312.pyc").write_bytes(b"\x00compiled")
    report = Report()

    check_installed_matches_checkout(report, repo)

    assert _only(report).status == PASS


def test_the_failure_names_both_sides_and_does_not_invent_an_order(tmp_path: Path):
    """Acceptance says the FAIL names both sides, and asserting only "DIFFERS" left that
    unpinned — dropping the checkout path from the message stayed green.

    It must also NOT claim which side is ahead: the version strings are equal in the case this
    exists for and file contents carry no order, so inventing a direction would send an operator
    to reinstall when the checkout was the stale one."""
    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    seat = repo / "fleet" / "src" / "gbfleet" / "seat.py"
    seat.write_text(seat.read_text() + "\n# drift\n")
    report = Report()

    check_installed_matches_checkout(report, repo)

    detail = _only(report).detail
    assert str(_installed()) in detail, "the installed path is not named"
    assert str(repo / "fleet" / "src" / "gbfleet") in detail, "the checkout path is not named"
    assert "cannot say which is ahead" in detail


def test_doctor_actually_reports_the_finding(tmp_path: Path, monkeypatch):
    """THE CALL, and the reviewer of PR #902 showed why a source grep is not it: with `run()`
    changed to `check_installed_matches_checkout(report, None)` every test here still passed,
    and every real doctor run would report UNKNOWN 'no repository' forever with nothing
    noticing. Commenting the call out keeps the substring too.

    So this runs `doctor.run` and reads the finding it produced."""
    import io

    repo = _fake_checkout(tmp_path / "repo", from_installed=_installed())
    seat = repo / "fleet" / "src" / "gbfleet" / "seat.py"
    seat.write_text(seat.read_text() + "\n# drift\n")
    # The slow and networked checks are not what this is about.
    for name in ("check_host", "check_memory", "check_sandbox", "check_credential_protection",
                 "check_state_and_lock", "check_workspace", "check_seats",
                 "check_supervision_mode", "check_service", "check_matrix"):
        monkeypatch.setattr(doctor_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(doctor_mod, "check_repository", lambda report, repo: Path(repo))

    report = doctor_mod.run(repo=repo, out=io.StringIO())

    rows = [f for f in report.findings if f.name == "installed matches checkout"]
    assert rows, "doctor produced no such finding — the check is not wired in"
    assert rows[0].status == FAIL, rows[0].detail
