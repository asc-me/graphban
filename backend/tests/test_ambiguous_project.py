"""A guessed project says so (GRPH-482).

The dispatcher resolves a call's project as `requested or key.project_id or allowed[0]`. That
last clause is an ordering: a key spanning several projects, called without `project_id`,
lands in whichever sorts first — no error, no warning, and nothing in the response naming the
project it chose.

`require_readable` elsewhere in this codebase fails **closed** on a missing project. This one
fails **open**, to an ordering. Two answers to the same question in one service.

**Not refused, and that is the decision.** Refusing is the honest-looking option and it breaks
every existing multi-project caller, including agent prompts already in the wild that cannot
be edited from here. `wrong-project-write.test.tsx` exists because the frontend had this exact
class of bug, and the fix there was to make the project explicit at the call site rather than
to reject the call. So: the choice becomes visible, and the caller decides what to do about it.

The annotation appears **only** when the project was genuinely guessed. Everywhere else it is
noise, and noise on every response is how a field that matters gets skimmed past.
"""
from __future__ import annotations

import json

import pytest


def _call(client, key, tool, args=None):
    r = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args or {}}},
                    headers={"X-API-Key": key}).json()["result"]
    assert not r.get("isError"), r
    return json.loads(r["content"][0]["text"])


@pytest.fixture()
def two_projects(client, auth):
    """A second project, so the key below spans more than one."""
    return client.post("/api/projects", json={"name": "Second"}, headers=auth).json()["id"]


@pytest.fixture()
def spanning_key(client, auth, two_projects):
    """A key with no project of its own, readable across every project its owner holds."""
    return client.post("/api/api-keys", json={"name": "spanning"},
                       headers=auth).json()["plaintext"]


# ── the guess is reported ─────────────────────────────────────────────────────

def test_a_multi_project_key_that_names_none_is_told_what_was_chosen(client, spanning_key):
    out = _call(client, spanning_key, "get_backlog")
    assert "resolved_project" in out, (
        "the project was picked by sort order and the response did not say so")
    assert out["resolved_project"]
    assert "project_id" in out["resolved_project_note"]


def test_the_named_project_is_what_the_call_actually_used(client, spanning_key, two_projects):
    """The annotation has to be TRUE, not merely present. A note naming a project the call
    did not use would be worse than none — a caller would trust it."""
    out = _call(client, spanning_key, "get_backlog")
    named = _call(client, spanning_key, "get_backlog", {"project_id": out["resolved_project"]})
    assert named["total"] == out["total"]


# ── when it must stay quiet ───────────────────────────────────────────────────

def test_naming_the_project_silences_it(client, spanning_key, two_projects):
    """The caller already knows — that is the whole remedy the note asks for."""
    out = _call(client, spanning_key, "get_backlog", {"project_id": two_projects})
    assert "resolved_project" not in out


def test_a_key_scoped_to_one_project_is_not_guessing(client, auth):
    """A single-project key has nothing to choose between. Annotating it would put the note
    on the commonest call in the product, where it means nothing."""
    key = client.post("/api/api-keys", json={"name": "scoped", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    out = _call(client, key, "get_backlog")
    assert "resolved_project" not in out


def test_a_keys_own_default_is_a_choice_not_a_guess(client, auth, two_projects):
    """An operator picked that default when minting the key. Only the fall-through to
    `allowed[0]` is arbitrary, and conflating the two would cry wolf on a deliberate setup."""
    key = client.post("/api/api-keys", json={"name": "defaulted", "project_id": two_projects},
                      headers=auth).json()["plaintext"]
    out = _call(client, key, "get_backlog")
    assert "resolved_project" not in out


# ── what must not change ──────────────────────────────────────────────────────

def test_the_call_still_succeeds(client, spanning_key):
    """NOT refused. Breaking every multi-project caller — including prompts in the wild —
    is the option this deliberately did not take."""
    out = _call(client, spanning_key, "get_backlog")
    assert "results" in out


def test_an_out_of_scope_project_is_still_refused(client, auth, spanning_key):
    """Making the guess visible must not soften the boundary that was already enforced."""
    r = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_backlog", "arguments": {"project_id": "not-in-scope"}},
    }, headers={"X-API-Key": spanning_key}).json()["result"]
    assert r.get("isError"), r
    assert "scope" in r["content"][0]["text"].lower()


def test_a_non_dict_result_is_returned_unharmed(client, spanning_key):
    """The annotation needs somewhere a client would look. A tool returning a list has none,
    and wrapping it to make room would change that tool's shape for an unrelated reason."""
    from app.mcp_server import _attach_resolved_project

    guessed = {"guessed": True, "project_id": "core"}
    assert _attach_resolved_project([1, 2], guessed) == [1, 2]
    assert _attach_resolved_project("text", guessed) == "text"
    assert _attach_resolved_project({"a": 1}, guessed)["resolved_project"] == "core"
