"""The switch that decides whether the schema ratchet demands anything (GRPH-559).

`conftest._is_full_run` chooses the branch. On a full run the ratchet requires every declared
MCP tool to have been exercised and refuses an empty aggregate; on a selection it forgives
both. Everything downstream hangs on that one boolean — and nothing exercised it.

`test_mcp_output_schema.py` calls `schema_probe.report(full_run=True)` and `(full_run=False)`
DIRECTLY, so both branches are well tested while the code that PICKS one never runs. Measured
on the ticket: pin the classifier to always report a selection —

    if config.option.keyword or config.option.markexpr:   ->   if True:
        return False                                               return False

— and all twelve tests in that file still pass. The ratchet then demands nothing on any run,
ever, and CI stays green.

**A ratchet that has been switched off is indistinguishable from one that is satisfied**, and
the failure is silent by construction: nobody notices a demand that stopped being made. It is
the same shape as the defect GRPH-465 fixed, in the opposite direction — there the ratchet
cried on every subset run, which people learn to switch off; here it would stop crying at all.

**Real `Config` objects, not mocks.** The whole value of this function is that it matches what
pytest actually does with `testpaths`, `-k` and a bare path argument. A mock asserts my model
of pytest, which is the thing most likely to be wrong: `pytest` with no arguments does not see
empty `args`, it sees `['tests']` substituted from the ini, and a classifier written against
the intuition would return False on every full run — the silent-off failure, shipped.
"""
from __future__ import annotations

import sys

import pytest
from _pytest.config import get_config

#: The conftest ALREADY LOADED by this session, by name rather than re-imported.
#:
#: `import conftest` happens to be safe here because Python returns the cached module — but
#: importing it under any OTHER name re-executes it, and conftest rewrites `DATABASE_URL` at
#: import time. That produced `graphban_test_gw0_gw0` and eighteen stray databases before
#: anyone noticed (see tests/dbnames.py, which exists for that reason). Reaching into
#: `sys.modules` says out loud that no second execution is wanted.
conftest = sys.modules["conftest"]


def _config(args: list[str]):
    """A real parsed pytest Config for `args`, as an invocation would produce."""
    config = get_config(args)
    config.parse(args)
    return config


# ── the load-bearing direction: it must be able to say True ───────────────────

@pytest.mark.parametrize("args", [
    pytest.param([], id="no-arguments"),
    pytest.param(["tests"], id="args-equal-testpaths"),
    pytest.param(["."], id="dot"),
])
def test_a_whole_suite_run_is_recognised(args):
    """THE case that matters. A classifier that never returns True switches the ratchet off
    everywhere, and that is the direction which reads as clean — no failure, no message,
    nothing to notice.

    `no-arguments` is the one a mock would get wrong: pytest substitutes `testpaths` from the
    ini, so `config.args` is `['tests']` and not `[]`.
    """
    assert conftest._is_full_run(_config(args)) is True, (
        f"pytest {' '.join(args) or '(no args)'} was classified as a SELECTION — the schema "
        "ratchet demands nothing on a run like this, and says nothing about it")


# ── the other direction: a selection must not be asked for everything ─────────

@pytest.mark.parametrize("args, why", [
    pytest.param(["-k", "something"], "a keyword filter", id="dash-k"),
    pytest.param(["-m", "something"], "a marker filter", id="dash-m"),
    pytest.param(["tests/test_code_graph.py"], "one file", id="single-file"),
    pytest.param(["tests/test_code_graph.py", "tests/test_galaxy.py"], "two files", id="two-files"),
])
def test_a_selection_is_not_asked_to_have_exercised_everything(args, why):
    """The complement, and the reason the branch exists at all. Failing one file's run because
    the other fifty tools did not happen makes the ratchet something people learn to ignore,
    which is worse than not having it — that is GRPH-465, already paid for once."""
    assert conftest._is_full_run(_config(args)) is False, (
        f"{why} was classified as a full run — this is the ratchet crying on a subset, which "
        "is how it gets switched off")


def test_the_classifier_actually_distinguishes():
    """Belt and braces against the two constant implementations. Both parametrised tests above
    could be satisfied by nothing at all if collection silently produced no cases, and a
    constant `True` or constant `False` is exactly what this function must never become."""
    verdicts = {conftest._is_full_run(_config(a)) for a in ([], ["-k", "x"])}
    assert verdicts == {True, False}, (
        f"the classifier returned {verdicts} for a full run and a filtered run — it is a "
        "constant wearing a decision's name")


# ── the xdist half, which takes a different path entirely ─────────────────────

def test_an_xdist_worker_records_but_does_not_judge(monkeypatch):
    """CI runs `-n auto`, so this is the path CI actually takes.

    A worker sees one shard of the suite. If it judged, it would hold its shard against the
    full-tool demand and fail for tools another worker exercised — the GRPH-465 failure again,
    multiplied by worker count. So a worker dumps its evidence and returns; the controller,
    which has no `workerinput`, is the one that reports.
    """
    from tests import schema_probe

    calls = {"dump": 0, "report": 0}
    monkeypatch.setattr(schema_probe, "dump", lambda *a, **k: calls.__setitem__("dump", calls["dump"] + 1))
    monkeypatch.setattr(schema_probe, "report", lambda *a, **k: calls.__setitem__("report", calls["report"] + 1) or [])

    class _Config:
        workerinput = {"workerid": "gw0"}
        option = type("O", (), {"keyword": "", "markexpr": ""})()
        args = ["tests"]

    class _Session:
        config = _Config()
        exitstatus = 0

    conftest.pytest_sessionfinish(_Session(), 0)

    assert calls["dump"] == 1, "a worker must record its evidence or the controller judges a hole"
    assert calls["report"] == 0, (
        "a worker judged the ratchet against its own shard — it would demand tools that ran "
        "on a different worker")
