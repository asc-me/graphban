"""The prototype handoff closes `grill → prototype → grill` without a human carrying
state between tools (GRPH-235).

The tests worth having are about the two edges a handoff can silently lose:
- the VERDICT must be IN the grill where the grader can reach it — a screenshot stored
  anywhere else is an artifact nobody interrogates, which is the AL-68 nudge with extra
  steps;
- the FLIP must not happen by itself — a wrong automatic `high → low` deletes the
  "needs a prototype" signal this whole arc exists to surface, and nothing downstream
  would say so.

Also pinned: these server-authored turns must interleave with the client-replay append
rule (GRPH-322) without duplicating or being treated as a replay.
"""
import pytest

# A 1x1 PNG, same shape every attachment test here uses.
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def prd(client, auth):
    r = client.post("/api/prds", json={"title": "Handoff PRD", "project_id": "core"},
                    headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@pytest.fixture()
def item(client, auth, prd):
    """A high-fidelity item — the state `decompose_prd` leaves prototype-first work in."""
    r = client.post("/api/items", headers=auth,
                    json={"title": "Sync link first-run", "project_id": "core",
                          "description": "What the link page shows before anything is "
                                         "pushed — the ambiguous case prose cannot settle."})
    assert r.status_code in (200, 201), r.text
    key = r.json()["id"]
    p = client.patch(f"/api/items/{key}", json={"fidelity": "high"}, headers=auth)
    assert p.status_code == 200, p.text
    assert p.json()["fidelity"] == "high"
    return key


@pytest.fixture()
def shot(client):
    r = client.post("/api/public/attachments",
                    files={"file": ("screen.png", _PNG_1x1, "image/png")})
    assert r.status_code == 201, r.text
    return r.json()  # {"id", "url", "size"}


def _emit(client, auth, prd_id, item_id, **kw):
    return client.post(f"/api/prds/{prd_id}/grill/prototype", headers=auth,
                       json={"item_id": item_id, **kw})


def _verdict(client, auth, prd_id, item_id, attachment_id, verdict="the placement settles it"):
    return client.post(f"/api/prds/{prd_id}/grill/prototype/verdict", headers=auth,
                       json={"item_id": item_id, "attachment_id": attachment_id,
                             "verdict": verdict})


def _state(client, auth, prd_id):
    r = client.get(f"/api/prds/{prd_id}/grill", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


# ---- emit: the handoff carries its own context --------------------------------------
def test_emit_returns_a_paste_ready_pack_and_records_the_handoff(client, auth, prd, item):
    r = _emit(client, auth, prd, item, note="the empty state on first link")
    assert r.status_code == 200, r.text
    out = r.json()
    # The pack is genuinely the doc's shared preamble + the specific question — the thing
    # that made the old route "re-derive the context by hand" was the absence of both.
    assert "#0d0f0e" in out["prompt_pack"]
    assert item in out["prompt_pack"]
    assert "prototype to settle" in out["prompt_pack"]  # the open_decisions question text
    assert "empty state on first link" in out["prompt_pack"]

    turns = _state(client, auth, prd)["turns"]
    handoff = [t for t in turns if "Prototype handoff emitted" in t["text"]]
    assert len(handoff) == 1, "recorded once — the transcript must not stack duplicates"
    assert handoff[0]["role"] == "agent"
    assert handoff[0]["via"] == "prototype"


def test_emit_rejects_an_unknown_dimension(client, auth, prd, item):
    r = _emit(client, auth, prd, item, dimension="vibes")
    assert r.status_code == 422
    assert "open_decisions" in r.text  # tells the caller what the real names are


# ---- verdict: the point of the item — the artifact comes BACK to the grill ------------
def test_verdict_lands_on_the_item_and_reenters_the_grill(client, auth, prd, item, shot):
    _emit(client, auth, prd, item)
    r = _verdict(client, auth, prd, item, shot["id"],
                 verdict="one rail entry, no modal — the ambiguity is gone")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["artifact_url"] == shot["url"]

    # Evidence: the receipt is on the ITEM (what AL-68 surfaced), screenshot kind,
    # pointing at the stored bytes.
    got = client.get(f"/api/items/{item}", headers=auth).json()
    receipts = [e for e in got["evidence"] if e.get("kind") == "screenshot"]
    assert len(receipts) == 1
    assert receipts[0]["url"] == shot["url"]
    assert "ambiguity is gone" in receipts[0]["detail"]

    # Grill: the verdict is a USER turn citing the artifact — the only shape the
    # text-only grader can consume (there is no vision path in providers).
    turns = _state(client, auth, prd)["turns"]
    v = [t for t in turns if t["role"] == "user" and t["via"] == "prototype"]
    assert len(v) == 1
    assert "ambiguity is gone" in v[0]["text"]
    assert shot["url"] in v[0]["text"]


def test_verdict_proposes_the_flip_but_never_applies_it(client, auth, prd, item, shot):
    _verdict(client, auth, prd, item, shot["id"])
    r = _verdict(client, auth, prd, item, shot["id"])
    out = r.json()
    assert out["fidelity"] == "high"          # untouched by the server...
    assert out["fidelity_proposal"]["confirmed"] is False  # ...only proposed.
    assert out["fidelity_proposal"]["to"] == "low"
    got = client.get(f"/api/items/{item}", headers=auth).json()
    assert got["fidelity"] == "high"


def test_the_human_confirms_the_flip_on_the_item(client, auth, prd, item, shot):
    """The confirmation is an ordinary PATCH — this also pins that `fidelity` rides the
    web ItemUpdate schema, without which the loop had no confirm button at all."""
    _verdict(client, auth, prd, item, shot["id"])
    r = client.patch(f"/api/items/{item}", json={"fidelity": "low"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["fidelity"] == "low"


def test_a_second_verdict_adds_a_turn_and_leaves_the_receipt_record_growing(
        client, auth, prd, item, shot):
    _verdict(client, auth, prd, item, shot["id"], verdict="first reading")
    _verdict(client, auth, prd, item, shot["id"], verdict="second reading, after a variant")
    turns = _state(client, auth, prd)["turns"]
    assert sum(1 for t in turns if "first reading" in t["text"]) == 1
    assert sum(1 for t in turns if "second reading" in t["text"]) == 1
    got = client.get(f"/api/items/{item}", headers=auth).json()
    # `append_evidence` dedupes identical receipts; different verdicts both survive.
    details = [e.get("detail", "") for e in got["evidence"] if e.get("kind") == "screenshot"]
    assert len(details) == 2


# ---- what the handoff must NOT do ------------------------------------------------------
def test_verdict_requires_prose_not_just_a_picture(client, auth, prd, item, shot):
    r = _verdict(client, auth, prd, item, shot["id"], verdict="   ")
    assert r.status_code == 422
    assert "screenshot alone" in r.text


def test_verdict_requires_a_real_attachment(client, auth, prd, item):
    r = _verdict(client, auth, prd, item, "att-nope")
    assert r.status_code == 422
    assert "unknown attachment" in r.text


def test_a_foreign_item_is_invisible_to_the_handoff(client, auth, prd, decoy):
    """Cross-tenant item → 404, not 403: existence is the thing being hidden (AL-76)."""
    foreign = decoy["item_ids"][0]
    r = _emit(client, auth, prd, foreign)
    assert r.status_code == 404
    r = _verdict(client, auth, prd, foreign, "x" * 8)
    assert r.status_code == 404


# ---- the append rule survives server-authored turns in the middle ----------------------
def test_client_replay_after_handoff_turns_does_not_duplicate_them(client, auth, prd, item, shot):
    """`record_grill_turns` recognises a client replay by PREFIX against the stored
    transcript. The handoff writes turns the server authored; the contract this pins is
    that a client that has been watching the transcript can still replay the FULL history
    — including those turns — and nothing lands twice, with seqs contiguous."""
    _emit(client, auth, prd, item)
    _verdict(client, auth, prd, item, shot["id"], verdict="settled")
    first = _state(client, auth, prd)["turns"]
    assert [t["seq"] for t in first] == list(range(len(first)))

    # The client now replays those stored turns plus its own new answer.
    history = [{"role": t["role"], "text": t["text"]} for t in first]
    with client.stream("POST", f"/api/prds/{prd}/grill/stream", headers=auth,
                       json={"message": "agreed, spec it in words", "history": history}) as r:
        assert r.status_code == 200, r.text
        for _ in r.iter_lines():
            pass

    after = _state(client, auth, prd)["turns"]
    assert [t["seq"] for t in after] == list(range(len(after))), "seqs stay contiguous"
    texts = [t["text"] for t in after]
    assert sum(1 for t in texts if "Prototype handoff emitted" in t) == 1
    assert sum(1 for t in texts if "settled" in t and "Prototype verdict" in t) == 1
    assert any("spec it in words" in t for t in texts), "the genuinely new answer landed"
