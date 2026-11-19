"""A read timeout posting scans_clean must not kill the Security scans job (GRPH-1731).

urllib raises TimeoutError and ConnectionResetError from getresponse, outside the handler
that wraps OSError as URLError. The job used to die with a traceback after the scans had
already passed, and the red job was read as a scan finding.
"""
from __future__ import annotations

import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import attest_ci  # noqa: E402
import scan_ci  # noqa: E402

from tests.test_scan_ci import _pr, fakes, repo  # noqa: E402  pytest fixtures

_TRANSIENT = {
    TimeoutError: "timed out",
    ConnectionResetError: "Connection reset by peer",
}


def _ledger(monkeypatch, exc_type, *, times, on="update_item"):
    """`call_tool` raises `exc_type` the first `times` calls whose tool name is `on`."""
    calls: list[tuple[str, dict]] = []
    sleeps: list[float] = []
    seen = {"n": 0}

    def call_tool(url, key, name, arguments, *, timeout=15.0):
        calls.append((name, arguments))
        # A neighbour in this process also sleeps. Postgres does, in 0.001s steps,
        # while the attestation write is in flight. That wait is not the retry.
        time.sleep(0.001)
        if name == on:
            seen["n"] += 1
            if seen["n"] <= times:
                raise exc_type(_TRANSIENT[exc_type])

    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(attest_ci, "call_tool", call_tool)
    # Record the retry's own wait. Patching scan_ci.time.sleep replaces time.sleep
    # for the whole process, so a Postgres run's sleeps land in this list.
    monkeypatch.setattr(scan_ci, "_retry_backoff", sleeps.append)
    return calls, sleeps


def _updates(calls):
    return [a for name, a in calls if name == "update_item"]


def _run(repo, head):
    return scan_ci.main(["--repo", str(repo), "--base", repo.base, "--head", head,
                         "--branch", "gb/x", "--text", "Attests: GRPH-1110"])


def test_a_read_timeout_is_retried_and_the_item_is_written_once(repo, fakes, monkeypatch,
                                                                capsys):
    """One TimeoutError, then the write lands. Drop TimeoutError from ATTEST_TRANSIENT and
    this raises instead of exiting 0 — the except no longer names the exception urllib raises
    on a read."""
    calls, sleeps = _ledger(monkeypatch, TimeoutError, times=1)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 0
    out = capsys.readouterr()
    written = _updates(calls)
    assert len(written) == 2  # the timed-out attempt, then the one write that landed
    assert written[0] == written[1]
    assert written[0]["id"] == "GRPH-1110"
    assert written[0]["evidence"][0]["predicates"][0]["passed"] is True
    assert out.out.count("GRPH-1110: scans_clean passed") == 1
    assert "attestation failed" not in out.out
    assert "Traceback" not in out.err
    assert sleeps == [attest_ci.LEDGER_BACKOFF]


def test_a_connection_reset_is_retried_the_same_way(repo, fakes, monkeypatch, capsys):
    calls, sleeps = _ledger(monkeypatch, ConnectionResetError, times=1)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 0
    out = capsys.readouterr()
    assert len(_updates(calls)) == 2
    assert out.out.count("GRPH-1110: scans_clean passed") == 1
    assert "Traceback" not in out.err
    assert sleeps == [attest_ci.LEDGER_BACKOFF]


def test_a_read_timeout_that_persists_names_the_box_and_is_not_a_scan_finding(
        repo, fakes, monkeypatch, capsys):
    """Scans passed. The write still failed after 3 attempts, and the annotation says so."""
    calls, sleeps = _ledger(monkeypatch, TimeoutError, times=attest_ci.LEDGER_ATTEMPTS)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 1
    out = capsys.readouterr()
    assert len(_updates(calls)) == attest_ci.LEDGER_ATTEMPTS
    assert sleeps == [attest_ci.LEDGER_BACKOFF, attest_ci.LEDGER_BACKOFF * 2]
    assert "scans_clean passed" in out.out
    assert "::error title=scans_clean attestation failed::" in out.out
    assert "scans were clean" in out.out
    assert "attestation write to the box timed out" in out.out
    assert "This is not a scan finding." in out.out
    assert "failed check" not in out.out
    assert "Traceback" not in out.err
    assert out.out.count("::error title=scans_clean failed::") == 0


def test_a_connection_reset_that_persists_names_the_reset(repo, fakes, monkeypatch, capsys):
    calls, _ = _ledger(monkeypatch, ConnectionResetError, times=attest_ci.LEDGER_ATTEMPTS)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 1
    out = capsys.readouterr().out
    assert len(_updates(calls)) == attest_ci.LEDGER_ATTEMPTS
    assert "scans were clean" in out
    assert "hit a connection reset" in out
    assert "This is not a scan finding." in out
    assert "::error title=scans_clean failed::" not in out


def test_a_read_timeout_posting_a_scan_row_warns_and_still_attests(repo, fakes, monkeypatch,
                                                                   capsys):
    """The row post is before the attestation. A timeout there used to traceback first."""
    calls, sleeps = _ledger(monkeypatch, TimeoutError, times=1, on="record_scan")
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 0
    out = capsys.readouterr()
    assert "scan row not recorded" in out.out
    assert "Traceback" not in out.err
    assert len(_updates(calls)) == 1
    assert sleeps == []  # a row post is not retried; the attestation is what retries


def test_a_refused_write_is_still_not_retried(repo, fakes, monkeypatch):
    """A RuntimeError is the ledger refusing the payload. Backoff would hide a bad key."""
    calls: list[tuple[str, dict]] = []
    sleeps: list[float] = []

    def call_tool(url, key, name, arguments, *, timeout=15.0):
        calls.append((name, arguments))
        time.sleep(0.001)  # a neighbour's sleep, same as _ledger; not the retry
        if name == "update_item":
            raise RuntimeError(f"{arguments['id']}: graphban refused the write")

    monkeypatch.setenv("GRAPHBAN_URL", "https://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(attest_ci, "call_tool", call_tool)
    monkeypatch.setattr(scan_ci, "_retry_backoff", sleeps.append)
    head = _pr(repo, {"backend/app/x.py": "x = 2\n"})
    assert _run(repo, head) == 1
    assert len(_updates(calls)) == 1
    assert sleeps == []
