"""A tier that cannot spawn does not report PASS (GRPH-805).

Reported from super-arc: the cheap tier resolved to `gbagent:qwen3.6:…`, was marked verified,
and doctor printed `[PASS]`. Spawning exited 78 — no model endpoint. **The one check that
looked green was the broken one**, which is worse than no check at all, because it is what an
operator consults instead of trying it.

The mechanism is a distinction that was right in one place and swallowed in another.
`known_models` returns `None` for "cannot be asked", and its docstring correctly argues that
refusing every model because we could not look would break a working setup. But "there is no
endpoint to ask" is not "we could not ask" — it is a configuration the adapter can read, and
one it will certainly refuse on. `installed_checker` treated the two the same and passed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import matrix as matrix_mod
from gbfleet.adapters import ADAPTERS

BIN = Path("/usr/bin/does-not-matter")


@pytest.fixture()
def no_endpoint(monkeypatch):
    monkeypatch.delenv("GBAGENT_BASE_URL", raising=False)


@pytest.fixture()
def endpoint(monkeypatch):
    monkeypatch.setenv("GBAGENT_BASE_URL", "http://localhost:11434")


# ---- the precondition itself -----------------------------------------------------------------

def test_gbagent_says_a_spawn_would_fail_when_there_is_no_endpoint(no_endpoint):
    said = ADAPTERS["gbagent"].spawn_blocked(BIN)

    assert said, "reported nothing for a configuration that exits 78"
    assert "GBAGENT_BASE_URL" in said, "did not name the variable to set"
    assert "78" in said, "did not name the exit code an operator will actually see"


def test_it_says_nothing_when_an_endpoint_is_configured(endpoint):
    assert ADAPTERS["gbagent"].spawn_blocked(BIN) == ""


def test_it_asks_no_network(no_endpoint, monkeypatch):
    """Configuration only. A precondition that needed the network could not tell "unset" from
    "down" — which is the whole distinction it exists to make."""
    import socket

    monkeypatch.setattr(socket, "socket",
                        lambda *a, **k: pytest.fail("spawn_blocked opened a socket"))

    assert ADAPTERS["gbagent"].spawn_blocked(BIN)


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_answers_the_question(name, endpoint):
    """The default is "nothing says a spawn will fail", and every adapter must be able to say
    so without raising — a precondition check that itself explodes is not a check."""
    assert isinstance(ADAPTERS[name].spawn_blocked(BIN), str)


# ---- and what the matrix does with it ----------------------------------------------------------

class _Resolved:
    def __init__(self, adapter):
        self.adapter, self.binary = adapter, BIN


def _checked(monkeypatch, harness="gbagent"):
    """`installed_checker` with adapter resolution stubbed, so the assertion is about the
    precondition and not about which binaries this machine happens to have."""
    monkeypatch.setattr(matrix_mod.adapters_mod, "resolve",
                        lambda h, binary=None: _Resolved(ADAPTERS[h]))
    row = next(r for r in matrix_mod.load().rows if r.harness == harness)
    return matrix_mod.installed_checker()(row)


def test_the_matrix_marks_the_row_unusable_when_a_spawn_would_fail(no_endpoint, monkeypatch):
    """THE REGRESSION. This returned (True, "") and doctor printed PASS."""
    ok, why = _checked(monkeypatch)

    assert ok is False
    assert "GBAGENT_BASE_URL" in why


def test_the_row_is_usable_again_once_the_endpoint_is_set(endpoint, monkeypatch):
    """The control. A check that failed the row unconditionally would satisfy the test above
    while telling an operator their working setup is broken."""
    ok, why = _checked(monkeypatch)

    assert ok is True, why


def test_the_precondition_is_asked_before_the_model_listing(no_endpoint, monkeypatch):
    """Order matters: `known_models` returning None is exactly what used to swallow this, so
    asking it first would let the same collapse happen again."""
    asked = []
    monkeypatch.setattr(matrix_mod.adapters_mod, "resolve",
                        lambda h, binary=None: _Resolved(ADAPTERS[h]))
    monkeypatch.setattr(type(ADAPTERS["gbagent"]), "known_models",
                        lambda self, b: asked.append("models") or None)

    _checked(monkeypatch)

    assert asked == [], "asked what models are served by an adapter that cannot spawn"
