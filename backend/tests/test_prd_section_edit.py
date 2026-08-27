"""Editing ONE section of a PRD, and refusing a replace that never read (GRPH-357).

`get_prd` (GRPH-519) made the read possible. It did not make the write safe: `update_prd`
still replaced the body whole, and the only thing between an agent and a silently gutted
PRD was a sentence in the tool description saying to call `get_prd` first. Prose is not a
guard — an agent forty turns into a compacted context has not got that sentence any more.

Two mechanisms here, and they are deliberately not the same one:

* `section` makes the safe edit POSSIBLE — rewrite one `## ` heading's contents, splice
  every other byte back verbatim. It needs no read token because it cannot lose what it
  never read.
* `base_hash` makes the destructive edit ACCOUNTABLE — a whole-body replace must carry the
  hash it read, and is refused if the document has moved.

The load-bearing assertions are byte-for-byte. Re-parsing the result and comparing section
titles would pass against an implementation that silently reflowed every other section,
which is the failure mode this exists to prevent.
"""
from __future__ import annotations

import pytest

from app.services import prds as prd_svc

BODY = (
    "# Providers\n\n"
    "## 1. Overview\n\nOne registry.\n\n"
    "## 2. Key decisions\n\nKeyed by row.\n\n"
    "## 3. Resolution order\n\nLegacy, project, deployment, stub.\n"
)


@pytest.fixture()
def mcp_key(client, auth):
    return client.post("/api/api-keys", json={"name": "prd-editor"},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def prd(client, auth):
    r = client.post("/api/prds", json={"title": "P", "body": BODY, "project_id": "core"},
                    headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _rpc(client, key, tool, args):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _call(client, key, name, **args):
    res = _rpc(client, key, name, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(client, key, name, **args):
    res = _rpc(client, key, name, args)
    assert res.get("isError"), f"expected a refusal, got {res}"
    return res["content"][0]["text"]


# ── the splice, at the byte level ─────────────────────────────────────────────

def test_editing_one_section_leaves_every_other_byte_alone():
    """THE POINT. Not "the other sections are still present" — byte-identical, because an
    implementation that reflowed or re-emitted them would pass a presence check and still
    have rewritten a document nobody asked it to touch."""
    out = prd_svc.replace_section(BODY, "2. Key decisions", "Keyed by DEPLOYMENT.")
    assert out == BODY.replace("Keyed by row.", "Keyed by DEPLOYMENT.")


def test_the_heading_line_itself_is_not_editable():
    """Content is bounded to start AFTER the heading line, so a caller cannot rename a
    section by writing its contents — which would silently orphan every item whose
    `prd_section` points at the old title."""
    out = prd_svc.replace_section(BODY, "1. Overview", "## Sneaky\n\nrenamed")
    assert "## 1. Overview\n" in out
    assert prd_svc.parse_sections(out)[0] == "1. Overview"


def test_the_last_section_does_not_grow_a_trailing_blank_line():
    """The off-by-one that only shows on the final section, where there is no next heading
    to butt against and the separator rule differs."""
    out = prd_svc.replace_section(BODY, "3. Resolution order", "Deployment first.")
    assert out.endswith("Deployment first.\n")
    assert not out.endswith("\n\n")


def test_a_no_op_edit_returns_the_document_unchanged():
    """Read a section, write it straight back, get the same bytes. If this drifts, every
    read-modify-write silently rewrites the parts it did not mean to."""
    for title in prd_svc.parse_sections(BODY):
        assert prd_svc.replace_section(BODY, title, prd_svc.section_content(BODY, title)) == BODY


def test_a_caller_need_not_reproduce_the_title_exactly():
    """Matched on the same normalised key the rest of this module uses, so casing and the
    leading section number are tolerated — an agent quoting `Key decisions` back should not
    be defeated by the `2. ` it did not think to include.

    The tolerance is exactly `_section_key`'s and no wider: `2 Key decisions`, with no
    period, keeps its number and does NOT match, because that is a different heading as far
    as every other consumer of this key is concerned. A splice is a byte-level edit, so the
    matching has to be the boring kind."""
    out = prd_svc.replace_section(BODY, "KEY DECISIONS", "x")
    assert "x" in out and "Keyed by row." not in out
    with pytest.raises(prd_svc.SectionNotFound):
        prd_svc.replace_section(BODY, "2 Key decisions", "x")


def test_an_unknown_section_is_refused_and_says_what_exists():
    with pytest.raises(prd_svc.SectionNotFound) as e:
        prd_svc.replace_section(BODY, "9. Rollout", "later")
    assert "1. Overview" in str(e.value), "the refusal must name the sections that DO exist"


def test_a_duplicated_title_is_refused_rather_than_guessed():
    """Editing the first of two same-named sections is how the other quietly becomes the
    stale copy nobody is looking at."""
    doubled = BODY + "\n## 1. Overview\n\nA second one.\n"
    with pytest.raises(prd_svc.AmbiguousSection):
        prd_svc.replace_section(doubled, "1. Overview", "x")


# ── fences ────────────────────────────────────────────────────────────────────

def test_a_heading_inside_a_code_fence_is_not_a_section():
    """No PRD in this repo does this today, which is exactly why it had to be handled
    before anything started writing bytes based on these offsets: the first document to
    contain a markdown example would have had a section boundary placed inside its code
    block, and the damage would be silent."""
    fenced = ("## 1. Overview\n\nSee below.\n\n```markdown\n## Not a heading\n```\n\n"
              "## 2. Next\n\nreal.\n")
    assert prd_svc.parse_sections(fenced) == ["1. Overview", "2. Next"]
    out = prd_svc.replace_section(fenced, "1. Overview", "See below.\n\n```markdown\n## Not a heading\n```")
    assert out == fenced


def test_the_lister_and_the_splitter_cannot_disagree():
    """`parse_sections` derives from `section_spans`. Two parsers with two notions of what
    a section is would corrupt precisely the documents hardest to notice corruption in."""
    fenced = "## A\n\n```\n## B\n```\n\n## C\n\nx\n"
    assert prd_svc.parse_sections(fenced) == [t for t, _s, _e in prd_svc.section_spans(fenced)]
    assert prd_svc.parse_sections(fenced) == ["A", "C"]


# ── the read token ────────────────────────────────────────────────────────────

def test_the_hash_moves_with_the_body_and_version_does_not():
    """Why this is a hash and not `version`: `version` only advances on an explicit
    `create_version` snapshot, so two entirely different bodies routinely share one and it
    cannot answer "is this still what I read"."""
    assert prd_svc.body_hash(BODY) != prd_svc.body_hash(BODY + "\n## 4. More\n\nx\n")
    assert prd_svc.body_hash(BODY) == prd_svc.body_hash(BODY)


def test_a_whole_body_replace_without_a_read_is_refused(client, mcp_key, prd):
    """The guard. This is the call that silently deleted every section it failed to
    reproduce, and nothing refused it."""
    msg = _err(client, mcp_key, "update_prd", prd_id=prd, body="# Gutted\n")
    assert "base_hash" in msg and "get_prd" in msg
    assert "## 2. Key decisions" in _call(client, mcp_key, "get_prd", prd_id=prd)["body"]


def test_a_stale_hash_is_refused_rather_than_winning(client, mcp_key, prd):
    """Two writers, one document. The second must not silently erase the first — the
    refusal names both hashes so the agent can tell staleness from a typo."""
    stale = _call(client, mcp_key, "get_prd", prd_id=prd)["body_hash"]
    _call(client, mcp_key, "update_prd", prd_id=prd, section="1. Overview",
          content_ignore=None, body="Someone else got here first.")
    msg = _err(client, mcp_key, "update_prd", prd_id=prd, base_hash=stale, body="# Mine\n")
    assert stale in msg
    assert "Someone else got here first." in _call(client, mcp_key, "get_prd", prd_id=prd)["body"]


def test_a_section_edit_needs_no_read_token(client, mcp_key, prd):
    """Exempt on purpose, not by omission: a section edit cannot lose what it did not read.
    Requiring a token would make the SAFE call as awkward as the dangerous one, which is how
    agents end up reaching for the dangerous one."""
    _call(client, mcp_key, "update_prd", prd_id=prd, section="2. Key decisions",
          body="Keyed by DEPLOYMENT.")
    after = _call(client, mcp_key, "get_prd", prd_id=prd)["body"]
    assert after == BODY.replace("Keyed by row.", "Keyed by DEPLOYMENT.")


def test_a_fresh_hash_is_accepted(client, mcp_key, prd):
    """The complement, so the guard cannot degenerate into refusing everything — which
    would pass every refusal test above perfectly."""
    read = _call(client, mcp_key, "get_prd", prd_id=prd)
    _call(client, mcp_key, "update_prd", prd_id=prd, base_hash=read["body_hash"],
          body="# Replaced\n\n## 1. Overview\n\nnew.\n")
    assert "# Replaced" in _call(client, mcp_key, "get_prd", prd_id=prd)["body"]


def test_the_hash_a_read_returns_is_the_one_a_write_accepts(client, mcp_key, prd):
    """Pins the two ends together. A read emitting a hash over one representation and a
    write checking another would refuse every honest caller."""
    read = _call(client, mcp_key, "get_prd", prd_id=prd)
    assert read["body_hash"] == prd_svc.body_hash(read["body"])


# ── the REST caller is deliberately untouched ─────────────────────────────────

def test_rest_can_still_replace_a_body_without_a_token(client, auth, prd):
    """Scoped to MCP on purpose. The REST caller is a human editing a textarea they are
    looking at — they have read it by construction, and demanding a token there breaks the
    UI for no safety gain."""
    r = client.patch(f"/api/prds/{prd}", json={"body": "# Rewritten\n"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "# Rewritten\n"
