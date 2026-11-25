"""A child that exits before registering says WHY, and the vendor's refusal is its own class (GRPH-1040).

Measured: a review-only seat on qwen-code hit DashScope `insufficient_quota` (429) and exited 0
before `register_agent`. The seat was never redeemed — the server consumes a seat only at
registration — so it is still good for a replacement child. What was missing is a log line
that says the vendor did it, distinct from a broken adapter (`never_registered`).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gbfleet.seat import Seat
from gbfleet.spawn import (
    Launch, LaunchFailed, VendorLimit, await_registration, spawn, unregistered_exit_reason,
)

SEAT = Seat(code="WORKER-J3J4SS", server_url="https://gb.invalid", api_key="gbk_test")


def _fake_adapter_child(scripts, tmp_path: Path, log_dir: Path, body: str):
    """A fake adapter: a script that prints and exits without ever registering."""
    script = tmp_path / "vendor.py"
    script.write_text(body, encoding="utf-8")
    launch = Launch(
        adapter="fake",
        argv=[str(scripts["python"]), str(script)],
        seat_path=tmp_path / "mcp.json",
        config=SEAT.mcp_config(),
        instruction="",
    )
    return spawn(launch, tmp_path, "gb/w", log_dir)


def _exit_record(caplog):
    rows = [r for r in caplog.records if r.message == "child_exit_unregistered"]
    assert len(rows) == 1, [r.message for r in caplog.records]
    return rows[0].gbfleet


@pytest.mark.parametrize("stream", ["sys.stdout", "sys.stderr"])
@pytest.mark.parametrize("said,reason", [
    ('{"error": {"code": "insufficient_quota", "message": "token plan exhausted"}}',
     "vendor_quota"),
    ('{"type": "error", "error": {"type": "rate_limit_error"}}', "vendor_quota"),
    ("Not logged in · Please run /login", "vendor_auth"),
    ("Traceback: RecursionError: maximum recursion limit exceeded", "never_registered"),
])
def test_an_exit_before_registering_is_logged_with_its_class(
    scripts, tmp_path: Path, log_dir: Path, caplog, said, reason, stream
):
    """Sabotage: return `never_registered` unconditionally from `unregistered_exit_reason` and
    every vendor row fails; match the bare word "limit" and the crash row reads as quota."""
    child = _fake_adapter_child(
        scripts, tmp_path, log_dir,
        # A vendor CLI writes UTF-8 (the `·` in Claude Code's login line); a Python child on
        # Windows would otherwise write cp1252 and the row would test the codec, not the class.
        f"import sys\n{stream}.reconfigure(encoding='utf-8')\n"
        f"print({said!r}, file={stream})\nsys.exit(0)\n")

    with caplog.at_level(logging.INFO, logger="gbfleet"):
        with pytest.raises(LaunchFailed) as exc:
            await_registration(child, lambda: {"agents": []}, window=5, poll=0.05,
                               sleep=lambda _: None)

    got = _exit_record(caplog)
    assert got["reason"] == reason
    assert got["exit_code"] == 0
    assert got["seat_redeemable"] is True
    if reason == "vendor_quota":
        assert isinstance(exc.value, VendorLimit), "quota is the account, not the adapter"
        assert said in str(exc.value)
    else:
        assert not isinstance(exc.value, VendorLimit)
    if reason != "never_registered":
        assert got["said"] == said


def test_the_classifier_never_guesses_from_the_word_limit():
    assert unregistered_exit_reason("open file limit reached", "rate limit") == (
        "never_registered", "")
    assert unregistered_exit_reason("", "Not logged in")[0] == "vendor_auth"
    # Quota outranks a login line printed beside it.
    assert unregistered_exit_reason("Not logged in", "insufficient_quota")[0] == "vendor_quota"
