"""Tool annotations: say only what differs from the spec default, and say it once (GRPH-48).

Two separate things landed here. The manifest had two tokens of headroom under its ceiling,
and ~388 of it was hints repeating values the MCP spec already defines as the default —
absent IS the default, so those bytes told every client something it already knew, 54 times.

The second is the bug that trim uncovered: seven tools carried a hand-written `annotations`
block in the TOOLS literal that the build loop then overwrote. Six agreed and were merely
noise. `review_recommendation` did not — it declared `idempotentHint: true` and shipped
`false`. The manifest advertised the opposite of what the author wrote, and nothing
anywhere said so.
"""
import json
import re
from pathlib import Path

import pytest

from app.mcp_server import _ANNOTATION_DEFAULTS, _READ_ONLY, TOOLS

from tests.annotations import effective

SRC = Path(__file__).resolve().parents[1] / "app" / "mcp_server.py"


# ── the trim ──────────────────────────────────────────────────────────────────

def test_no_tool_ships_a_hint_that_equals_the_spec_default():
    """The trim itself. A default-valued hint is bytes that change nothing: a client that
    reads it and a client that does not both end up at the same value."""
    offenders = {t["name"]: {k: v for k, v in t["annotations"].items()
                             if _ANNOTATION_DEFAULTS[k] == v}
                 for t in TOOLS}
    offenders = {n: d for n, d in offenders.items() if d}
    assert not offenders, f"redundant hints: {offenders}"


def test_the_trim_changes_no_tool_s_actual_claim():
    """Lossless, checked against the claim rather than the bytes. Every tool must still
    resolve to exactly the four hints the build loop computed for it."""
    for t in TOOLS:
        ro = t["name"] in _READ_ONLY
        assert effective(t)["readOnlyHint"] is ro, t["name"]
        assert set(effective(t)) == set(_ANNOTATION_DEFAULTS), t["name"]


def test_a_hint_that_differs_from_the_default_is_still_sent():
    """The complement, so the trim cannot degenerate into dropping everything — which
    would pass the first test perfectly while telling clients nothing at all."""
    assert TOOLS, "no tools"
    sent = [t for t in TOOLS if t["annotations"]]
    assert len(sent) == len(TOOLS), "some tool went completely silent"
    # `openWorldHint: false` differs from the default on every tool here: nothing in this
    # server reaches an open world. If that stops being emitted, clients start assuming it.
    assert all(t["annotations"].get("openWorldHint") is False for t in TOOLS)


def test_the_defaults_table_matches_the_spec():
    """The one thing nothing else can catch. Every test above is derived FROM this table,
    so a wrong entry is invisible to all of them — and it is not ours to choose: these are
    the MCP `ToolAnnotations` defaults, unchanged from 2025-03-26 through 2026-07-28.

    The failure it prevents is specific and bad. Record `destructiveHint` as defaulting to
    False and the trim starts dropping `destructiveHint: false` from all 19 read-only
    tools — where a client, applying the REAL default, would read every one of them as
    destructive. A saving of a few hundred bytes would have inverted the safety hint on
    every read tool in the server.
    """
    assert _ANNOTATION_DEFAULTS == {
        "readOnlyHint": False,     # "If true, the tool does not modify its environment."
        "destructiveHint": True,   # "If true, the tool may perform destructive updates."
        "idempotentHint": False,
        "openWorldHint": True,     # "may interact with an open world of external entities"
    }


def test_no_read_tool_can_be_read_as_destructive():
    """The consequence, asserted independently of the table above so the two cannot be
    wrong together. Resolved through the defaults exactly as a client resolves them."""
    for t in TOOLS:
        if t["name"] in _READ_ONLY:
            assert effective(t)["destructiveHint"] is False, t["name"]
    destructive = [t["name"] for t in TOOLS if effective(t)["destructiveHint"]]
    assert destructive == ["update_item"], destructive


# ── the bug the trim uncovered ────────────────────────────────────────────────

def test_annotations_are_declared_in_exactly_one_place():
    """THE guard. A hand-written `annotations` block inside the TOOLS literal is silently
    overwritten by the build loop below it, so it looks authoritative in review, reads as
    the tool's declared safety contract, and has no effect whatsoever. Seven existed; one
    of them was shipping the opposite of what it said."""
    src = SRC.read_text()
    literal = src[: src.index("for _t in TOOLS:")]
    stray = re.findall(r'"annotations":', literal)
    assert not stray, (f"{len(stray)} hand-written annotation block(s) in the TOOLS literal — "
                       "the build loop overwrites them, so they are silently ignored")


def test_review_recommendation_advertises_that_it_is_idempotent():
    """The tool it was wrong for. It sets a status and commits, so a second identical call
    changes nothing further — approving an already-approved recommendation is a no-op. An
    agent told otherwise has to treat a retry as unsafe."""
    t = next(t for t in TOOLS if t["name"] == "review_recommendation")
    assert effective(t)["idempotentHint"] is True
    assert effective(t)["readOnlyHint"] is False, "it does write — just not repeatedly"


@pytest.mark.parametrize("name", ["update_item", "create_item", "search_items", "sign_off"])
def test_the_hints_survive_the_round_trip_to_a_client(client, auth, name):
    """Through the real endpoint, not just the module constant: the trim happens where the
    manifest is built, so a test reading TOOLS could pass while the wire format differed."""
    key = client.post("/api/api-keys", json={"name": "ann-rt"}, headers=auth).json()["plaintext"]
    tl = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"X-API-Key": key}).json()
    by = {t["name"]: t for t in tl["result"]["tools"]}
    wire, built = by[name], next(t for t in TOOLS if t["name"] == name)
    assert wire["annotations"] == built["annotations"]
    assert not any(_ANNOTATION_DEFAULTS[k] == v for k, v in wire["annotations"].items())


def test_the_trim_is_worth_what_it_claims():
    """Pins the win. ~388 tokens against a ceiling that had 2 to spare — if a future change
    makes this saving evaporate, the ceiling is the thing that will start failing, and this
    says why."""
    lean = len(json.dumps({"tools": TOOLS}))
    fat = lean + sum(
        len(json.dumps({**_ANNOTATION_DEFAULTS, **t["annotations"]})) - len(json.dumps(t["annotations"]))
        for t in TOOLS)
    assert (fat - lean) // 4 > 300, f"trim now saves only ~{(fat - lean) // 4} tokens"
