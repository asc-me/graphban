"""Code-locality clustering (feature B).

Relate items by shared *touchpoints* (files / globs / modules they affect) plus existing
typed links, so an agent can pick up a whole code-neighborhood in one pass instead of
context-switching. Also auto-populates the `code` link type from touchpoint overlap.
"""
from __future__ import annotations

import fnmatch
import re

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import Item, Link
from app.services import items as items_svc


def _has_glob(path: str) -> bool:
    return any(c in (path or "") for c in "*?")


def _filename_pattern(path: str) -> bool:
    """A `*` or `?` in the last segment that still names files, not the directory.

    `backend/tests/*` and `backend/tests/**` are the directory: the last segment is only
    wildcards. `backend/tests/test_prd*.py` is a claim about names. GRPH-1848: reading the
    second as the first joined every file in `backend/tests/` into one cluster.
    """
    seg = (path or "").strip().rstrip("/").rsplit("/", 1)[-1]
    if not _has_glob(seg):
        return False
    return bool(seg.replace("*", "").replace("?", ""))


def _directory_covers(area: str, path: str) -> bool:
    """True when `area` is a directory touchpoint that contains `path`.

    `backend/tests/`, `backend/tests` and `backend/tests/*` contain
    `backend/tests/test_prd*.py`. A sibling (`backend/tests/helpers`) does not: a filename
    pattern is not "somewhere in this directory"."""
    a = (area or "").strip().rstrip("/")
    p = (path or "").strip().rstrip("/")
    if not a or not p:
        return False
    seg = a.rsplit("/", 1)[-1]
    parent = a.rsplit("/", 1)[0] if seg and not seg.replace("*", "").replace("?", "") else a
    if not parent or (parent == a and _filename_pattern(a)):
        return False
    return p == parent or p.startswith(parent + "/")


def _atoms(pat: str) -> list | None:
    """Filename-glob atoms, or None when a construct is not worth proving disjoint.

    `*`, `?`, literals and `[seq]` / `[!seq]`. An unclosed class, or a class this parser
    will not stand behind, is None — the caller collides, which is the safe direction for
    two patterns that might name one file."""
    out: list = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if not out or out[-1] != ("star",):
                out.append(("star",))
            i += 1
        elif c == "?":
            out.append(("any",))
            i += 1
        elif c == "[":
            j = pat.find("]", i + 2 if i + 1 < n and pat[i + 1] in "!^" else i + 1)
            if j == -1:
                return None
            body = pat[i + 1:j]
            i = j + 1
            neg = bool(body) and body[0] in "!^"
            if neg:
                body = body[1:]
            if not body:
                return None
            chars: set[str] = set()
            k = 0
            while k < len(body):
                if body[k] == "\\" and k + 1 < len(body):
                    chars.add(body[k + 1])
                    k += 2
                elif k + 2 < len(body) and body[k + 1] == "-" and body[k + 2] != "]":
                    start, end = ord(body[k]), ord(body[k + 2])
                    if end < start or end - start > 256:
                        return None
                    chars.update(chr(ch) for ch in range(start, end + 1))
                    k += 3
                else:
                    chars.add(body[k])
                    k += 1
            if not chars:
                return None
            out.append(("class", frozenset(chars), neg))
        else:
            out.append(("lit", c))
            i += 1
    return out


def _atom_overlap(a: tuple, b: tuple) -> bool:
    """Can these two non-star atoms consume one character together?"""
    def chars(atom: tuple) -> set[str] | None:
        if atom[0] == "lit":
            return {atom[1]}
        if atom[0] == "any":
            return None
        if atom[2]:  # negated class: infinite. Overlap unless the other side is a known miss.
            return None
        return set(atom[1])

    ca, cb = chars(a), chars(b)
    if ca is None and cb is None:
        return True
    if ca is None:
        if a[0] == "class" and a[2]:
            return any(ch not in a[1] for ch in cb)
        return True
    if cb is None:
        if b[0] == "class" and b[2]:
            return any(ch not in b[1] for ch in ca)
        return True
    return bool(ca & cb)


def _globs_overlap(a: str, b: str) -> bool:
    """True when some filename matches both globs.

    `test_*.py` and `*_adoption.py` share `test_rule_adoption.py` even though neither
    pattern fnmatches the other's text. Two patterns this parser will not compare collide:
    a false "disjoint" is two workers on one file."""
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    aa, bb = _atoms(a), _atoms(b)
    if aa is None or bb is None:
        return True
    memo: dict[tuple[int, int], bool] = {}

    def rec(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(aa) and j == len(bb):
            memo[key] = True
            return True
        if i < len(aa) and aa[i] == ("star",):
            ok = rec(i + 1, j) or (j < len(bb) and rec(i, j + 1))
            memo[key] = ok
            return ok
        if j < len(bb) and bb[j] == ("star",):
            ok = rec(i, j + 1) or (i < len(aa) and rec(i + 1, j))
            memo[key] = ok
            return ok
        if i == len(aa) or j == len(bb):
            memo[key] = False
            return False
        ok = _atom_overlap(aa[i], bb[j]) and rec(i + 1, j + 1)
        memo[key] = ok
        return ok

    return rec(0, 0)


def _filename_globs_overlap(x: str, y: str) -> bool:
    """The last segments, which is the whole pattern once the directory already matches."""
    return _globs_overlap(x.rstrip("/").rsplit("/", 1)[-1], y.rstrip("/").rsplit("/", 1)[-1])


def why_match(x: str, y: str) -> str:
    """WHICH rule relates two touchpoints, or "" for none (GRPH-810).

    The rule name is the whole point. "These two items were serialised" is not actionable;
    "they were serialised because they are files in the same directory" is — a reader can
    look at the two paths and say whether that is true of the work.

    `directory` is the broad one, and it was the one nobody could see. Every pair of files in
    one directory relates, whatever they are, so a directory of five files collapses five items
    into one cluster. That is a defensible clustering heuristic and a costly reservation rule,
    and until this it was impossible to tell which of the two you were looking at.
    """
    x, y = x.strip(), y.strip()
    if not x or not y:
        return ""
    if x == y:
        return "exact"
    if (_has_glob(x) and fnmatch.fnmatch(y, x)) or (_has_glob(y) and fnmatch.fnmatch(x, y)):
        # GRPH-1399: a glob over a test folder and one named test file in it. Named, so the
        # clustering view can say why they did not collide, and in `NON_COLLIDING`, so they
        # do not. Test files are added per item and almost never edited by two items at once;
        # `backend/tests/*` held by one long build had reserved every new test file for its
        # whole lease, and stalled every drain whose item added one.
        if _test_glob_over_test_file(x, y) or _test_glob_over_test_file(y, x):
            return "test-glob"
        return "glob"
    # GRPH-1848: a filename pattern is a claim about names. It relates to a path that matches
    # it (the glob branch above), to another filename pattern that can name the same file, and
    # to a directory touchpoint that contains it. It does not relate to every other file in
    # the directory — that reading is what chained 31 items into one cluster.
    if _filename_pattern(x) and _filename_pattern(y):
        same_dir = "/" in x and "/" in y and x.rsplit("/", 1)[0] == y.rsplit("/", 1)[0]
        if same_dir and _filename_globs_overlap(x, y):
            return "glob"
        return ""
    if _filename_pattern(x) or _filename_pattern(y):
        pattern, other = (x, y) if _filename_pattern(x) else (y, x)
        if _directory_covers(other, pattern):
            return "directory"
        return ""
    if "/" in x and "/" in y and x.rsplit("/", 1)[0] == y.rsplit("/", 1)[0]:
        # GRPH-951: two NAMED files are a statement about exactly which files the work
        # touches. `NeedsYou.tsx` and `CueQueue.tsx` declared by two items are two claims that
        # do not overlap, and reading "same folder" as "same code" serialised nine
        # independent screens. The rule stays for the vague side — a glob or a directory
        # says "somewhere around here", and a neighbour is a fair guess at what that means.
        if is_file(x) and is_file(y):
            return ""
        return "directory"
    return ""


#: Rules `why_match` names that do NOT make two touchpoints collide (GRPH-1399). Named rather
#: than returned as "" so a view can explain the pair it let through.
NON_COLLIDING = frozenset({"test-glob"})

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs"})
_TEST_FILE = re.compile(r"^(test_.+|.+_test\.[^.]+|.+\.(test|spec)\.[^.]+)$")


def _test_glob_over_test_file(glob: str, path: str) -> bool:
    """Is `glob` a wildcard over a test folder and `path` one named test file under it?

    The folder is the glob's fixed part, before its first wildcard, and must contain a test
    directory segment (`tests`, `__tests__`, …). The file must be named like a test
    (`test_x.py`, `x_test.go`, `x.test.tsx`, `x.spec.ts`): `conftest.py`, fixtures and
    helpers are shared by every test in the folder, so a glob still reserves them.
    """
    if not any(c in glob for c in "*?[") or not is_file(path):
        return False
    # GRPH-1848: `backend/tests/test_prd*.py` names files. The exemption is a wildcard over
    # the folder (`backend/tests/*`), which is the touchpoint that was reserving every new
    # test. A filename pattern still collides with the test file it matches.
    if _filename_pattern(glob):
        return False
    fixed = re.split(r"[*?\[]", glob, maxsplit=1)[0]
    folder = fixed.rsplit("/", 1)[0] if "/" in fixed else ""
    if not _TEST_DIRS.intersection(folder.split("/")):
        return False
    return bool(_TEST_FILE.match(path.rsplit("/", 1)[-1]))


def is_file(path: str) -> bool:
    """Does this touchpoint name one concrete file, rather than a glob or a directory?

    Judged from the text alone, because a touchpoint is a declaration and not a path on disk:
    no glob character, no trailing slash, and a last segment with an extension (a leading dot
    counts: `.gitignore`). A bare name like `Makefile` reads as a directory, which errs toward
    clustering — the cheap direction, since a wrong split lets two workers into one file.
    """
    p = (path or "").strip()
    if not p or p.endswith("/") or any(c in p for c in "*?["):
        return False
    return "." in p.rsplit("/", 1)[-1]


def _match(x: str, y: str) -> bool:
    """Two touchpoints relate if equal, one glob-matches the other, or they share a directory
    and at least one of them is not a concrete file (GRPH-951) — unless the rule that relates
    them is one of `NON_COLLIDING` (GRPH-1399: a test-folder glob and a named test file).

    One definition, expressed through `why_match`: two functions answering "do these relate?"
    is how the reservation check and the partition came to disagree in the first place.
    """
    rule = why_match(x, y)
    return bool(rule) and rule not in NON_COLLIDING


def shared_touchpoints(a: list[str], b: list[str]) -> list[str]:
    """Touchpoints of `a` that relate to any touchpoint of `b`."""
    return sorted({x for x in (a or []) if any(_match(x, y) for y in (b or []))})


def _links_for(db: Session, item_id: str) -> dict[str, set[str]]:
    """other_item_id -> {link types} for links touching item_id."""
    rows = db.scalars(select(Link).where(or_(Link.a == item_id, Link.b == item_id))).all()
    out: dict[str, set[str]] = {}
    for lk in rows:
        other = lk.b if lk.a == item_id else lk.a
        out.setdefault(other, set()).add(lk.type)
    return out


class ItemPool(list):
    """A project's items plus every link between them, loaded once (two statements), so a
    caller relating many items to the pool does not re-list or re-query links per item."""

    links: dict[str, dict[str, set[str]]]


def load_pool(db: Session, project_id: str | None) -> ItemPool:
    pool = ItemPool(items_svc.list_items(db, project_id=project_id))
    pool.links = {}
    ids = [i.id for i in pool]
    if ids:
        for lk in db.scalars(select(Link).where(or_(Link.a.in_(ids), Link.b.in_(ids)))).all():
            pool.links.setdefault(lk.a, {}).setdefault(lk.b, set()).add(lk.type)
            pool.links.setdefault(lk.b, {}).setdefault(lk.a, set()).add(lk.type)
    return pool


def related_items(db: Session, item: Item, project_id: str | None,
                  pool: list[Item] | None = None) -> list[dict]:
    """Items related to `item` by shared touchpoints and/or existing links, best-first.

    Returns [{item, score, shared, link_types}]. Touchpoint overlap weighs most; a dependency
    link adds a strong base, any other link a small one.
    """
    a_tps = item.touchpoints or []
    links = pool.links.get(item.id, {}) if isinstance(pool, ItemPool) else _links_for(db, item.id)
    out = []
    candidates = pool if pool is not None else items_svc.list_items(db, project_id=project_id)
    for other in candidates:
        if other.id == item.id:
            continue
        shared = shared_touchpoints(a_tps, other.touchpoints or [])
        types = links.get(other.id, set())
        if not shared and not types:
            continue
        score = len(shared) * 2 + (2 if "dependency" in types else 0) + (1 if types else 0)
        out.append({"item": other, "score": score, "shared": shared, "link_types": sorted(types)})
    out.sort(key=lambda r: (-r["score"], r["item"].sort_order))
    return out


def next_cluster(
    db: Session, agent_id: str, project_id: str | None, max_items: int = 3, lease_seconds: int | None = None,
) -> list[dict]:
    """Claim the best ready item, then claim its related *ready* neighbours (up to max_items),
    assigning the whole cluster to `agent_id`. Returns [{item, shared, link_types}] for each
    claimed item — the seed first. Empty list when nothing is ready.
    """
    lease = lease_seconds or items_svc.DEFAULT_LEASE_SECONDS
    seed = items_svc.claim_next(db, agent_id, project_id=project_id, lease_seconds=lease)
    if seed is None:
        return []
    batch = [{"item": seed, "shared": [], "link_types": [], "seed": True}]
    for rel in related_items(db, seed, project_id):
        if len(batch) >= max_items:
            break
        # GRPH-1530. claim_item only refuses a stale in_progress reclaim. A fresh
        # neighbour whose dependency is still open is not one, and claim_next never
        # saw it — this loop is the handout. Skip it; the seed stays claimed.
        if items_svc.unfinished_dependency_ids(db, rel["item"]):
            continue
        try:
            claimed = items_svc.claim_item(db, rel["item"].id, agent_id, lease_seconds=lease)
        except (items_svc.OutOfScope, items_svc.ReachesOutsideTheRepo,
                items_svc.TierBelowNeeds):
            # A neighbour outside the seat's scope (GRPH-827). Skipped rather than fatal: the
            # seed was claimable and IS the work, and a related item in another PRD is exactly
            # what a scoped wave should decline to drag in. The seed cannot hit this — it came
            # through `claim_next`, which filters. GRPH-832 rides the same path for the same
            # reason: a neighbour that acts on a running system is dragged in by nothing.
            # GRPH-1647 rides it too: a neighbour whose bounce named a higher tier must not
            # fail the seed's claim. The empty-seed reply names `needs_tier` at the MCP
            # boundary, from the same list `claim_next` attaches.
            continue
        if claimed is not None:
            batch.append({"item": claimed, "shared": rel["shared"], "link_types": rel["link_types"], "seed": False})
    return batch


def sync_code_links(db: Session, item: Item, project_id: str | None = None) -> int:
    """Ensure a `code` link exists between `item` and every OPEN item sharing a touchpoint.
    Idempotent — skips pairs that already have any link. Returns the number created.

    Done items take no stored `code` link, on either end (GRPH-1068): linking to every finished
    item that ever touched a hub file gave one new item 178 links to history. `related_items`
    reads overlap live, so finished work stays discoverable without the stored edge."""
    if not item.touchpoints or item.status == "done":
        return 0
    from app.services import links as links_svc

    pid = project_id or item.project_id
    already = set(_links_for(db, item.id).keys())
    created = 0
    for other in items_svc.list_items(db, project_id=pid):
        if other.id == item.id or other.id in already or other.status == "done":
            continue
        shared = shared_touchpoints(item.touchpoints, other.touchpoints or [])
        if shared:
            links_svc.create_link(
                db, a=item.id, b=other.id, type_="code",
                reason="shares " + ", ".join(shared[:3]),
                confidence=min(1.0, 0.4 + 0.2 * len(shared)), project_id=pid,
            )
            created += 1
    return created


def prune_code_links(db: Session, item: Item) -> int:
    """Delete the `code` links on `item`, both directions — called when it reaches `done`
    (GRPH-1068). Only `code`: `dependency` and `semantic` links are authored and stay. Does not
    commit; returns the number removed."""
    res = db.execute(delete(Link).where(
        Link.type == "code", or_(Link.a == item.id, Link.b == item.id)))
    return res.rowcount or 0
