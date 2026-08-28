"""One module per vendor, and the refusals that happen before a process starts.

PRD-22 S2. The point of an adapter is that a broken one fails at spawn, loudly, and
never produces a child that runs but never registers — the silent drop, which costs a
registration window and blames the vendor for the supervisor's mistake.
"""

from __future__ import annotations

import re
import shutil
import sys
import subprocess
from pathlib import Path

import pytest

import gbfleet
from gbfleet.adapters import (
    ADAPTERS,
    Adapter,
    AdapterUnavailable,
    Support,
    UnknownAdapter,
    VersionUnsupported,
    parse_version,
    resolve,
)
from gbfleet.seat import Seat
from gbfleet.worktree import SEAT_FILES, create, seat_key
from conftest import console_script, make_stub_binary  # noqa: E402

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
    fake = make_stub_binary(tmp_path / "claude", prints="1.0.0 (Claude Code)")

    with pytest.raises(VersionUnsupported) as exc:
        resolve("claude", binary=fake)
    message = str(exc.value)
    assert "1.0.0" in message
    assert "2.0" in message, "the refusal must name the range it wanted"


def test_a_binary_inside_the_range_resolves(tmp_path: Path):
    """The control. Without it the refusal above could be refusing everything."""
    fake = make_stub_binary(tmp_path / "claude", prints="2.1.233 (Claude Code)")
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

    relative = seat_key(seat_path, tree.path)
    if relative is None:
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


MATRIX_HEADING = "## The matrix"


def _rows_under(heading: str) -> dict[str, str]:
    """The vendor rows of ONE table, keyed by name.

    Parsed rather than searched for. Both of the matrix tests below first checked whether
    the name appeared ANYWHERE in the document, and both survived a sabotage that deleted
    the table row — because every vendor is also discussed in the prose underneath. A docs
    guard that a paragraph can satisfy is the absence-reads-clean defect pointed at
    documentation.

    **Scoped to one section**, which it was not at first. It read every table in the file
    and got away with it because the model table and the tuning table also happen to have
    vendor names in their first column. The moment a table appeared whose first column was
    something else — `gbagent`'s exit codes — the guard started reporting `75` and `other`
    as unregistered vendors. It was reading the right rows by luck.

    Takes the heading as an argument so the model and tuning tables get the guard the
    matrix already had (GRPH-528). Those two arrived after it and did not inherit it:
    inverting grok's `validated?` column from **yes** to **no**, or exchanging claude's and
    grok's tuning knobs, left the whole fleet suite green.
    """
    rows: dict[str, str] = {}
    inside = False
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line.strip() == heading
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = cells[0].strip("`")
        if name in {"vendor", ""} or set(name) <= set("- :"):
            continue
        rows[name] = line
    return rows


def _matrix_rows() -> dict[str, str]:
    """THE matrix's rows. One parser, so the three tables cannot drift apart in how they
    are read."""
    return _rows_under(MATRIX_HEADING)


def test_the_matrix_section_is_where_this_thinks_it_is():
    """The scoping above is only as good as the heading it keys on. A renamed section would
    leave every guard below parsing nothing and passing."""
    assert MATRIX_HEADING in MATRIX.read_text(encoding="utf-8")
    assert len(_matrix_rows()) >= 4, "the matrix section parsed almost nothing"


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


@pytest.mark.parametrize("name", sorted(set(ADAPTERS) - {"gbagent"}))
def test_a_real_installed_binary_resolves(name: str):
    """Run against real data, not a fixture.

    Skipped where the vendor is not installed rather than mocked, because a mocked
    `--version` proves the parser and nothing about the vendor. Where the binary IS
    here, this is the only test that would notice a vendor changing its version format.

    `gbagent` is excluded because for it a skip would be wrong — see below.
    """
    binary = shutil.which(ADAPTERS[name].binary)
    if binary is None:
        pytest.skip(f"{ADAPTERS[name].binary} is not installed here")

    found = resolve(name)
    assert found.adapter.name == name
    assert Path(found.binary) == Path(binary)
    assert found.version, "a real binary reported no version at all"


def test_the_first_party_binary_resolves_and_is_never_skipped():
    """`gbagent` absent is a broken build, not an absent vendor (PRD-24 D8).

    It ships in this same wheel, so "not installed here" cannot be a legitimate outcome the
    way it is for `grok`. This test also has to find it WITHOUT `PATH`: CI runs
    `.venv/bin/python -m pytest` without activating the venv, so `shutil.which("gbagent")`
    is None on the very machine where the package is definitely installed. Skipping on that
    would leave the support matrix claiming a row that CI never actually exercises — the
    docstring in `adapters/gbagent.py` says this is re-verified on every run, and this is
    what makes that sentence true rather than decorative.
    """
    binary = console_script("gbagent")
    assert binary.exists(), (
        f"gbagent is not installed at {binary}. It ships in this distribution; a missing "
        "console script means the install is broken. Run: uv pip install -e '.[dev]'"
    )

    found = resolve("gbagent", binary=binary)

    assert found.adapter.name == "gbagent"
    assert found.version == f"gbagent {gbfleet.__version__}"
    assert found.adapter.support.exact == parse_version(gbfleet.__version__)


def test_a_gbagent_from_another_install_is_refused():
    """The only version mismatch that can actually happen here, and the reason the pin is
    exact rather than a range. A range wide enough to be useful would accept it."""
    other = parse_version(gbfleet.__version__)

    assert not ADAPTERS["gbagent"].support.permits(other[:-1] + (other[-1] + 1,))
    assert ADAPTERS["gbagent"].support.permits(other)
    assert "exactly" in ADAPTERS["gbagent"].support.describe()


def test_the_model_table_keeps_its_caveats():
    """A measurement table without its limits reads as a recommendation (GRPH-518).

    Two models, five runs, one repository is thin evidence, and the two sentences most likely
    to be tidied away are exactly the two that stop a reader over-reading it: how thin it is,
    and that nobody owns the routing question it points at.

    Whitespace-normalised, because prose gets rewrapped and a guard that fails when a sentence
    moves across a line break is noise that teaches people to weaken it.
    """
    text = " ".join(MATRIX.read_text(encoding="utf-8").split())

    assert "qwen3.6:35b-a3b-coding-mtp-det" in text and "qwen3-coder:30b" in text, (
        "the table has lost the models it was measured on"
    )
    # The ownership claim is the one sentence here that a LEDGER write can falsify without
    # touching this repository, and nothing in the tree records PRD-11's status — the PRD
    # index only carries PRDs with a repo copy, and PRD-11 is ledger-only. So it cannot be
    # pinned to a value; it is DATED instead, which keeps it true as a historical
    # observation and tells the reader to re-check (GRPH-527).
    assert "as measured on" in text, (
        "the PRD-11 ownership claim lost its date. Stated flat it becomes false the moment "
        "PRD-11 is approved, and no test in this repository can notice — there is no "
        "committed record of a ledger-only PRD's status to compare against."
    )

    assert "thin evidence and should be read as thin" in text, (
        "the limits of five runs on two models have gone, so this now reads as a ranking"
    )
    assert "Nothing owns the routing question" in text, (
        "PRD-24 §4 defers model routing to PRD-11, which has no approved baseline — dropping "
        "that leaves the arc's load-bearing variable looking owned when it is not"
    )


# ---- the model and tuning tables cannot drift from the adapters (GRPH-528) ----------------

MODEL_HEADING = "## Naming a model (GRPH-483)"
TUNING_HEADING = "## Per-vendor tuning (GRPH-484)"


def test_the_model_and_tuning_sections_are_where_this_thinks_they_are():
    """Same guard-on-the-guard the matrix has. A renamed section leaves the two tests below
    parsing nothing and passing, which is the failure they exist to prevent."""
    for heading in (MODEL_HEADING, TUNING_HEADING):
        assert heading in MATRIX.read_text(encoding="utf-8"), f"{heading} moved or was renamed"
        assert len(_rows_under(heading)) >= 4, f"{heading} parsed almost nothing"


def test_the_model_table_agrees_with_the_adapters_about_who_can_be_checked():
    """`an unvalidated pass-through must not read the same as a verified one` — GRPH-483.

    The doc says per vendor whether a named model can be checked before spawning, and
    nothing kept that true: inverting grok's column from **yes** to **no** left the whole
    fleet suite green, and an operator reading it would skip validating the one model they
    could have validated.

    Whether an adapter ATTEMPTS a listing is visible from the class, because `claude` alone
    inherits the base `known_models`. So the correspondence is checkable without any vendor
    binary — which is what makes this cheap enough to be worth having.
    """
    rows = _rows_under(MODEL_HEADING)
    assert set(rows) == set(ADAPTERS), (
        f"the model table lists {sorted(rows)}; the registry holds {sorted(ADAPTERS)}"
    )

    for name, adapter in ADAPTERS.items():
        attempts = type(adapter).known_models is not Adapter.known_models
        says_no = rows[name].lower().split("|")[3].strip().startswith("**no.")
        assert says_no is not attempts, (
            f"{name}: the table says {'it cannot be checked' if says_no else 'it can be'}, "
            f"but the adapter {'does not' if not attempts else 'does'} attempt a listing. "
            "One of the two is wrong, and the doc is what an operator reads."
        )


def test_the_tuning_table_names_the_knob_each_vendor_actually_has():
    """A caller following a wrong row passes a knob the vendor refuses, and gets a message
    naming the one it DOES accept — while the doc that sent them there stays wrong.

    Exchanging claude's and grok's rows left the fleet suite green. `adapter.tuning` is a
    plain set, so the rows are checkable directly against it.
    """
    rows = _rows_under(TUNING_HEADING)
    assert set(rows) == set(ADAPTERS), (
        f"the tuning table lists {sorted(rows)}; the registry holds {sorted(ADAPTERS)}"
    )

    # The knob names as an operator would type them, mapped to the field an adapter declares.
    spellings = {"fallback_model": "--fallback-model", "effort": "--reasoning-effort",
                 "turns": "--turns", "window": "--window"}
    for name, adapter in ADAPTERS.items():
        row = rows[name]
        for field in adapter.tuning:
            flag = spellings.get(field)
            assert flag, f"{field} has no known spelling — add it to this test's map"
            assert flag in row, (
                f"{name} declares tuning={sorted(adapter.tuning)} but its row does not "
                f"mention {flag}: {row}"
            )
        for field, flag in spellings.items():
            if field not in adapter.tuning:
                assert flag not in row, (
                    f"{name}'s row offers {flag}, which it does not accept — "
                    f"it declares {sorted(adapter.tuning) or 'no knobs'}: {row}"
                )
