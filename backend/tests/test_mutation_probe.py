"""The probe measures a sabotage rather than believing one (GRPH-566, PRD-26 §Mutation probe).

The gate's weakest link is that `tests_failed` is supplied by the agent it gates. This file
holds the probe honest in the one way that matters: **both directions or neither.** A mutation
known to break tests must be OBSERVED breaking them, and a no-op must be OBSERVED breaking
nothing — because a probe exercised only against a state where the mutation cannot matter
reports zero and reads as clean (GRPH-466).

Everything except those two runs against an injected runner, so the refusals are cheap. The
two that carry the acceptance criterion drive a REAL pytest against a REAL temporary
repository, because a probe proved only against a fake runner has never once measured
anything.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import probe_sabotage as ps  # noqa: E402


def runner_returning(*outputs: str):
    """A stand-in suite that returns each output in turn (baseline first, then mutated)."""
    seq = list(outputs)

    def run(command: str, cwd) -> str:
        return seq.pop(0) if seq else seq_last
    seq_last = outputs[-1] if outputs else ""
    return run


# ---- the two that ARE the acceptance criterion -----------------------------------------

@pytest.fixture()
def tiny_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A real repository with a real suite: one function, one test that pins it."""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n",
        encoding="utf-8")
    return tmp_path


def _pytest_cmd() -> str:
    return f"{sys.executable} -m pytest -q -p no:cacheprovider"


def test_a_mutation_that_breaks_the_suite_is_observed_breaking_it(tiny_repo):
    """Direction one. Without this the probe could refuse everything and look strict."""
    obs = ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
                   command=_pytest_cmd(), cwd=tiny_repo)

    assert obs.observed_failed >= 1, "a real break was not observed breaking anything"
    assert obs.effective is True
    assert obs.landed == 1
    assert obs.baseline_failed == 0, "the baseline must be green or the count means nothing"


def test_a_mutation_that_breaks_nothing_is_observed_breaking_nothing(tiny_repo):
    """Direction two, and the one the PRD names.

    A probe that reported every mutation as effective would satisfy the test above and be
    worth nothing. This changes a comment: it lands, it is not a no-op string-wise, and it
    cannot affect a single test.
    """
    obs = ps.probe(tiny_repo, file="calc.py", old="def add(a, b):",
                   new="def add(a, b):  # touched, and harmless",
                   command=_pytest_cmd(), cwd=tiny_repo)

    assert obs.observed_failed == 0
    assert obs.effective is False, "a harmless edit was reported as an effective sabotage"


def test_the_receipt_records_an_ineffective_mutation_rather_than_omitting_it(tiny_repo):
    """`passed: false`, on the record. An omitted predicate leaves the item looking
    un-probed, which is indistinguishable from the self-report this replaces."""
    obs = ps.probe(tiny_repo, file="calc.py", old="def add(a, b):",
                   new="def add(a, b):  # touched", command=_pytest_cmd(), cwd=tiny_repo)
    receipt = ps.attestation(obs, commit="a" * 40)

    assert receipt["predicates"][0]["name"] == "sabotage_observed"
    assert receipt["predicates"][0]["passed"] is False
    assert "broke NOTHING" in receipt["predicates"][0]["detail"]


def test_the_tree_is_restored_after_a_probe(tiny_repo):
    """A mutation left behind poisons every later run, and presents as somebody else's
    regression rather than as this probe's residue."""
    before = (tiny_repo / "calc.py").read_text(encoding="utf-8")
    ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
             command=_pytest_cmd(), cwd=tiny_repo)

    assert (tiny_repo / "calc.py").read_text(encoding="utf-8") == before


def test_a_still_failing_suite_after_restore_is_refused(tiny_repo):
    """The restore is verified by RE-READING the file, not by assuming the write worked."""
    obs = ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
                   command=_pytest_cmd(), cwd=tiny_repo)
    assert obs.restored_clean is True


# ---- refusals: every route to a zero that is not a measurement ---------------------------

def test_a_mutation_that_does_not_land_is_refused_not_counted_as_zero(tiny_repo):
    """THE defect this whole probe exists to remove.

    A mutation that never applied leaves the suite measuring the unmutated tree and printing
    a clean pass — which reads exactly like a surviving mutation, and is the shape that gets
    written up as a finding.
    """
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.probe(tiny_repo, file="calc.py", old="return a * b", new="return a - b",
                 command=_pytest_cmd(), cwd=tiny_repo)

    assert "land 0 times" in str(exc.value)
    assert "UNMUTATED" in str(exc.value)


def test_a_mutation_landing_more_than_once_is_refused():
    text = "x = 1\ny = 1\n"
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.check_mutation(text, "= 1", "= 2")
    assert "land 2 times" in str(exc.value)


def test_a_no_op_mutation_is_refused():
    """`old == new` passes while changing nothing — a green run that proves nothing."""
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.check_mutation("anything", "same", "same")
    assert "no-op" in str(exc.value)


def test_a_red_baseline_is_refused(tiny_repo):
    """A failure count taken against an already-failing suite cannot be attributed to the
    mutation, so it is not a measurement."""
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
                 command=_pytest_cmd(), cwd=tiny_repo,
                 runner=runner_returning("1 failed, 0 passed in 0.1s"))
    assert "baseline is already red" in str(exc.value)


def test_a_run_where_no_tests_ran_is_refused_rather_than_read_as_zero():
    """`no tests ran` is not a pass. Treating it as zero failures is how a mutation gets
    recorded as ineffective when the truth is that nothing looked at it."""
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.observed_failures("no tests ran in 0.00s")
    assert "nothing was measured" in str(exc.value)


def test_errors_without_failures_are_refused_rather_than_counted():
    """A broken fixture would otherwise masquerade as an effective sabotage."""
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.observed_failures("1 warning, 11 errors in 0.8s")
    assert "error" in str(exc.value)


def test_a_real_failure_count_is_read():
    assert ps.observed_failures("1 failed, 41 passed, 1 warning in 2.1s") == 1
    assert ps.observed_failures("42 passed in 2.1s") == 0


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.probe(tmp_path, file="nope.py", old="a", new="b",
                 command="true", cwd=tmp_path)
    assert "does not exist" in str(exc.value)


def test_a_repository_that_declares_no_test_command_is_refused(tmp_path):
    """Guessing a verification command is the same mistake as guessing a vendor flag."""
    with pytest.raises(ps.ProbeRefused) as exc:
        ps.declared_command(tmp_path)
    assert ps.CONFIG_NAME in str(exc.value)


def test_this_repository_declares_one_the_probe_can_read():
    """The probe reads the repo's OWN declaration, so it measures the suite the repo means."""
    root = pathlib.Path(__file__).resolve().parents[2]
    command, cwd = ps.declared_command(root)
    assert "pytest" in command
    assert cwd.name == "backend"


# ---- the attestation itself ---------------------------------------------------------------

def test_the_attestation_names_the_adapter_and_the_commit(tiny_repo):
    obs = ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
                   command=_pytest_cmd(), cwd=tiny_repo)
    receipt = ps.attestation(obs, commit="b" * 40, run_ref="http://example/run")

    assert receipt["kind"] == "attestation"
    assert receipt["adapter"] == "mutation-probe"
    assert receipt["commit"] == "b" * 40
    assert receipt["predicates"][0]["passed"] is True


def test_the_attestation_satisfies_the_servers_own_attestation_contract(tiny_repo):
    """It must survive `normalize_evidence` as an attestation rather than being demoted to a
    note — otherwise the probe writes prose and the gate reads nothing."""
    from app.services import items as items_svc

    obs = ps.probe(tiny_repo, file="calc.py", old="return a + b", new="return a - b",
                   command=_pytest_cmd(), cwd=tiny_repo)
    stored = items_svc.normalize_evidence([ps.attestation(obs, commit="c" * 40)])

    assert stored and stored[0]["kind"] == "attestation", stored
    assert items_svc.attestation_receipts(stored), "the server does not recognise the receipt"


def test_the_cli_refuses_with_a_distinct_exit_code(tiny_repo, capsys):
    """Exit 2 is `could not measure`; exit 0 with a `passed: false` receipt is `measured
    nothing`. An operator has to be able to tell those apart."""
    code = ps.main(["--item", "GRPH-1", "--commit", "d" * 40, "--root", str(tiny_repo),
                    "--file", "calc.py", "--old", "return a * b", "--new", "return a - b",
                    "--tests", _pytest_cmd()])
    assert code == 2
    assert "probe refused" in capsys.readouterr().err


def test_the_probe_posts_the_attestation_the_gate_would_read(monkeypatch, tiny_repo):
    """THE CALL. Measurement refusals are pinned; dropping post() so the probe never
    writes — even with GRAPHBAN_URL and GRAPHBAN_GATE_KEY set — left 18 passed
    (GRPH-566 bounce). The gate still reads the agent's tests_failed.
    """
    posted: list[dict] = []

    def capture(url, key, item, receipt, **kw):
        posted.append(receipt)

    obs = ps.Observation(
        file="calc.py", old="return a + b", new="return a - b",
        landed=1, baseline_failed=0, observed_failed=1, restored_clean=True,
    )
    monkeypatch.setenv("GRAPHBAN_URL", "http://example.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "gb_sk_x")
    monkeypatch.setattr(ps, "probe", lambda *a, **k: obs)
    monkeypatch.setattr(ps, "post", capture)

    code = ps.main(["--item", "GRPH-1", "--commit", "d" * 40, "--root", str(tiny_repo),
                    "--file", "calc.py", "--old", "return a + b", "--new", "return a - b",
                    "--tests", _pytest_cmd()])

    assert code == 0
    assert posted, "the probe measured but never posted — the gate still reads a self-report"
    names = [p["name"] for p in posted[0].get("predicates") or []]
    assert "sabotage_observed" in names, posted[0]
