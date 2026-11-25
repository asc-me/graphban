import pytest

from gbfleet.bench import compare as c


def _run(rid, version="1", pass_a="check_green"):
    return {"id": "bnb_" + rid, "run_id": rid, "vendor": "anthropic", "model": "sonnet",
            "binary_version": "1.0", "suite_id": "template", "suite_version": version,
            "outcomes": [{"task_id": "t1", "outcome": pass_a}, {"task_id": "t2", "outcome": "check_red"}],
            "rollups": {"B7": {"n": 2, "green": 1 if pass_a == "check_green" else 0}}}


def test_compare_refuses_across_suite_versions_naming_both():
    with pytest.raises(c.CompareRefused) as e:
        c.compare(_run("a", "1"), _run("b", "2"))
    assert "v1" in str(e.value) and "v2" in str(e.value)


def test_compare_shows_per_task_and_per_capability():
    out = "\n".join(c.compare(_run("a"), _run("b", pass_a="check_red")))
    assert "t1: check_green | check_red  *" in out
    assert "t2: check_red | check_red" in out
    assert "B7: 1/2 | 0/2" in out


def test_find_run_by_run_id_or_missing():
    rs = [_run("a")]
    assert c.find_run(rs, "a")["run_id"] == "a"
    with pytest.raises(c.CompareRefused):
        c.find_run(rs, "zzz")
