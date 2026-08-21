"""One module per vendor, and the refusals that happen before a process starts.

PRD-22 S2. The point of an adapter is that a broken one fails at spawn, loudly, and
never produces a child that runs but never registers — the silent drop, which costs a
registration window and blames the vendor for the supervisor's mistake.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gbfleet.adapters import (
    ADAPTERS,
    AdapterUnavailable,
    Support,
    UnknownAdapter,
    VersionUnsupported,
    parse_version,
    resolve,
)
from gbfleet.seat import Seat
from gbfleet.worktree import SEAT_FILES, create

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "fleet-adapters.md"
SEAT = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="gbk_secret")


# --- versions do not share a scheme ------------------------------------------------


@pytest.mark.parametrize(
    "reported, expected",
    [
        ("2.1.233 (Claude Code)", (2, 1, 233)),          # claude, semver
        ("2026.04.17-787b533", (2026, 4, 17)),           # cursor-agent, CalVer + hash
        ("grok 1.0.5 (5115b46bc909) [stable]", (1, 0, 5)),  # grok, semver behind a name
    ],
)
def test_every_real_version_string_parses(reported: str, expected: tuple):
    """All three were read off a binary that was actually run.

    A semver-only parser would return nothing for `2026.04.17-787b533`, and a version
    that fails to parse is a version outside every range — so cursor-agent would refuse
    to spawn always, and the message would blame the vendor.
    """
    assert parse_version(reported) == expected


def test_calver_and_semver_each_order_within_their_own_scheme():
    assert parse_version("2026.04.17") < parse_version("2026.5.1")
    assert parse_version("2.1.233") < parse_version("2.2.0")
    # And a range is never compared across vendors, which is why this is safe.
    assert parse_version("2026.04.17") > parse_version("2.1.233")


def test_an_unparseable_version_is_refused_not_treated_as_zero():
    """`()` must not compare as "below everything and therefore fine to widen to".

    An empty parse means we do not know what is installed, and permitting it would make
    the check pass hardest exactly when it knows least.
    """
    support = Support(minimum=(1, 0))
    assert support.permits(()) is False
    assert support.permits((0, 9)) is False
    assert support.permits((1, 0)) is True


# --- selection is explicit ---------------------------------------------------------


@pytest.mark.parametrize("name", ["codex", "gpt-5", "CLAUDE", ""])
def test_an_unknown_vendor_is_refused_and_the_known_ones_named(name: str):
    with pytest.raises(UnknownAdapter) as exc:
        resolve(name)
    for known in ADAPTERS:
        assert known in str(exc.value)


def test_there_is_no_path_scan_for_whichever_cli_is_installed():
    """G5 is the one thing the supervisor is uniquely able to enforce, and it survives
    only if the vendor is named. A `which` loop over the registry would produce a fleet
    whose composition nobody chose."""
    source = (Path(__file__).resolve().parents[1] / "src" / "gbfleet" / "adapters").rglob("*.py")
    for path in source:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "shutil.which" in line:
                assert "adapter.binary" in line, (
                    f"{path.name}:{lineno} looks up a binary that was not named by the "
                    f"caller: {line.strip()}"
                )


def test_a_missing_binary_refuses_by_name(tmp_path: Path):
    with pytest.raises(AdapterUnavailable) as exc:
        resolve("claude", binary=tmp_path / "definitely-not-here")
    assert "claude" in str(exc.value)


def test_a_version_outside_the_range_refuses_at_resolve(tmp_path: Path):
    """The load-bearing refusal: before a worktree exists, before a seat is written,
    before anything can fail in a way that looks like the vendor's fault."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '1.0.0 (Claude Code)'\n", encoding="utf-8")
    fake.chmod(0o755)

    with pytest.raises(VersionUnsupported) as exc:
        resolve("claude", binary=fake)
    message = str(exc.value)
    assert "1.0.0" in message
    assert "2.0" in message, "the refusal must name the range it wanted"


def test_a_binary_inside_the_range_resolves(tmp_path: Path):
    """The control. Without it the refusal above could be refusing everything."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '2.1.233 (Claude Code)'\n", encoding="utf-8")
    fake.chmod(0o755)
    found = resolve("claude", binary=fake)
    assert found.adapter.name == "claude"
    assert found.binary == fake
    assert found.version == "2.1.233 (Claude Code)", (
        "the reported version is carried, not just checked — S6 puts it in every "
        "child's record so a failure can be tied to a build"
    )


# --- nothing carrying a credential goes on argv ------------------------------------


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_no_adapter_puts_a_secret_on_argv(name: str, git_repo: Path, tmp_path: Path):
    """argv is readable by every process on the machine.

    Declining to sandbox (D-k) is a different thing from publishing a live seat to `ps`.
    Parametrised over the registry so a new adapter is covered the day it is added
    rather than the day somebody remembers.
    """
    tree = create(git_repo, tmp_path / f"w-{name}", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text(f"code={SEAT.code}", encoding="utf-8")

    launch = ADAPTERS[name].launch(SEAT, tree, instruction, Path("/usr/bin/true"))
    joined = " ".join(launch.argv)

    assert SEAT.code not in joined, f"{name} put the enrolment code on argv"
    assert SEAT.api_key not in joined, f"{name} put the API key on argv"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_delivers_the_instruction_somehow(name: str, git_repo: Path, tmp_path: Path):
    """The other half: keeping it off argv is useless if it never arrives.

    Either the file is fed on stdin, or its PATH is on argv — grok takes
    `--prompt-file`. What must not happen is neither, which would leave a child with a
    seat and no idea what to do, failing as a silent drop.
    """
    tree = create(git_repo, tmp_path / f"w-{name}", "wave", "1")
    instruction = tmp_path / "instr"
    instruction.write_text("do the thing", encoding="utf-8")

    launch = ADAPTERS[name].launch(SEAT, tree, instruction, Path("/usr/bin/true"))
    on_stdin = launch.stdin_file == instruction
    by_path = str(instruction) in launch.argv

    assert on_stdin or by_path, f"{name} never hands the child its instructions"


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_a_seat_written_into_the_worktree_is_one_salvage_knows_about(
    name: str, git_repo: Path, tmp_path: Path
):
    """The trap D-f sets and D-g has to catch.

    A vendor that forces its config into the project directory puts a live credential
    somewhere `git status` can see it — so it must be in SEAT_FILES, or salvage commits
    it. A vendor that takes a path (claude) keeps it out of the tree entirely and needs
    no entry.
    """
    tree = create(git_repo, tmp_path / f"w-{name}", "wave", "1")
    seat_path = ADAPTERS[name].seat_path(tree.path)

    try:
        relative = str(Path(seat_path).resolve().relative_to(tree.path.resolve()))
    except ValueError:
        return  # outside the worktree: nothing for salvage to exclude

    assert relative in SEAT_FILES, (
        f"{name} writes its seat to {relative} inside the worktree, and salvage does "
        "not know to exclude it — a WIP commit would carry a live credential"
    )


def test_claude_keeps_its_seat_out_of_the_repository_entirely(git_repo: Path, tmp_path: Path):
    """Named separately because it is the good case and worth protecting.

    `--mcp-config <path>` means the credential never enters the project directory: it
    cannot be committed by salvage, cannot be seen by `git status`, and needs no entry
    in SEAT_FILES. The supervisor removes it at reap because the worktree reaper cannot.
    """
    tree = create(git_repo, tmp_path / "w1", "wave", "1")
    seat_path = ADAPTERS["claude"].seat_path(tree.path)
    with pytest.raises(ValueError):
        Path(seat_path).resolve().relative_to(tree.path.resolve())


# --- the support matrix ------------------------------------------------------------


def _matrix_rows() -> dict[str, str]:
    """The vendor rows of the table, keyed by name.

    Parsed rather than searched for. Both of these tests first checked whether the name
    appeared ANYWHERE in the document, and both survived a sabotage that deleted the
    table row — because every vendor is also discussed in the prose underneath. A docs
    guard that a paragraph can satisfy is the absence-reads-clean defect pointed at
    documentation.
    """
    rows: dict[str, str] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = cells[0].strip("`")
        if name in {"vendor", ""} or set(name) <= set("- :"):
            continue
        rows[name] = line
    return rows


def test_the_matrix_lists_exactly_the_registered_adapters():
    """A support matrix that has drifted from the registry is worse than none: it is the
    thing somebody checks before installing a vendor."""
    rows = _matrix_rows()
    assert rows, "no vendor rows found — the table moved and this guard stopped guarding"
    assert set(ADAPTERS) <= set(rows), (
        f"registered but missing a table row: {sorted(set(ADAPTERS) - set(rows))}"
    )
    assert set(rows) - set(ADAPTERS) == {"codex"}, (
        f"the table names vendors that are not registered: "
        f"{sorted(set(rows) - set(ADAPTERS) - {'codex'})}"
    )


def test_the_matrix_says_codex_is_not_implemented():
    """The absence has to be visible IN THE TABLE. A vendor missing from a matrix reads
    as an oversight; one listed as deliberately absent, with the reason, is a decision —
    and it is the row somebody scans, not the paragraph."""
    assert "codex" not in ADAPTERS
    row = _matrix_rows().get("codex")
    assert row, "codex is absent from the registry AND from the table, which reads as forgotten"
    assert "not implemented" in row.lower()


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_says_what_it_was_verified_against(name: str):
    """`verified_against` is separate from the range on purpose. A declared range is a
    claim; a claim nobody has exercised must not read like one that has."""
    support = ADAPTERS[name].support
    assert support.verified_against, f"{name} declares a range nobody has run"
    assert parse_version(support.verified_against)
    assert support.permits(parse_version(support.verified_against)), (
        f"{name} says it was verified against a version its own range refuses"
    )


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_the_matrix_quotes_the_version_that_was_actually_run(name: str):
    text = MATRIX.read_text(encoding="utf-8")
    assert ADAPTERS[name].support.verified_against in text


# --- against the binaries that are actually here -----------------------------------


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_a_real_installed_binary_resolves(name: str):
    """Run against real data, not a fixture.

    Skipped where the vendor is not installed rather than mocked, because a mocked
    `--version` proves the parser and nothing about the vendor. Where the binary IS
    here, this is the only test that would notice a vendor changing its version format.
    """
    binary = shutil.which(ADAPTERS[name].binary)
    if binary is None:
        pytest.skip(f"{ADAPTERS[name].binary} is not installed here")

    found = resolve(name)
    assert found.adapter.name == name
    assert Path(found.binary) == Path(binary)
    assert found.version, "a real binary reported no version at all"
