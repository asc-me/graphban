"""One content-hash rule for the whole codebase (GRPH-406).

`CodeNode.content_hash` was agent-supplied and unspecified — the MCP schema said "Source
hash — powers staleness" and nothing more, and `code_graph.upsert_node` stored whatever
string arrived. That was adequate while one agent described one project and only ever
compared against its own previous value: self-consistency was all staleness needed.

**It stops being adequate the moment two hashes have to be compared across producers**, and
two places already do:

- `code_sync.compute_diff` decides what an incremental push sends by comparing
  `{path: content_hash}` against the last-pushed manifest. PRD-17 made several agents
  describing one project normal, so a second agent hashing differently re-pushes every path
  it touched. Churn, not corruption — but it is being paid now.
- Cross-repo duplication detection has no other proof-grade signal. The cloud holds
  summaries and structure, never source, so identical hashes are the only thing that can
  show two repos contain the same file. Unspecified, that signal cannot be used at all, and
  a detector's silence would read as "no duplication" when it means "no comparable hashes".

The definition is **not new**. `artifact_inventory` solved this exact problem for a
different surface and its reasoning is adopted verbatim rather than reinvented: trailing
whitespace is normalised because an editor adding a final newline is not a human forking a
file, and a flag that fires on that is one people learn to ignore — at which point it stops
protecting what it exists to protect.

The prefix is the part that is new, and it earns its place by making an unknown *look*
unknown. Rows written before this module hold digests of unknown provenance; without a
marker they are indistinguishable from specified ones, and the system would compare
incomparable values and be confidently wrong. Prefixed, a legacy hash stays perfectly
usable for same-project staleness — where it only ever has to match itself — and is
excluded from any comparison across producers.
"""
from __future__ import annotations

import hashlib

PREFIX = "sha256:"


def content_hash(text: str) -> str:
    """`sha256:<hex>` of the contents, with trailing whitespace normalised."""
    digest = hashlib.sha256((text or "").rstrip().encode("utf-8", "replace")).hexdigest()
    return f"{PREFIX}{digest}"


def bare_digest(text: str) -> str:
    """The digest without the prefix — what `artifact_inventory` has always stored.

    Kept so that surface's rows do not all read as forked on the next scan. A stored value
    is only ever compared against another value produced the same way there, which is the
    situation the prefix exists to distinguish and this one does not need.
    """
    return hashlib.sha256((text or "").rstrip().encode("utf-8", "replace")).hexdigest()


def comparable(value: str | None) -> bool:
    """Whether a stored hash may be compared with one from a DIFFERENT producer.

    Only a prefixed value qualifies. An unprefixed one was computed by an unknown rule, and
    the honest thing to do with an unknown is refuse to draw a conclusion from it — not
    guess that it probably used the same algorithm.
    """
    return bool(value) and value.startswith(PREFIX)
