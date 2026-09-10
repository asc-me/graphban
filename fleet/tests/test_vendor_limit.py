"""A vendor account limit is not an adapter crash (GRPH-829).

Measured on a real wave. Three children died in under a second each and the wave ended
`{"ok": false, "reason": "cap", "spawned": 6, "minted": 0}`. What the operator was shown:

    FAILED p11e-4: adapter 'claude': child exited 1 before registering.
    stderr tail:

The tail is empty because stderr was empty. The cause was 67 bytes in `stdout.log`:

    You've hit your session limit · resets 12:20pm (America/New_York)

So the entire class of vendor-account failure — rate limited, out of quota — presented
identically to a broken adapter, which is the exact misattribution `Adapter.notes` says this
package exists to prevent. It also ended the wave on a reason that invites the wrong fix:
`cap` means "you hit YOUR --max-children" and reads as "raise it".

Three separate claims are pinned here, because fixing one and not the others still leaves an
operator debugging the wrong component:

1. the message is FOUND, wherever the vendor put it;
2. the failure is a distinguishable TYPE, so a wave can act on it;
3. nothing guesses — a real crash stays a real crash.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet.adapters import ADAPTERS
from gbfleet.spawn import Launch, LaunchFailed, VendorLimit, await_registration, spawn
from gbfleet.seat import Seat

SEAT = Seat(code="WORKER-7F3K", server_url="https://gb.invalid", api_key="gbk_test")

#: Verbatim, including the separator and the parenthesised zone. Quoting the vendor rather
#: than paraphrasing is the point of the field: the operator wants the clock.
SAID = "You've hit your session limit · resets 12:20pm (America/New_York)"


def _child_that(scripts, tmp_path: Path, log_dir: Path, body: str, adapter="claude"):
    script = tmp_path / "vendor.py"
    script.write_text(body, encoding="utf-8")
    launch = Launch(
        adapter=adapter,
        argv=[str(scripts["python"]), str(script)],
        seat_path=tmp_path / "mcp.json",
        config=SEAT.mcp_config(),
        instruction="",
    )
    return spawn(launch, tmp_path, "gb/w", log_dir)


def _no_roster():
    return {"agents": []}


# ---- 1. the message is found ------------------------------------------------------------

def test_a_limit_on_stdout_is_reported_as_the_account_not_the_adapter(
    scripts, tmp_path: Path, log_dir: Path
):
    """THE ONE THAT MATTERS, and the exact measured shape: exit 1, the message on stdout,
    stderr completely empty.

    Sabotage: make `_account_limit` read only `child.tail()` — the default stream is stderr,
    the text is not there, and this comes back as a plain `LaunchFailed` again."""
    child = _child_that(
        scripts, tmp_path, log_dir,
        f"import sys\nprint({SAID!r})\nsys.exit(1)\n",
    )

    with pytest.raises(VendorLimit) as exc:
        await_registration(child, _no_roster, window=0.5, poll=0.05, sleep=lambda _: None)

    said = str(exc.value)
    assert "out of quota" in said
    assert "resets 12:20pm" in said, "swallowed the one part that tells you what to do"
    assert exc.value.adapter == "claude"


def test_a_limit_on_stderr_is_found_too(scripts, tmp_path: Path, log_dir: Path):
    """Both streams, so a vendor that moves the message does not silently stop being
    recognised. An absent message is what made this look like a crash the first time."""
    child = _child_that(
        scripts, tmp_path, log_dir,
        f"import sys\nprint({SAID!r}, file=sys.stderr)\nsys.exit(1)\n",
    )

    with pytest.raises(VendorLimit):
        await_registration(child, _no_roster, window=0.5, poll=0.05, sleep=lambda _: None)


# ---- 2. a distinguishable type ----------------------------------------------------------

def test_it_is_still_a_launch_failure_so_every_existing_handler_keeps_working():
    """A subclass rather than a new hierarchy: this can only ever make an existing failure
    MORE specific, never route one somewhere that has no handler."""
    assert issubclass(VendorLimit, LaunchFailed)


# ---- 3. nothing guesses -----------------------------------------------------------------

def test_a_real_crash_is_still_reported_as_a_crash(scripts, tmp_path: Path, log_dir: Path):
    """THE CONTROL, and the reason the base class matches nothing. A matcher that fired on
    the word "limit" would relabel real crashes as billing problems — the same misattribution
    this fixes, pointing the other way."""
    child = _child_that(
        scripts, tmp_path, log_dir,
        "import sys\nprint('RecursionError: maximum recursion limit exceeded',"
        " file=sys.stderr)\nsys.exit(1)\n",
    )

    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _no_roster, window=0.5, poll=0.05, sleep=lambda _: None)

    assert not isinstance(exc.value, VendorLimit), "guessed from the word 'limit'"


def test_both_streams_are_printed_when_it_is_a_crash(scripts, tmp_path: Path, log_dir: Path):
    """The report that started this said `stderr tail:` followed by nothing, with no hint
    that another stream existed. A tail of the wrong stream is indistinguishable from a child
    that said nothing at all."""
    child = _child_that(
        scripts, tmp_path, log_dir,
        "import sys\nprint('a clue nobody could see')\nsys.exit(3)\n",
    )

    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _no_roster, window=0.5, poll=0.05, sleep=lambda _: None)

    said = str(exc.value)
    assert "stdout tail:" in said and "a clue nobody could see" in said


def test_an_adapter_this_package_does_not_know_is_not_a_limit(
    scripts, tmp_path: Path, log_dir: Path
):
    """Stand-in adapters exist (`make_launch_factory`, probes). A lookup that raised on one
    would crash the diagnostic path exactly when something is already failing."""
    child = _child_that(
        scripts, tmp_path, log_dir,
        f"import sys\nprint({SAID!r})\nsys.exit(1)\n", adapter="fake",
    )

    with pytest.raises(LaunchFailed) as exc:
        await_registration(child, _no_roster, window=0.5, poll=0.05, sleep=lambda _: None)

    assert not isinstance(exc.value, VendorLimit)


def test_every_adapter_answers_the_question_and_the_default_is_silence():
    """The base class returns "" so an adapter that has never been observed hitting a limit
    cannot invent one. Only `claude` has a measured string today, and that is stated rather
    than left for a reader to infer from four empty methods."""
    answers = {name: a.account_limit(SAID, "") for name, a in ADAPTERS.items()}

    assert answers["claude"] == SAID
    assert [n for n, v in answers.items() if v] == ["claude"]
