"""Project tags and the key grammar they render (PRD-13).

A user-visible key is **not stored**. It is rendered from three things: the
project's current ``tag``, the entity's kind, and the entity's ``number``. The
stored id (``items.id`` and friends) is frozen at issue time and never rewritten,
so retagging a project is one UPDATE on one row and touches nothing else — no
foreign keys move, no audit rows are falsified, no alias chains form.

That matters because twelve columns across ten tables hold an entity id and only
three of them are enforced foreign keys; a design that rewrote ids would be
correcting nine columns by hand, forever, with the database checking none of them.

Tags are stored **uppercase**. That is what lets a plain UNIQUE constraint express
case-insensitive uniqueness on both engines, with no functional index.
"""
from __future__ import annotations

import re

TAG_MIN, TAG_MAX = 2, 4
TAG_RE = re.compile(rf"^[A-Z][A-Z0-9]{{{TAG_MIN - 1},{TAG_MAX - 1}}}$")

# Kind -> the letter that follows the tag. Items render bare so the overwhelmingly
# common case stays shortest; requests and PRDs take a single discriminating letter.
# `A` is agents (PRD-17). Agents are runtime entities rather than tracked work, but they
# are named in a Fleet view — "GRPH-A3 is stuck" — so they need a key, and a key rendered
# from the project tag is retag-safe for free. PRD-17's data model says "AGT-n"; that shape
# is the PRE-tag, product-wide prefix PRD-13 exists to replace (it is what LEGACY_KEY_RE
# still resolves), so the convention the same sentence cites wins over the literal example.
KIND_LETTER = {"item": "", "request": "R", "prd": "P", "agent": "A"}
_LETTER_KIND = {v: k for k, v in KIND_LETTER.items()}

# A rendered key. The first hyphen delimits the tag, so this stays unambiguous even
# for a tag that contains digits (`A1-R12` is request 12 of the project tagged A1).
KEY_RE = re.compile(rf"^([A-Za-z][A-Za-z0-9]{{{TAG_MIN - 1},{TAG_MAX - 1}}})-([RrPpAa]?)(\d+)$")


# A pre-tag id: `AL-12`, `AL-01`, `R-33`, `PRD-4`. The prefix was a product-wide
# constant, not a project tag, which is why these need a lookup table rather than
# tag history to stay resolvable.
LEGACY_KEY_RE = re.compile(r"^([A-Za-z]+)-0*(\d+)$")


def normalize(tag: str) -> str:
    return (tag or "").strip().upper()


def legacy_number(entity_id: str) -> int | None:
    """The numeric part of a pre-tag id — ``AL-01`` -> 1, ``PRD-12`` -> 12.

    Used by the backfill and by the interim mint path, so both read a stored id the
    same way. ``None`` when the id doesn't look like one, which the caller must handle
    rather than assume away.
    """
    m = LEGACY_KEY_RE.match((entity_id or "").strip())
    return int(m.group(2)) if m else None


def validate(tag: str) -> str:
    """Normalize and check, or raise ValueError naming the rule.

    Callers turn the message into a 422 — it is written to be shown to a human.
    """
    t = normalize(tag)
    if not TAG_RE.match(t):
        raise ValueError(
            f"tag must be {TAG_MIN}-{TAG_MAX} characters, start with a letter, and use "
            f"only letters and digits (got {tag!r})"
        )
    return t


def _tokens(name: str) -> list[str]:
    """Split a project name on separators *and* camelCase boundaries.

    ``Graphban`` -> ``[Agent, Ledger]``; ``glyphy-board`` -> ``[glyphy, board]``;
    ``Republiq`` -> ``[Republiq]``. Acronyms stay whole: ``MCPBridge`` -> ``[MCP, Bridge]``.
    """
    out: list[str] = []
    for part in (p for p in re.split(r"[^A-Za-z0-9]+", name or "") if p):
        out.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part) or [part])
    return out


def derive(name: str) -> str:
    """A plausible tag for a project name — a starting point, never the last word.

    Multi-token names give initials (``Graphban`` -> ``AL``); a single token gives
    its leading characters (``Republiq`` -> ``REPU``). Always returns something that
    passes ``validate``, so a caller that omits a tag still gets a usable one rather
    than an error.
    """
    toks = _tokens(name)
    if len(toks) >= 2:
        base = "".join(t[0] for t in toks)
    elif toks:
        base = toks[0]
    else:
        base = ""
    base = re.sub(r"^[0-9]+", "", base.upper())[:TAG_MAX]  # must start with a letter
    if len(base) < TAG_MIN:
        base = (base + "PJ")[:TAG_MAX]
    return base


def variants(base: str):
    """``base``, then numbered fallbacks that stay inside the length rule.

    ``GB`` -> ``GB``, ``GB2``, ``GB3`` … Used to de-collide a derived tag against
    whatever the deployment already holds. The caller stops at the first available one.
    """
    base = validate(base)
    yield base
    for n in range(2, 1000):
        suffix = str(n)
        cand = (base[: max(1, TAG_MAX - len(suffix))] + suffix)[:TAG_MAX]
        if TAG_RE.match(cand):
            yield cand


def render(tag: str, kind: str, number: int) -> str:
    """The user-visible key: ``GRPH-12``, ``GRPH-R33``, ``GRPH-P4``, ``GRPH-A7``.

    Always uses the project's *current* tag. There is no as-of variant: stored prose
    (PRD bodies, version snapshots, shard text) keeps whatever string was typed and is
    never re-rendered, so historical labels are a question that dissolves rather than a
    choice to make. Exactly one rendering path is the point.
    """
    return f"{normalize(tag)}-{KIND_LETTER[kind]}{number}"


def parse(key: str) -> tuple[str, str, int] | None:
    """``(tag, kind, number)`` for a current-form key, or ``None`` when it is not one.

    ``None`` is not an error — it means "try the other resolution sources" (tag history,
    then the legacy table).
    """
    m = KEY_RE.match((key or "").strip())
    if not m:
        return None
    return m.group(1).upper(), _LETTER_KIND[m.group(2).upper()], int(m.group(3))
