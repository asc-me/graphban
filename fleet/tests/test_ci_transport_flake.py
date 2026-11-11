"""GRPH-1501: a CI check killed by a transport error is rerun once, never bounced.

Sabotage: make `transport_flake_jobs` return [] and `test_transport_error_log_is_rerun_not_bounced`
fails; drop the `review_held` guard in `_return_or_block_ci_red` and the held-claim test fails;
drop the `_signed_off_commit` check in `_merge_behind` and the signed-off test fails.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gbfleet import propose, supervisor as sup, until
from gbfleet.until import _ci_attested

ROW = {"id": "GRPH-1", "branch": "gb/x"}
HEAD = "abcdef1234567890"
FLAKE = "ConnectionResetError: [Errno 104] Connection reset by peer\n"
REAL = "FAILED tests/x.py::t1 - AssertionError\n===== 1 failed, 3 passed in 1s =====\n"


def _cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _forge(monkeypatch, log):
    rollup = {"statusCheckRollup": [
        {"name": "Security scans", "conclusion": "FAILURE", "detailsUrl": "https://g/runs/9/job/77"}]}
    calls = []

    def fake(repo, *args, **kw):
        calls.append(args)
        return _cp(json.dumps(rollup)) if args[0] == "pr" else _cp(log)

    monkeypatch.setattr(propose, "_gh", fake)
    monkeypatch.setattr(until, "ci_verdict", lambda *a: ("red", HEAD, "Security scans"))
    monkeypatch.setattr(until.observe, "emit", lambda *a, **k: None)
    return calls


class _Planner:
    def __init__(self):
        self.calls = []

    def call(self, tool, /, **args):
        self.calls.append((tool, args))
        return {}


def test_transport_error_log_is_rerun_not_bounced(monkeypatch):
    calls = _forge(monkeypatch, FLAKE)
    planner, logged = _Planner(), set()
    got = _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="P")
    assert got == ([], [ROW], [])
    assert not [c for c in planner.calls if c[0] == "bounce"]
    assert ("run", "rerun", "--job", "77") in calls


def test_a_second_red_at_the_same_head_bounces(monkeypatch):
    _forge(monkeypatch, FLAKE)
    planner, logged = _Planner(), set()
    _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="P")
    got = _ci_attested([ROW], Path("."), logged, planner=planner, agent_id="P")
    assert got == ([], [], [ROW])
    assert [c for c in planner.calls if c[0] == "bounce"]


def test_a_real_test_failure_still_bounces(monkeypatch):
    calls = _forge(monkeypatch, REAL)
    planner = _Planner()
    got = _ci_attested([ROW], Path("."), set(), planner=planner, agent_id="P")
    assert got == ([], [], [ROW])
    assert [c for c in planner.calls if c[0] == "bounce"]
    assert not any(c[:2] == ("run", "rerun") for c in calls)


def test_a_held_review_claim_blocks_the_rework(monkeypatch):
    _forge(monkeypatch, REAL)
    planner = _Planner()
    held = {**ROW, "review_claimed_by": "GRPH-A1"}
    assert until._return_or_block_ci_red(planner, held, HEAD, "job", "P") is False
    assert planner.calls == []


def test_a_signed_off_item_does_not_take_the_base_merge(monkeypatch, tmp_path):
    wave = sup.Wave()
    wave.behind_merge["gb/x"] = (2, "main", ["a.py"])

    class C:
        def call(self, tool, /, **a):
            return {"evidence": [{"kind": "attestation", "adapter": "fleet.sign_off",
                                  "commit": "219565d5aaaa"}]}

    monkeypatch.setattr(sup.wt_mod, "merge_ref_into",
                        lambda *a: (_ for _ in ()).throw(AssertionError("merged after sign-off")))
    sup._merge_behind(wave, tmp_path, "gb/x", "main", ["GRPH-1"], client=C())
    assert "signed off at 219565d5aaaa"[:20] in wave.behind_clean["gb/x"]
