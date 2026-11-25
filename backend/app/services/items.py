"""Item (tracker) service — shared by REST routers and the MCP server."""
from __future__ import annotations

import logging
import re

from contextlib import contextmanager
from datetime import timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import Refused
from app.models import Agent, Enrolment, Item, Link, Prd, Project, utcnow
from app.services import keys
from app.services import routing as routing_svc

logger = logging.getLogger(__name__)

STATUSES = ["backlog", "next", "in_progress", "review", "done", "blocked"]
FIDELITIES = ["low", "high"]  # low = specifiable now; high = needs a prototype (AL-68)
#: What kind of change an item is (GRPH-1358). Empty string = not recorded, which is a third
#: answer and not a synonym for any of these: an old row must not read as "not a feature".
KINDS = ["feature", "fix", "chore"]


def _check_kind(kind: str) -> None:
    if kind not in KINDS and kind != "":
        raise ValueError(
            f"invalid kind: {kind!r} — one of {', '.join(KINDS)}, or empty for not recorded")


def check_line(line_id: str | None, prd_id: str | None, kind: str | None) -> None:
    """A line item is a `fix` and has no PRD (GRPH-1681). Empty `kind` is not a fix."""
    if not line_id:
        return
    if prd_id:
        raise ValueError("an item cannot have both prd_id and line_id")
    if kind != "fix":
        raise ValueError(
            f"only a fix can carry line_id (kind is {kind or 'empty'!r}); "
            "a feature or chore belongs to a PRD")

#: What `severity` may hold (GRPH-1683). Empty = not recorded.
SEVERITIES = ["security"]

#: The dependency reason and the blocker text filing a blocking fix writes (GRPH-1696).
#: `release_when_blocking_fix_done` reads both. A manual blocker does not end with the suffix.
BLOCKING_FIX_REASON = "blocking fix"
BLOCKING_FIX_BLOCKER_SUFFIX = ": blocking fix filed on this item"

#: Dependency reason written when a blocker text names the items it waits on (GRPH-1530).
#: A manual note, a typed wait, and a blocking-fix filing do not use it. The fleet drain
#: matches the same key grammar and the same person/decision words; keep them together.
BLOCKER_DEP_REASON = "blocker"
_BLOCKER_KEY = re.compile(
    r"(?<![\w/.-])([A-Za-z][A-Za-z0-9]{1,3}-[RrPpAaLl]?\d+)(?![\w/.-])"
)
_PERSON_OR_DECISION = re.compile(r"\b(?:person|human|decision)\b", re.IGNORECASE)

#: The three exits an agent that finds a defect has. Every fix refusal quotes them.
FIX_EXITS = (
    "a defect has three exits: fix it on the item you hold (only when build.found_bug is "
    "fix_in_build); file a blocking fix on a PRD — create_item(kind=fix, prd_id=<the PRD>, "
    "blocks=<an item on that PRD>, description with ## Acceptance, touchpoints); or file it on "
    "the maintenance line — create_item(kind=fix, line_id=<get_context.maintenance_line.id>, "
    "description with ## Acceptance, touchpoints)")


def _check_severity(severity: str, kind: str | None) -> None:
    if severity not in SEVERITIES and severity != "":
        raise ValueError(
            f"invalid severity: {severity!r} — {', '.join(SEVERITIES)}, or empty for not recorded")
    if severity and kind != "fix":
        raise ValueError(
            f"severity={severity} is only for a fix (kind is {kind or 'empty'!r}); "
            "set kind=fix, or drop severity")


def is_filed_fix(db: Session, item) -> bool:
    """A fix filed under the GRPH-1683 rules: on the line, or a blocking fix on a PRD.

    A blocking fix is a `fix` on a PRD with no section and an item on that PRD depending on
    it. A historical fix with a section, or with nothing waiting on it, is neither, and keeps
    today's sign-off rule.
    """
    if item.kind != "fix":
        return False
    if item.line_id:
        return True
    return bool(item.prd_id) and not item.prd_section and has_blocking_dependency(db, item)


def check_fix_scope(*, kind: str | None, line_id: str | None, prd_id: str | None,
                    prd_section: str) -> None:
    """A fix must say where it lives (GRPH-1683).

    `create_item` may already have filled `line_id` from the project's maintenance line
    (GRPH-1798). This check does not look the line up itself: a fix that still has
    neither scope is refused, which is the behaviour when the project has no line.
    """
    if kind != "fix":
        return
    if not line_id and not prd_id:
        raise ValueError(f"a fix needs a scope: it has neither prd_id nor line_id. {FIX_EXITS}")
    if prd_id and not line_id and prd_section:
        raise ValueError(
            f"a blocking fix on a PRD carries no prd_section (got {prd_section!r}): it is not "
            "a section's work. Drop prd_section; to put it on a section, file a feature")


def check_fix_shape(*, description: str, touchpoints) -> None:
    """Acceptance and touchpoints a filed fix must carry; the refusal names which is missing."""
    from app.services.fleet import parse_acceptance
    missing = []
    if not parse_acceptance(description or "")[0]:
        missing.append("an acceptance clause (a list item under `## Acceptance` in the "
                       "description)")
    if not [t for t in (touchpoints or []) if isinstance(t, str) and t.strip()]:
        missing.append("a touchpoint (touchpoints=[...] naming the files the fix changes)")
    if missing:
        raise ValueError("a fix needs " + " and ".join(missing) + " before it can be filed")


def _blocked_item(db: Session, blocks: str | None, prd_id: str | None) -> Item:
    """The item on `prd_id` that a blocking fix holds up (the dependency's source)."""
    if not blocks:
        raise ValueError(
            "a blocking fix needs `blocks`: an item on the same PRD that depends on it, so the "
            f"fix is not filed with nothing waiting on it. {FIX_EXITS}")
    blocked = db.get(Item, keys.resolve_item(db, blocks) or blocks)
    if blocked is None:
        raise ValueError(f"blocks: unknown item {blocks!r}")
    if blocked.prd_id != prd_id:
        raise ValueError(
            f"blocks: {blocked.key} is not on the PRD this fix is filed on; name an item on "
            "that PRD (it may differ from the PRD you are building)")
    return blocked


def has_blocking_dependency(db: Session, item: Item) -> bool:
    """True if an item on `item`'s PRD depends on it."""
    from app.models import Link
    srcs = db.scalars(select(Link.a).where(Link.b == item.id, Link.type == "dependency")).all()
    return any((o := db.get(Item, a)) is not None and o.prd_id == item.prd_id for a in srcs)


def lines_svc_resolve(db: Session, line_id: str, project_id: str) -> str:
    from app.services import lines as lines_svc
    return lines_svc.resolve_for_item(db, line_id, project_id)


def _open_overlaps(db: Session, *, line_id: str | None, prd_id: str | None,
                   touchpoints, exclude=()) -> list[Item]:
    """Open items on the same scope (the line, else the PRD) sharing a touchpoint (GRPH-1695).

    `exclude` are ids that are not candidate duplicates: the item a blocking fix holds and
    the item the fix was found during — a fix usually touches exactly their files."""
    from app.services.clustering import shared_touchpoints
    scope = Item.line_id == line_id if line_id else Item.prd_id == prd_id
    rows = db.scalars(select(Item).where(scope, Item.status != "done")).all()
    return [o for o in rows if o.id not in set(exclude)
            and shared_touchpoints(o.touchpoints or [], touchpoints or [])]


def check_duplicates(db: Session, *, line_id: str | None, prd_id: str | None,
                     touchpoints, distinct_from, exclude=()) -> list[tuple[Item, str]]:
    """Refuse a fix overlapping an open item on its scope unless every overlap is named in
    `distinct_from` with a reason. Returns the (item, reason) pairs to link."""
    named: list[tuple[Item, str]] = []
    for entry in distinct_from or []:
        ref = entry.get("id") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if not isinstance(ref, str) or not isinstance(reason, str) or not reason.strip():
            raise ValueError("distinct_from entries are {id, reason}, and the reason says in "
                             f"what way the fix is not a duplicate; got {entry!r}")
        other = db.get(Item, keys.resolve_item(db, ref) or ref)
        if other is None:
            raise ValueError(f"distinct_from: unknown item {ref!r}")
        named.append((other, reason.strip()))
    overlaps = _open_overlaps(db, line_id=line_id, prd_id=prd_id, touchpoints=touchpoints,
                              exclude=exclude)
    left = [o for o in overlaps if o.id not in {n.id for n, _ in named}]
    if left:
        raise ValueError(
            "possible duplicate: open items on this scope already touch the same files: "
            + ", ".join(o.key for o in left)
            + ". Resubmit with distinct_from=[{id, reason}] naming each, with why the fix is "
            "not the same work")
    return named


def resolve_found_during(db: Session, *, found_during: str | None,
                         held: list[Item]) -> Item | None:
    """The item a fix was found on: named, else the one item the caller holds (GRPH-1695)."""
    if found_during:
        item = db.get(Item, keys.resolve_item(db, found_during) or found_during)
        if item is None:
            raise ValueError(f"found_during: unknown item {found_during!r}")
        return item
    if len(held) > 1:
        raise ValueError("you hold more than one item, so say which one this fix was found "
                         "during: found_during=<one of " + ", ".join(h.key for h in held) + ">")
    return held[0] if held else None


def found_during_of(db: Session, item: Item) -> str | None:
    """Key of the item `item` was found on, or None (not recorded)."""
    from app.models import Link
    b = db.scalar(select(Link.b).where(Link.a == item.id, Link.type == "found_during"))
    other = db.get(Item, b) if b else None
    return other.key if other is not None else None


def refuse_overlap_with_held(db: Session, *, touchpoints, held: list[Item],
                             project_id: str) -> None:
    """`build.found_bug` under `fix_in_build`: a new fix over the files of an item the caller
    holds is fixed on that item, not filed (GRPH-1682). `file_ticket` does not refuse."""
    from app.services import rule_toggles
    from app.services.clustering import shared_touchpoints
    if rule_toggles.found_bug_mode(db, project_id) != "fix_in_build":
        return
    hit = [h for h in held if shared_touchpoints(h.touchpoints or [], touchpoints or [])]
    if hit:
        raise Refused(
            409, f"{rule_toggles.FOUND_BUG_RULE} is fix_in_build: this fix touches the files of "
            f"{hit[0].key}, which you hold. Fix the bug on {hit[0].key} instead of filing a "
            "ticket for it", rule_id=rule_toggles.FOUND_BUG_RULE)


def refuse_review_over_open_fix(db: Session, item: Item) -> None:
    """`build.found_bug` under `file_ticket`: the held item does not reach review while its
    diff touches a path on an open fix found during it (GRPH-1682)."""
    from app.models import Link
    from app.services import rule_toggles
    from app.services.clustering import shared_touchpoints
    if rule_toggles.found_bug_mode(db, item.project_id) != "file_ticket":
        return
    ids = db.scalars(select(Link.a).where(Link.b == item.id, Link.type == "found_during")).all()
    for fid in ids:
        fix = db.get(Item, fid)
        if (fix is not None and fix.kind == "fix" and fix.status != "done"
                and shared_touchpoints(item.touchpoints or [], fix.touchpoints or [])):
            raise Refused(
                409, f"{rule_toggles.FOUND_BUG_RULE} is file_ticket: {item.key} cannot move to "
                f"review while its diff touches a path on open fix {fix.key}. Finish {fix.key}, "
                "or take those paths out of this item's diff",
                rule_id=rule_toggles.FOUND_BUG_RULE)


def _default_kind(tags) -> str:
    """The kind an item gets when its creator does not say: `fix` for a `bug`-tagged item,
    otherwise empty (not recorded). Never `feature` — that is only ever declared."""
    if any(isinstance(t, str) and t.strip().lower() == "bug" for t in (tags or [])):
        return "fix"
    return ""
DEFAULT_LEASE_SECONDS = 600  # a claim with no heartbeat within this window is reclaimable


def _stored_prd_id(db: Session, prd_id: str | None, project_id: str | None = None) -> str | None:
    """The frozen id a caller's PRD key means, for storing in `Item.prd_id`.

    `keys` resolves the entity a call *addresses*; this covers the entity a call
    *references*. Without it a caller who passes a live rendering (`GRPH-P12`) freezes
    that rendering into the row, and `coverage` — the one place that joins on the raw
    string — stops seeing the item. Reads hide it: `_key_of` renders a dangling
    reference back as itself, so the item looks correctly linked from every surface.

    Unresolvable values are stored as given rather than dropped, matching how the rest
    of the resolve path degrades: a dangling reference is recoverable, a silently
    discarded link is not.

    **A key that resolves into ANOTHER project is refused** (GRPH-457). `resolve_prd` is
    global, so `create_item(project_id="gliphy-board", prd_id="GRPH-P22")` resolved cleanly
    against a PRD in `agentledger` and froze it into the row. That is worse than the dangling
    case the paragraph above describes, because the reference resolves — so it looks *more*
    correct, while `prd_coverage` is project-scoped and never counts the item under the PRD
    it names. An item that reads as linked on every surface and contributes to no PRD's
    coverage anywhere is an absence reading as clean, in the traceability surface PRD-12
    exists to make trustworthy.

    Loud rather than normalised, for the reason the store-as-given branch is loud in the
    other direction: there is no supported meaning for a cross-project reference — numbering
    and identity are per-project — so quietly dropping it would hide a caller's mistake
    instead of correcting it.

    `resolve_prd` itself stays global on purpose. It also resolves the entity a call
    *addresses*, and `prd_coverage("GRPH-P22")` legitimately crosses projects because the
    caller may not hold that project as their key's default.
    """
    if not prd_id:
        return prd_id
    resolved = keys.resolve_prd(db, prd_id)
    if resolved is None:
        return prd_id
    if project_id:
        owner = db.scalar(select(Prd.project_id).where(Prd.id == resolved))
        if owner and owner != project_id:
            raise ValueError(
                f"{prd_id} belongs to project {owner!r}, not {project_id!r} — an item "
                "cannot reference a PRD in another project. PRD numbering is per-project, "
                "so this key means a different document here or nothing at all."
            )
    return resolved


def list_items(db: Session, project_id: str | None = None, status: str | None = None) -> list[Item]:
    stmt = select(Item)
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    if status:
        stmt = stmt.where(Item.status == status)
    stmt = stmt.order_by(Item.sort_order.asc(), Item.created_at.desc())
    return list(db.scalars(stmt).all())


@contextmanager
def batched_row_lookups(db: Session, items: list[Item]):
    """Hold what a rendered row looks up, for the length of the render (GRPH-1898).

    A list row renders `prd_id` as the PRD's key and `question_state` from the row's
    dependency waits. Read a row at a time, each costs statements of its own: a 400-row
    project measured 135 of them for one page — 80 PRD lookups and 51 link queries — against
    the 7 a flat read needs.

    Yields the referenced PRDs, and holding them IS the fix rather than a formality: the
    identity map keeps objects weakly, so a PRD nothing else references is collected the
    moment its key is rendered and the next row queries it again.
    """
    from app.services import waits as waits_svc

    prd_ids = {i.prd_id for i in items if i.prd_id}
    held = list(db.scalars(select(Prd).where(Prd.id.in_(prd_ids)))) if prd_ids else []
    with waits_svc.batched(db, items):
        yield held


def get_item(db: Session, item_id: str) -> Item | None:
    return db.get(Item, keys.resolve_item(db, item_id) or item_id)


def default_maintenance_line(db: Session, *, kind: str | None, explicit_kind: bool,
                             agent_create: bool, prd_id: str | None,
                             line_id: str | None, project_id: str) -> tuple[str | None, bool]:
    """GRPH-1798. A filed fix with neither scope lands on the project's maintenance line.

    An explicit `prd_id` or `line_id` is returned unchanged. A project with no line
    returns the caller's `line_id` and False, so the scope refusal is unchanged.

    Only a filed fix takes the default: an explicit `kind=fix`, or any fix an agent
    files (a `bug` tag defaults kind there too). A non-agent bug-tagged default is
    the historical path that predates the filing rules and stays unscoped.
    """
    if kind != "fix" or not (explicit_kind or agent_create) or prd_id or line_id:
        return line_id, False
    from app.services import lines as lines_svc
    line = lines_svc.get_line(db, project_id)
    if line is None:
        return line_id, False
    return line.id, True


def create_item(
    db: Session,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    effort: int = 0,
    status: str = "backlog",
    project_id: str = "core",
    reporter: dict | None = None,
    date: str = "",
    touchpoints: list[str] | None = None,
    prd_id: str | None = None,
    prd_section: str = "",
    fidelity: str = "low",
    commit: bool = True,
    question: str | None = None,
    options: list[str] | None = None,
    kind: str | None = None,
    line_id: str | None = None,
    severity: str = "",
    blocks: str | None = None,
    agent_create: bool = False,
    distinct_from: list | None = None,
    found_during: str | None = None,
    held: list[Item] | None = None,
) -> Item:
    """Create a tracked item.

    `kind` unset (None) takes `_default_kind(tags)`; an explicit `""` records "not recorded".

    `question`/`options` file it as a structured decision wait (`waits.set_decision`).

    ``commit=False`` leaves the row flushed but uncommitted so a caller can make the item
    and something that references it one transaction — `accept_request` needs the item and
    its link to land together, because an item that exists while its request still sits in
    the triage queue reads as untriaged work that is already on the board.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    if status == "done":
        # Constructor used to assign `done` with empty evidence (GRPH-543 bounce). Completing
        # is a transition, not a birth status — create as backlog/next and go through
        # update_item or sign_off.
        raise MissingAttestation(
            "cannot create an item as done: `done` needs an `attestation` receipt naming "
            "the adapter, the commit it binds to, and at least one predicate that passed — "
            "written by a key with the `gate` scope (CI, or a reviewer via sign_off). "
            "Create as backlog or next, then complete it"
        )
    if fidelity not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fidelity}")
    explicit_kind = kind is not None
    kind = _default_kind(tags) if kind is None else kind
    _check_kind(kind)
    severity = severity or ""
    _check_severity(severity, kind)
    if db.get(Project, project_id) is None:
        raise ValueError(f"unknown project: {project_id!r}")
    line_id, line_defaulted = default_maintenance_line(
        db, kind=kind, explicit_kind=explicit_kind, agent_create=agent_create,
        prd_id=prd_id, line_id=line_id, project_id=project_id)
    check_line(line_id, prd_id, kind)
    blocked = None
    distinct: list[tuple[Item, str]] = []
    origin: Item | None = None
    # An explicit `fix`, or any `fix` an agent files (a bug tag defaults it there too), is a
    # filed fix (GRPH-1683). Only a non-agent caller's bug-tagged default predates the rules.
    if kind == "fix" and (explicit_kind or agent_create):
        check_fix_scope(kind=kind, line_id=line_id, prd_id=prd_id, prd_section=prd_section)
        if agent_create:
            check_fix_shape(description=description, touchpoints=touchpoints)
        if prd_id and not line_id:
            blocked = _blocked_item(db, blocks, _stored_prd_id(db, prd_id, project_id))
        if agent_create:
            origin = resolve_found_during(db, found_during=found_during, held=held or [])
            refuse_overlap_with_held(db, touchpoints=touchpoints, held=held or [],
                                     project_id=project_id)
            distinct = check_duplicates(
                db, line_id=lines_svc_resolve(db, line_id, project_id) if line_id else None,
                prd_id=_stored_prd_id(db, prd_id, project_id), touchpoints=touchpoints,
                distinct_from=distinct_from,
                exclude=[x.id for x in (blocked, origin) if x is not None])
    elif blocks:
        raise ValueError("blocks is only for a blocking fix (kind=fix with prd_id)")
    if line_id:
        line_id = lines_svc_resolve(db, line_id, project_id)
    effort_val = int(effort or 0)
    if effort_val < 0:
        raise ValueError(f"negative effort: {effort_val}")
    max_order = db.scalar(select(func.max(Item.sort_order))) or 0
    # The id is frozen identity; `number` is what the key renders from (PRD-13).
    item_id, number = keys.mint(db, project_id, "item")
    item = Item(
        id=item_id,
        number=number,
        project_id=project_id,
        title=title,
        description=description or "",
        tags=tags or [],
        touchpoints=touchpoints or [],
        effort=effort_val,
        status=status,  # done refused above via MissingAttestation
        sort_order=max_order + 1,
        reporter=reporter or {},
        date=date,
        prd_id=_stored_prd_id(db, prd_id, project_id),
        line_id=line_id or None,
        prd_linked_at=utcnow() if prd_id else None,
        prd_section=prd_section or "",
        fidelity=fidelity,
        kind=kind,
        severity=severity,
    )
    if question or options:
        from app.services.waits import set_decision
        set_decision(item, question, options)
    db.add(item)
    if blocked is not None:
        from app.models import Link
        db.flush()
        # In the same transaction as the fix: a blocking fix with nothing waiting on it is
        # the state this refuses.
        db.add(Link(project_id=blocked.project_id, a=blocked.id, b=item.id, type="dependency",
                    reason=BLOCKING_FIX_REASON, confidence=1.0))
    if distinct or origin is not None:
        from app.models import Link
        db.flush()
        for other, reason in distinct:
            db.add(Link(project_id=item.project_id, a=item.id, b=other.id, type="semantic",
                        reason=reason, confidence=1.0))
        if origin is not None:
            db.add(Link(project_id=item.project_id, a=item.id, b=origin.id,
                        type="found_during", reason="found during", confidence=1.0))
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()  # assigns the row without ending the caller's transaction
    # GRPH-1696: the holder stops when they are the blocked item. A dependency written after
    # the fact does not retract a lease; filing the blocking fix does. A held item is one the
    # caller holds (`held` is built from the caller's own claims), so another agent's lease on
    # the blocked item is never touched.
    if blocked is not None and agent_create and any(h.id == blocked.id for h in held or []):
        holder = blocked.claimed_by
        if holder and release_item(db, blocked.id, holder, to_status="blocked",
                                   reason=f"blocked by the fix {item.key} it filed") is not None:
            blocked.blocker = f"{item.key}{BLOCKING_FIX_BLOCKER_SUFFIX}"
            if commit:
                db.commit()
                db.refresh(item)
            else:
                db.flush()
    if item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)
    if any(isinstance(t, str) and t.strip().lower() == "bug" for t in (item.tags or [])):
        from app.services import harness as harness_svc
        harness_svc.on_bug_filed(db, item)
        if commit:
            db.commit()
    if line_defaulted:
        # Reply-only. Not a column: a later read of the row must not say the line was
        # defaulted, and a refresh must not drop the flag before the caller serializes.
        db.info.setdefault("line_defaulted", set()).add(item.id)
    return item


_EVIDENCE_KINDS = {"test", "url", "screenshot", "health", "note", "sabotage", "attestation", "lesson"}

# What a `sabotage` receipt must carry to be one (GRPH-321). Without these it is a `note`
# wearing a stronger name, and a gate that counted it would be satisfied by prose.
_SABOTAGE_FIELDS = ("claim", "mutation", "tests_failed")

# The run a `test` receipt may describe as fields (GRPH-1271). `scope` is the full suite or
# not; `passed` / `failed` / `skipped` are pytest's summary counts.
_TEST_SCOPES = ("full", "partial")
_TEST_COUNTS = ("passed", "failed", "skipped")


def _test_run_fields(e: dict) -> dict:
    """The valid structured run fields of a `test` receipt; an invalid one is left out."""
    out: dict = {}
    engine = e.get("engine")
    if isinstance(engine, str) and engine.strip():
        out["engine"] = engine.strip().lower()
    scope = e.get("scope")
    if isinstance(scope, str) and scope.strip().lower() in _TEST_SCOPES:
        out["scope"] = scope.strip().lower()
    for k in _TEST_COUNTS:
        n = e.get(k)
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            out[k] = n
    return out


# What an `attestation` receipt must carry to be one (GRPH-542). Same bargain as `sabotage`
# above: a structured kind that accepts unstructured input is the free-text field with a
# stronger name, and the completion gate would then be checking a label rather than a fact.
_ATTESTATION_FIELDS = ("adapter", "commit", "predicates")


def _normalize_predicates(raw) -> list[dict]:
    """Coerce an attestation's predicate list to `[{name, passed, detail}]`.

    **Structure is validated; vocabulary is not.** An adapter must stay free to check more
    than Graphban knows about, or every adapter improvement becomes a breaking change to the
    core. So an unrecognised predicate name is recorded, not rejected.

    `passed` must be a real bool. This is the fiddly part and it is load-bearing: a JSON
    client that sends `"passed": "false"` would otherwise store a truthy string, and the gate
    — which asks `all(p["passed"])` — would read a failure as a pass. Rejecting the row is
    right rather than coercing it, because there is no reading of `"false"` that is safe to
    guess at.

    `compared` and `waived` are carried through because the gate reads them (GRPH-1007): a
    predicate whose check could not run reports `passed: False, compared: False`, and a
    reviewer's `waived` reason is what admits it. This function REBUILDS each row from named
    fields rather than copying it, so anything not named here is silently dropped — which is
    how the pair would have survived every in-memory test and been inert the moment an
    attestation was stored and read back. Add a field the gate consults, add it here.
    """
    out: list[dict] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        passed = p.get("passed")
        if not name or not isinstance(passed, bool):
            continue
        row = {"name": name, "passed": passed,
               "detail": str(p.get("detail") or "").strip()}
        # Only when actually present: absent is "this adapter does not report it", which is
        # not the same claim as `compared: False` ("the check did not run").
        if isinstance(p.get("compared"), bool):
            row["compared"] = p["compared"]
        if str(p.get("waived") or "").strip():
            row["waived"] = str(p["waived"]).strip()
        out.append(row)
    return out


def normalize_evidence(raw) -> list[dict]:
    """Coerce evidence receipts to {kind, detail, url}; drop empties (AL-53).

    `kind` is advisory (test | url | screenshot | health | note) and falls back to `note`; a
    receipt with neither detail nor url is dropped. A `url` keeps a `commit` it names
    (GRPH-846) — the one advisory field that survives, see the branch below for why.

    **A bare string is the degenerate note, not a malformed receipt (GRPH-839).** It used to
    hit the non-dict `continue` below and vanish, while `update_item` returned the item with
    no error and the status move it was asked for — so an agent that sent
    `evidence=["fleet suite: 1072 passed", ...]` reached `review` with its proof discarded and
    nothing anywhere saying so. Since `kind` already falls back to `note`, the only thing a
    string is missing is the wrapper, and inventing it loses nothing. What could NOT be
    repaired — a number, a list, a receipt with neither detail nor url — is still refused, and
    `evidence_intake` is what makes that refusal visible instead of an array that quietly did
    not grow.

    **`sabotage`, `attestation`, and `lesson` are not advisory.** A structured kind that
    accepts unstructured input is the free-text field with a new name, and anything gating
    on it would be checking a label rather than a fact. `sabotage` needs claim / mutation /
    tests_failed (GRPH-321); `attestation` needs adapter / commit / predicates; `lesson`
    needs `shard_id` of a published shard this item's project can already search. Missing
    structure demotes to `note`, never drops.

    Graphban owns the RECEIPT, not the run. It cannot verify the mutation happened; what it
    can do is make the claim falsifiable and queryable, which is the same trade PRD-12 already
    accepts for citations.
    """
    rows, _ = normalize_evidence_report(raw)
    return rows


def _evidence_payload(raw) -> list:
    """The receipts as SENT, so `sent` and the loop below always count the same things.

    A bare string or a lone dict is ONE receipt sent unwrapped, never a sequence of them.
    Iterating a string would make a note out of every character and a dict out of every key —
    and since GRPH-839 both of those are now storable, so what used to be an obvious drop
    would have become quiet corruption. `union_touchpoints` extends the same courtesy to a
    bare path for the same reason.
    """
    if not raw:
        return []
    if isinstance(raw, (str, dict)):
        return [raw]
    return list(raw)


def normalize_evidence_report(raw) -> tuple[list[dict], list[dict]]:
    """`normalize_evidence`, and one `{index, reason}` for each receipt it refused.

    The reasons exist so a caller can be TOLD. Every writer of `evidence` runs the payload
    through here, and until GRPH-839 a refusal showed only as an array that did not grow —
    which is indistinguishable from having sent nothing, and reads as the reassuring one. The
    normalising itself is unchanged; this returns what it discarded on the way.

    `index` is into the payload as sent, so the caller can name the entry it has in hand
    rather than being handed a count and asked to guess which.
    """
    out: list[dict] = []
    dropped: list[dict] = []
    for index, e in enumerate(_evidence_payload(raw)):
        if isinstance(e, str):
            e = {"kind": "note", "detail": e}
        if not isinstance(e, dict):
            # Nothing to repair: there is no reading of `42` or `["a", "b"]` that says what
            # the agent meant, and guessing one would store a receipt it never wrote.
            dropped.append({"index": index,
                            "reason": f"{type(e).__name__} is not a receipt — send an "
                                      "object, or a string for a plain note"})
            continue
        kind = str(e.get("kind") or "note").lower()
        if kind not in _EVIDENCE_KINDS:
            kind = "note"
        detail = str(e.get("detail") or "").strip()
        url = str(e.get("url") or "").strip()
        row = {"kind": kind, "detail": detail, "url": url}
        try:
            schema_version = int(e.get("schema_version") or 1)
        except (TypeError, ValueError):
            schema_version = 1
        if kind == "sabotage":
            claim = str(e.get("claim") or "").strip()
            mutation = str(e.get("mutation") or "").strip()
            failed = e.get("tests_failed")
            if claim and mutation and isinstance(failed, int) and not isinstance(failed, bool) \
                    and failed >= 0:
                row.update({"claim": claim, "mutation": mutation, "tests_failed": failed})
                # A summary so the receipt reads as prose too — the ledger and the item view
                # render `detail`, and a sabotage that showed there as an empty string would
                # be invisible to every human surface.
                if not detail:
                    row["detail"] = (f"broke {claim!r} via {mutation!r} — {failed} test(s) failed"
                                     if failed else
                                     f"broke {claim!r} via {mutation!r} — NOTHING failed")
            else:
                # Demoted, but never DISAPPEARED. An incomplete receipt with no `detail` would
                # otherwise hit the empty-receipt drop below and vanish — the agent recorded a
                # finding, the server silently discarded it, and nothing anywhere says so.
                # Whatever it did manage to say is preserved as prose.
                row["kind"] = "note"
                if not row["detail"]:
                    said = [f"{k}={e.get(k)!r}" for k in _SABOTAGE_FIELDS if e.get(k) is not None]
                    row["detail"] = ("incomplete sabotage receipt (" + ", ".join(said) + ")"
                                     if said else "")
        elif kind == "attestation":
            adapter = str(e.get("adapter") or "").strip()
            commit = str(e.get("commit") or "").strip()
            preds = _normalize_predicates(e.get("predicates"))
            if adapter and commit and preds:
                row.update({
                    "adapter": adapter,
                    "commit": commit,
                    "predicates": preds,
                    # Opaque, deliberately. Graphban never resolves it on a critical path —
                    # that is what keeps the core independent and offline-capable.
                    "run_ref": str(e.get("run_ref") or "").strip(),
                    # GRPH-983 / GRPH-992. This row is rebuilt from a CLOSED SET, so a key the
                    # adapter sends and this does not name is silently discarded — which is
                    # what happened to `branch` for two weeks: attest_ci started sending it,
                    # the receipts on GRPH-983 for 33b193be and dc7e24a3 carry no `branch`,
                    # and the test only asserted the dict CI builds rather than what the
                    # server stores. So the fix shipped green and did nothing.
                    #
                    # `branch` is the ref CI ran on, which is how `_already_in_base` tells this
                    # item's own attestation from a sibling PR's. `matched` is how the id was
                    # found — `explicit` or `prose` — so a reader can weigh a scanned match.
                    # Empty when absent: an older receipt names neither, and an empty string is
                    # "not recorded" rather than a claim about either.
                    "branch": str(e.get("branch") or "").strip(),
                    "matched": str(e.get("matched") or "").strip(),
                    # GRPH-1411: every commit the run covered (`git rev-list base..head`).
                    # Empty for a push run, and for older receipts.
                    "range": _normalize_range(e.get("range")),
                    "schema_version": schema_version,
                })
                if not detail:
                    # "all passed" must not be printable while a predicate never ran. The
                    # summary is what a human reads, and it said "all passed" over receipts
                    # carrying two unperformed checks (GRPH-1007).
                    failed = [q["name"] for q in preds
                              if not q["passed"] and q.get("compared") is not False]
                    waived = [q["name"] for q in preds
                              if q.get("compared") is False and q.get("waived")]
                    uncompared = [q["name"] for q in preds
                                  if q.get("compared") is False and not q.get("waived")]
                    parts = []
                    if failed:
                        parts.append("FAILED: " + ", ".join(failed))
                    if uncompared:
                        parts.append("NOT COMPARED: " + ", ".join(uncompared))
                    if waived:
                        parts.append("WAIVED: " + ", ".join(waived))
                    row["detail"] = (
                        f"{adapter} attested {commit[:12]} — {len(preds)} predicate(s), "
                        + ("; ".join(parts) if parts else "all passed"))
            else:
                # Demoted, never DISAPPEARED — same reasoning as the sabotage branch above.
                row["kind"] = "note"
                if not row["detail"]:
                    said = [f"{k}={e.get(k)!r}" for k in _ATTESTATION_FIELDS
                            if e.get(k) is not None]
                    row["detail"] = ("incomplete attestation receipt (" + ", ".join(said) + ")"
                                     if said else "")
        elif kind == "test":
            # A `test` receipt may say WHAT ran as fields (GRPH-1271, PRD-63 D2): the engine,
            # whether it was the full suite, and the pytest counts. Each is kept only when it
            # validates, so a gate reading them reads a fact and never a malformed guess; the
            # receipt itself stays advisory and is never demoted for a bad field.
            row.update(_test_run_fields(e))
        elif kind == "note":
            # The one structured fact a note can carry (PRD-63 D2): `done: false` marks work
            # the builder says was NOT done. A bool and nothing else, so the observation
            # ledger counts a flag and never reads the prose beside it.
            if e.get("done") is False:
                row["done"] = False
        elif kind == "url":
            # A `url` may name the COMMIT it points at (GRPH-846). The fleet's merge receipt
            # is the reason: after `gh pr merge --squash` the reviewed commit is not an
            # ancestor of the trunk and the squash SHA is the only one that is, so a
            # dependency check that reads "every commit this item was attested at"
            # (`gbfleet.deps._commits`) held every dependant forever until the merge commit
            # was on the item too. The supervisor holds no `gate` scope, so an attestation
            # is refused to it — and would be the wrong kind anyway: nobody ran tests at the
            # squash SHA, it was merely observed to land.
            #
            # No authority escalates through this field. `attestation_receipts` reads
            # `kind`, so a url naming a commit satisfies no completion gate; the only reader
            # asks git whether the commit reaches the base, which a receipt cannot fake.
            # Kept on `url` alone: `note` and `test` rows have no thing they point at.
            commit = str(e.get("commit") or "").strip()
            if commit:
                row["commit"] = commit
        elif kind == "lesson":
            # shard_id is the structure. Visibility of the shard is checked in update_item
            # after persist (needs the item's project); missing id demotes here so a
            # structured kind never stores unstructured input.
            shard_id = str(e.get("shard_id") or "").strip()
            if shard_id:
                row["shard_id"] = shard_id
                if not detail:
                    row["detail"] = f"applied lesson {shard_id}"
            else:
                row["kind"] = "note"
                if not row["detail"]:
                    row["detail"] = "incomplete lesson receipt (no shard_id)"
        if not row["detail"] and not row["url"]:
            dropped.append({"index": index,
                            "reason": "receipt has neither detail nor url"})
            continue
        out.append(row)
    return out, dropped


def evidence_intake(raw) -> dict:
    """What the server DID with an evidence payload: sent, accepted, and why the rest were not.

    The receipt for the receipts (GRPH-839). `update_item` returns the item, and the item's
    `evidence` array shows the truth — but only to a caller that counts it against what it
    sent, and an agent that got a success back has no reason to. Stating both numbers in the
    same reply makes the mismatch impossible to miss without asking anyone to go looking.

    **`added` counts what is ON THE RECORD because of this call, not how much the array
    grew.** `append_evidence` treats an identical resend as the retry it is, so a receipt that
    was already there is added, not dropped — reporting a retry as a refusal would send an
    agent to re-run work that is already recorded.

    Report it only where evidence was actually SENT. `dropped: []` on a call that carried none
    says "nothing was discarded" when the truth is that nobody looked, which is the reading
    this whole field exists to prevent.

    One block rather than three sibling fields, because it describes THIS CALL where the rest
    of the reply describes the item — and because the manifest is measured: three declared
    properties cost 36 tokens of a surface with 12 to spare, where one opaque object costs 8
    and says the same thing at the moment it is read.
    """
    accepted, dropped = normalize_evidence_report(raw)
    return {"evidence_intake": {"sent": len(_evidence_payload(raw)),
                                "added": len(accepted), "dropped": dropped}}


def _prepare_lesson_evidence(db: Session, item: Item, old_len: int) -> list[str]:
    """Demote non-visible kind=lesson receipts on the newly appended rows.

    Gate preview uses append_evidence alone and must not write outcomes, so
    visibility lives here — after the merge, only on rows this call added.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import MemoryShard
    from app.services import memory as mem_svc

    pending: list[str] = []
    new_rows = list(item.evidence or [])[old_len:]
    dirty = False
    for row in new_rows:
        if not isinstance(row, dict) or row.get("kind") != "lesson":
            continue
        sid = row.get("shard_id")
        shard = db.get(MemoryShard, sid) if sid else None
        if shard is None or not mem_svc.lesson_visible_from(db, shard, item.project_id):
            row["kind"] = "note"
            if not row.get("detail"):
                row["detail"] = "lesson is not visible to this project"
            dirty = True
            continue
        pending.append(sid)
    if dirty:
        flag_modified(item, "evidence")
    return pending


def _record_applied_lessons(db: Session, item: Item, shard_ids: list[str]) -> None:
    """After a successful persist. Incoming well-formed lesson rows only."""
    from app.services import memory as mem_svc

    for sid in shard_ids:
        mem_svc.record_outcome(
            db, sid, kind="applied", source="evidence",
            related_item_id=item.id,
            detail=f"cited from {item.key}",
        )


def touchpoint_intake(existing, incoming) -> dict:
    """What the server DID with a touchpoints payload: sent, added, and what it KEPT (GRPH-985).

    The same receipt `evidence_intake` gives, for the same reason and a worse silence.
    `union_touchpoints` cannot remove a path — deliberately, because a write that dropped a
    declared area would read as "this item collides with nothing" — but a narrowing write is
    ACCEPTED and then ignored, with nothing in the reply to say so. `update_item` returns the
    item, so the truth is there; a caller that got a success back has no reason to diff it.

    That cost a wave. Widening two items' touchpoints to stop an `UNDECLARED` refusal collapsed
    three independent clusters into one — 34 points of effort behind a single child — and the
    narrowing write sent to undo it was accepted and discarded:

        sent:   [projecthome/, SyncLinkPanel.tsx, project-home.test.tsx, sync-link.test.tsx]
        stored: [...those four..., web/src/lib/api.ts, queries.ts, types.ts]

    No error, no warning, no diff. The item had to be re-filed as GRPH-986 to get a correct
    list, which lost its history — the same unrecoverable shape as GRPH-955's false attestation.

    `retained` is the load-bearing field: stored paths this call did NOT name. Non-empty means
    the caller tried to narrow and did not. `added` counts what this call put on the record,
    and an identical resend is a retry rather than a refusal, exactly as in `evidence_intake`.

    Report it only where touchpoints were actually SENT. `retained: []` on a call that carried
    none would say "nothing was kept back" when the truth is that nobody looked.
    """
    incoming = [incoming] if isinstance(incoming, str) else list(incoming or [])
    sent = [q.strip() for q in incoming if isinstance(q, str) and q.strip()]
    before = [q for q in (existing or []) if isinstance(q, str) and q.strip()]
    after = union_touchpoints(existing, incoming)
    return {"touchpoint_intake": {
        "sent": len(sent),
        "added": len([q for q in after if q not in before]),
        "retained": [q for q in before if q not in sent],
    }}


def remove_touchpoints(db: Session, item: Item, remove, *, by: str | None,
                       planner: bool = False) -> list[str]:
    """Take paths off an item's touchpoints (GRPH-1398). Returns the paths actually removed.

    `touchpoints` only ever unions, so a broad glob filed to unblock a PR stayed on the item
    and made every later claim collide with unrelated work. Removal is its own, explicit
    field so the safe default is unchanged: a plain write still cannot shrink the list.

    Refused while the item is `in_progress` and held by ANOTHER agent (a planner may), so a
    live claim's reservations cannot shrink under it. Recorded as a `note` receipt, and any
    area reservation on a removed path is released. Does not commit.
    """
    if isinstance(remove, str):
        remove = [remove]
    wanted = [q.strip() for q in (remove or []) if isinstance(q, str) and q.strip()]
    if not wanted:
        return []
    holder = item.claimed_by
    if (item.status == "in_progress" and holder and not planner
            and by and holder != by):
        raise ValueError(
            f"{item.key} is in_progress and held by {holder}; only the holder or a planner "
            "may remove its touchpoints"
        )
    stored = list(item.touchpoints or [])
    removed = [q for q in dict.fromkeys(wanted) if q in stored]
    if not removed:
        return []
    item.touchpoints = [q for q in stored if q not in removed]
    from app.models import AreaReservation
    db.execute(delete(AreaReservation).where(
        AreaReservation.item_id == item.id, AreaReservation.area.in_(removed)))
    item.evidence = append_evidence(item.evidence, [{
        "kind": "note",
        "detail": f"touchpoints removed by {by or 'unknown'}: {', '.join(removed)}",
    }])
    return removed


def union_touchpoints(existing, incoming) -> list[str]:
    """Add measured paths without removing declared ones (GRPH-611 / P30 D10).

    `update_item(touchpoints=[...])` used to assign the incoming list straight over the
    stored one. An empty write then read as "this item collides with nothing", which is
    the absence that looks like a clean partition — predicted/declared paths gone, the
    next `claim_cluster` treating the item as unconstrained. The client sends **this
    reap's measured paths only**; the server keeps what was already there.

    Same reason as `append_evidence`: there is no way to remove a path through this
    verb. A wrong one is corrected by adding the real one, which leaves both readable.
    An identical path is a retry, not a second entry.

    Empty incoming is a no-op on the list itself — callers should skip the write, and
    `update_item` does.
    """
    if isinstance(incoming, str):
        incoming = [incoming]
    out: list[str] = []
    seen: set[str] = set()
    for path in list(existing or []) + list(incoming or []):
        if not isinstance(path, str):
            continue
        cleaned = path.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def append_evidence(existing, incoming) -> list[dict]:
    """Add receipts to an item's record without removing any already there (GRPH-494).

    **The record only grows.** `update_item(evidence=[...])` used to assign the incoming list
    straight over the stored one, while `sign_off` and the superseded-intent receipt both
    appended — one field, three writers, two meanings. The destructive one was the widest:
    every agent has it. Recording an independent review verdict on GRPH-487 silently deleted
    the author's test summary and 7-mutation sabotage receipt, and the item still read as
    fully evidenced afterwards, because a populated array says nothing about what used to be
    in it.

    That is not merely a lost note. `fleet.sign_off` gates on `has_effective_sabotage` over
    the STORED array, so deleting a builder's receipts can leave an item unsignable by its
    own proof — and asks them to re-run sabotages to replace evidence nobody can see was
    removed. There is no audit trail: `evidence` has no history.

    So there is no way to remove a receipt through this path, deliberately. A wrong one is
    corrected by adding a correcting receipt, which is what this repo already does in prose
    (the GRPH-437 attribution notes, GRPH-340 keeping superseded counts in view) and which
    leaves both the error and the correction readable. The acceptance gate honours that
    correction: the latest block per clause is the verdict, so a later `{kind: test}`
    that names the clause and does not deny it supersedes an earlier denial without
    anyone deleting the line. A denial on the next line of that same receipt, naming
    no other clause, is still a denial (GRPH-1054). Deleting
    a receipt is still not this path. If a genuine need to delete ever turns up, it wants
    an explicit destructive operation that says so — not the default behaviour of the verb
    every agent calls to add a note.

    **An identical receipt is a retry, not a second receipt.** `update_item` has no
    idempotency key, so a call that times out after committing gets sent again; appending
    blindly would double every receipt on a flaky link. Equality is on the normalised dict,
    so two genuinely different receipts of the same kind both survive.
    """
    out = list(existing or [])
    for row in normalize_evidence(incoming):
        if row not in out:
            out.append(row)
    return out


def sabotage_receipts(evidence) -> list[dict]:
    """Every well-formed sabotage receipt on an item."""
    return [e for e in (evidence or [])
            if isinstance(e, dict) and e.get("kind") == "sabotage"]


def vacuous_sabotages(evidence) -> list[dict]:
    """Sabotages where the mutation broke NOTHING.

    **These are findings, not failures to record.** A mutation that removes the behaviour and
    leaves every test green has proved the test cannot fail — which is more valuable than a
    passing sabotage and must never be mistaken for one. It happened twice in the session that
    motivated this item: once because the mutation string did not match the real source, and
    once because the test was pointed at a seam adjacent to the claim it named.
    """
    return [e for e in sabotage_receipts(evidence) if not e.get("tests_failed")]


def has_effective_sabotage(evidence) -> bool:
    """Whether any claim on this item was broken on purpose and something failed.

    The question a gate asks. `tests_failed >= 1` is the whole of it: a sabotage nothing
    failed under is evidence the guard is absent, so counting it would let exactly the
    condition it detects satisfy the check that exists to detect it.

    A probe attestation with `sabotage_observed` passed is the same measurement from a
    different adapter — the observation, not a self-report (GRPH-623). `attested_predicates`
    reads only from `valid_attestations`, so a name appearing here passed on a sound receipt
    (every predicate on it true); a failing probe cannot satisfy this.
    """
    if any(e.get("tests_failed") for e in sabotage_receipts(evidence)):
        return True
    return "sabotage_observed" in attested_predicates(evidence)


def _normalize_range(value) -> list[str]:
    """The commits a CI receipt covered: a list of hex strings, deduplicated, else []."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for c in value:
        c = str(c or "").strip().lower()
        if _COMMIT.match(c) and c not in out:
            out.append(c)
    return out


#: GRPH-1416. The predicate CI's run-start marker carries (scripts/attest_ci.py `--mode started`).
CI_RUNNING_PREDICATE = "ci_in_progress"


def _is_run_marker(e: dict) -> bool:
    """A run-start announcement: every predicate on it is `ci_in_progress`.

    Stored as `kind: attestation` because CI writes with the `gate` scope, but it is NOT a
    verdict and must never be read as one. Its predicate is `passed: True`, so a reader that
    saw it would take "a run began" for "the work was checked": the gate would pass a PR
    whose CI is still running, or has since gone red (the GRPH-1416 bounce).
    """
    names = [q.get("name") for q in (e.get("predicates") or []) if isinstance(q, dict)]
    return bool(names) and all(n == CI_RUNNING_PREDICATE for n in names)


def attestation_receipts(evidence) -> list[dict]:
    """Every well-formed attestation on an item (GRPH-542).

    Run-start markers (GRPH-1416) are excluded here, so no verdict reader — the completion
    gate, required predicates, head staleness, sign-off, metrics — ever sees one. They
    announce a run; they attest nothing. `_ci_run_records` is the one reader that wants them.
    """
    return [e for e in (evidence or [])
            if isinstance(e, dict) and e.get("kind") == "attestation"
            and not _is_run_marker(e)]


def _ci_run_records(evidence) -> list[dict]:
    """Attestations INCLUDING run-start markers, in evidence order (for `ci_run_in_progress`)."""
    return [e for e in (evidence or [])
            if isinstance(e, dict) and e.get("kind") == "attestation"]


WORKTREE_ADAPTER = "fleet.worktree"

#: The owner-close adapter (GRPH-1030). Distinct from `fleet.sign_off` on purpose: a
#: signed-in project owner closing hand-merged work is not a reviewer, and a receipt
#: that reused the sign-off adapter would impersonate one. Metrics that key on
#: `reviewed_by` / `fleet.sign_off` therefore do not count it.
OWNER_CLOSE_ADAPTER = "owner.close"

#: A merge commit the owner names. Same floor as `same_commit` — shorter than 7 hex
#: chars is a collision, not an identity.
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

#: How a supervisor says which commit it cut a branch from, in a `note`'s prose (GRPH-970).
#:
#: A note, and not the attestation it wants to be, because writing `kind: attestation`
#: needs the `gate` scope (mcp_server, GRPH-544) and a supervisor runs on whatever
#: `GBFLEET_API_KEY` holds — `["read", "write"]` on every wave measured. An attestation
#: receipt would have been REFUSED, the supervisor's `except` would have logged a wave
#: failure nobody reads, and the gate below would have found no base and reported "not
#: compared" for ever: a guard that does nothing, which is the defect it exists to fix.
#:
#: Prose is a worse carrier than a field and is used anyway, because `normalize_evidence`
#: keeps only `kind`/`detail`/`url` on a note and inventing a field for this would change
#: the evidence schema for one caller. It is written in exactly one place
#: (`fleet.record_observation`, the supervisor observations route — GRPH-975) and read in
#: exactly one place (below), and the pair is pinned by test_fleet_base_commit_gate.
#: The supervisor posts the facts; it does not compose this sentence, because its client
#: may not call `update_item`.
CUT_FROM_MARKER = "gbfleet: branch cut from "

_SHA = re.compile(r"\b([0-9a-f]{7,40})\b")


def cut_from_commits(evidence) -> list[str]:
    """Every commit a supervisor recorded as the base a branch was CUT FROM (GRPH-970).

    The one fact about a child that the child is the worst possible source for, so it comes
    from the supervisor that cut the tree. Two carriers are read: the `fleet.worktree`
    attestation a `gate`-scoped supervisor can post, and the `CUT_FROM_MARKER` note any
    supervisor can. Neither is a verdict — the attestation form carries no predicates, so
    `valid_attestations` will not let it satisfy completion.

    Measured on the p47-ui wave, 2026-09-27: GRPH-954 reached `done` on a `fleet.sign_off`
    naming the base commit of its own branch, with three fabricated sabotage receipts and no
    commit for the item anywhere on any ref. Every predicate passed. This is the fact that
    was missing.

    Why evidence rather than a column: evidence APPENDS and never removes
    (`append_evidence`), so a child cannot delete the receipt that convicts it. A child CAN
    add one, and that buys it nothing — another recorded base only narrows which commits it
    may attest, and it can never remove the real one.
    """
    out = []
    for e in (evidence or []):
        if not isinstance(e, dict):
            continue
        if e.get("kind") == "attestation" and e.get("adapter") == WORKTREE_ADAPTER:
            sha = str(e.get("commit") or "").strip()
            if sha:
                out.append(sha)
            continue
        detail = str(e.get("detail") or "")
        if CUT_FROM_MARKER in detail:
            found = _SHA.search(detail[detail.index(CUT_FROM_MARKER) + len(CUT_FROM_MARKER):])
            if found:
                out.append(found.group(1))
    return out


#: How a supervisor says a child committed on the branch it cut (GRPH-1116).
#:
#: A git commit does not touch the item row, so `updated_at <= claimed_at` still
#: reads "wrote nothing" after the sha landed. `release_item` would then clear
#: `built_by`, and the next agent to move the item to review — often the planner —
#: would be stamped as the author. The supervisor may not call `update_item`, so the
#: fact rides this note, written by `fleet.record_observation` and read only here.
COMMITTED_MARKER = "gbfleet: committed "


def committed_by(evidence, agent_id: str) -> bool:
    """Whether `agent_id` is the agent named on a supervisor commit note (GRPH-1116).

    The sentence is "gbfleet: committed <sha> on `<branch>` by <agent>". A note that
    names somebody else does not keep this holder's authorship: the agent who is
    leaving is the one the sentence has to name. A marker with no sha is not a commit.
    """
    who = (agent_id or "").strip()
    if not who:
        return False
    for e in (evidence or []):
        if not isinstance(e, dict):
            continue
        detail = str(e.get("detail") or "")
        if COMMITTED_MARKER not in detail:
            continue
        named = detail.rsplit(" by ", 1)[-1].strip() if " by " in detail else ""
        if named != who:
            continue
        tail = detail[detail.index(COMMITTED_MARKER) + len(COMMITTED_MARKER):]
        if _SHA.search(tail):
            return True
    return False


def same_commit(a: str | None, b: str | None) -> bool:
    """Do two revisions name the same commit, allowing for abbreviation?

    Git abbreviates, and the two sides of this comparison come from different places: the
    supervisor records what `rev-parse` gave it, a reviewer types or pastes whatever it has.
    Comparing full strings would let `7ea90ebe` slip past a recorded `7ea90ebebe2b...` — the
    exact pair from the wave this guard was written for. Prefix comparison in the shorter
    direction, with a floor, because a 4-character "match" is a collision, not an identity.
    """
    x = (a or "").strip().lower()
    y = (b or "").strip().lower()
    if len(x) < 7 or len(y) < 7:
        return bool(x) and x == y
    return x.startswith(y) or y.startswith(x)


def _predicate_admits(q) -> bool:
    """Whether one predicate lets its attestation through the completion gate.

    `passed is True` is the ordinary answer, and `passed` must be a real bool: a JSON client
    sending `"false"` would otherwise store a truthy string (GRPH-542).

    The exception is a predicate whose check COULD NOT RUN — no base recorded, no `built_by`.
    Those report `passed: False, compared: False` rather than claiming a comparison nobody
    made (GRPH-1007), which would otherwise block a hand-built item forever. A reviewer may
    record a `waived` reason to admit one, and only one of those: a predicate that ran and
    FAILED (`compared: True, passed: False`) is a real refusal and no waiver reaches it.
    """
    if not isinstance(q, dict):
        return False
    if q.get("passed") is True:
        return True
    return q.get("compared") is False and bool(str(q.get("waived") or "").strip())


def valid_attestations(evidence, *, commit: str | None = None) -> list[dict]:
    """Attestations that actually attest something: at least one predicate, all passed.

    **The empty-predicate check is the load-bearing half.** `all([])` is True, so an
    attestation carrying `predicates: []` would satisfy "nothing failed" vacuously and the
    completion gate would pass an item nobody checked. That is GRPH-466's shape exactly — a
    guard that held because it had nothing to hold against — and it is the failure this
    receipt exists to prevent, so it must not be reintroduced by the receipt itself.

    `commit` narrows to attestations bound to one revision. Passing None asks the weaker
    question "was this ever attested" rather than "is this attested AS IT STANDS"; see
    `has_valid_attestation` for why the caller has to supply it.
    """
    out = []
    for a in attestation_receipts(evidence):
        preds = a.get("predicates") or []
        # `passed` must be a real bool here too, not only at normalize. Stored rows
        # predate that check (and other writers skip it); `all(q.get("passed"))` is
        # True for the JSON-client string `"false"` — the exact case GRPH-542 exists
        # to stop, and the empty-predicate stored-row hole wearing different clothes.
        if not preds or not all(_predicate_admits(q) for q in preds):
            continue
        if commit is not None and a.get("commit") != commit:
            continue
        out.append(a)
    return out


def has_valid_attestation(evidence, *, commit: str | None = None) -> bool:
    """The question the completion gate asks (GRPH-543).

    Graphban cannot observe the working tree, so it cannot tell on its own whether an
    attestation is CURRENT — only whether one exists. Staleness is therefore the caller's
    to assert, by naming the commit it is completing at. That is a real limit and it is
    stated here rather than in a comment on the gate, because this is the function anyone
    reaching for the guarantee will read first.
    """
    return bool(valid_attestations(evidence, commit=commit))


def attested_predicates(evidence, *, commit: str | None = None) -> set[str]:
    """Every predicate name that PASSED on a valid attestation (GRPH-569).

    Unioned ACROSS attestations, because adapters are separate and each answers what it can:
    CI attests `suite_green`, a reviewer attests `conformance`, a probe attests
    `sabotage_observed`. Requiring one adapter to carry them all would mean an install could
    only ever require what its most capable adapter emits.

    **`valid_attestations` is what makes a failing predicate not count, and it is the only
    thing that does.** It admits a receipt only when EVERY predicate on it passed, so a name
    riding on an attestation with another failing predicate has not been attested, it has
    been contradicted — and by the time the names are read here, no failing one can remain.

    There is deliberately no `if q.get("passed")` filter below. It reads like a safety net and
    is dead code against that precondition: it can never exclude anything, so it would pass
    every test while carrying none of the property, and the first person to change
    `valid_attestations` would trust a guard that had never once fired. The mechanism is
    named here instead, and `test_predicates_are_read_only_from_sound_receipts` sabotages the
    real one.
    """
    # PASSED, not merely present. A waived predicate rides on an admitted receipt
    # (GRPH-1007) and its name would otherwise satisfy `missing_predicates`, so a reason
    # string would stand in for a check that never ran — the laundering path the waiver is
    # explicitly not meant to open. A waiver admits ONE item past completion; it does not
    # make a required guarantee true for anything that asks whether it holds.
    return {str(q.get("name") or "")
            for a in valid_attestations(evidence, commit=commit)
            for q in (a.get("predicates") or [])
            if q.get("passed") is True} - {""}


def missing_predicates(evidence, required, *, commit: str | None = None) -> list[str]:
    """Required predicate names that nothing has attested, in a stable order.

    **Absent is not failing, and the two call for opposite actions.** A missing predicate
    means nothing ran that checks it — go and run the adapter. A failing one means something
    ran and found a problem — go and read it. The completion gate reports them separately for
    that reason; collapsing them would send half its readers to the wrong place, which is the
    defect GRPH-543 already fixed once for stale-versus-missing.

    This helper answers only the absent half. `failing_predicates` answers the other; the
    CALL in `update_item` splits the two so a required name that ran and failed is not
    reported as never run (GRPH-569 bounce).
    """
    return sorted(set(required or []) - attested_predicates(evidence, commit=commit))


def failing_predicates(evidence, *, commit: str | None = None) -> set[str]:
    """Predicate names that something RAN and reported as failing (GRPH-569).

    Distinct from `missing_predicates`: those are names nobody attested as passing. A
    name can sit in both — required, never passed, but DID fail — and that is a third
    answer, not "nobody looked". `valid_attestations` drops the whole receipt, so
    reading names only from there would make every failure look like an absence.

    `commit` narrows the same way the rest of the gate does: a failure at another
    revision is not a failure of the head being completed.
    """
    names: set[str] = set()
    for a in attestation_receipts(evidence):
        if commit is not None and a.get("commit") != commit:
            continue
        for q in a.get("predicates") or []:
            if not isinstance(q, dict):
                continue
            name = str(q.get("name") or "")
            if name and q.get("passed") is False:
                names.add(name)
    return names


#: Refusal kinds that are an expected wait rather than a lesson: the evidence note is
#: written, a memory shard is not. `pr_cooldown` resolves itself, and a shard saying "the
#: cooldown had 18s left" would ride every later brief as a lesson (GRPH-1234). Every other
#: kind (`attestation_missing`, the failed-predicate refusals) names something to change, so
#: it still writes.
_NO_SHARD_REFUSALS = frozenset({"pr_cooldown"})


def record_refusal(db: Session, item: Item, *, predicate: str, detail: str) -> None:
    """Write a gate refusal where the next agent will meet it (GRPH-550).

    A gate that blocks without teaching gets routed around, and a gate that is routinely
    bypassed is worse than none — it reads as protection while protecting nothing. So a
    refusal lands twice: as evidence on the item, and as a memory shard that
    `search_memory` will surface to whoever plans similar work next.

    **At refusal time, not on completion.** The first design routed this through
    `extract_lessons`, which fires on `done` — so a refused item never completed and the
    loop had no mechanism at all. Refusals must compound even for items that are abandoned
    or never finish, because those teach the most: an item refused and then dropped is
    exactly the case nobody writes up.

    **Committed deliberately.** The caller raises immediately after this returns, and an
    exception unwinds the session — so a refusal recorded without committing would vanish
    with the failure it describes, which is the same defect one level down.

    **PUBLISHED, not a candidate**, which is the one decision here worth arguing. The
    trusted-publication boundary (AL-49) keeps UNREVIEWED AGENT SELF-REPORTS out of
    retrieval, and `search` honours it by returning published shards only. A gate refusal is
    not a self-report: the server refused, and it knows exactly why. Provenance is the
    system rather than the agent, which is what that boundary sorts on. Filed as a candidate
    it would sit in the triage queue, never surface, and the compounding this exists for
    would not happen. `origin` marks it so these stay filterable and auditable.
    """
    from sqlalchemy import select as _select

    from app.embeddings import safe_embed
    from app.models import MemoryShard
    from app.services import memory as memory_svc

    receipt = {"kind": "note",
               "detail": f"refused at `done` ({predicate}): {detail}"}
    item.evidence = append_evidence(item.evidence, [receipt])
    if predicate in _NO_SHARD_REFUSALS:
        db.commit()
        return

    # Keyed on (item, predicate) — NOT on the commit. A non-functional refactor changes the
    # SHA, so including it would re-emit the same unresolved refusal as a fresh shard and
    # produce exactly the flooding this rule exists to prevent.
    source = f"gate refusal: {predicate}"
    text = (
        f"{item.key} was refused at `done` — {detail} "
        f"Completion needs an `attestation` receipt on the item, and only a key carrying the "
        f"`gate` scope may write one: a reviewer signing off with a commit, or CI. Recording "
        f"`test` or `sabotage` receipts is proof of work but does not open the gate."
    )
    existing = db.scalar(_select(MemoryShard).where(
        MemoryShard.item_id == item.id, MemoryShard.source == source))
    if existing is None:
        memory_svc.add_memory(
            db, text_body=text, scope="item", source=source, item_id=item.id,
            project_id=item.project_id, status="published", origin="gate",
            auto_triage=False)
    elif existing.text != text:
        # Updated in place rather than added beside. Two shards saying almost the same thing
        # is the noise the dedupe key exists to avoid, and re-embedding keeps the vector
        # honest about the text it now holds.
        existing.text = text
        existing.embedding = safe_embed(text)
    # `add_memory` commits on the create path, so this is load-bearing only on the UPDATE
    # one — where the text changed because a different predicate started failing. Kept
    # explicit rather than relying on that: a reader should not have to know which branch
    # another service happens to commit in, and the failure if it changed would be a
    # refusal that quietly stopped persisting.
    db.commit()


class MissingAttestation(Exception):
    """`done` was asked for on an item nothing has attested (GRPH-543).

    Its own class rather than a ValueError so the boundaries can map it to a `conflict` —
    the state is wrong, not the request — exactly as `MissingAdversarialEvidence` is.
    """


class EvidenceRequired(Exception):
    """The `build.engine_evidence` library rule is on and this item, moving to review, lacks the
    evidence its class needs (GRPH-1271). A 409 at the boundaries, naming the rule."""


class PRCooldown(Exception):
    """`done` was asked for too soon after the PR was linked (GRPH-567).

    Its own class for the same reason as `MissingAttestation`, and mapped the same way: the
    caller is permitted and the request is well formed — the outcome simply cannot be known
    yet. It is also the one refusal in this file that FIXES ITSELF, so the message says when
    rather than what to change.
    """


class PRCINotGreen(PRCooldown):
    """The cooldown is over, or CI has already answered, and `suite_green` is not attested at
    the PR's head (GRPH-1345). A PRCooldown so every caller that already refuses `done` on a
    cooldown still refuses on this, but unlike its parent it does NOT clear itself: waiting
    longer changes nothing. A boundary that hints "wait and retry" for PRCooldown must catch
    this subclass first and name the action instead (see mcp_server)."""


class NoPRForBranch(PRCINotGreen):
    """`done` was asked for an item whose work is on a branch and has no PR (GRPH-1457).

    A PRCINotGreen so every caller that refuses on a PR gate refuses here too, and with the
    same "this does not clear itself" reading: the fix is a PR, not more waiting.
    """


#: A link to a proposed change, as opposed to any other URL an item might carry.
#:
#: GitHub and GitLab spell it differently and both are matched; anything else is not a PR
#: and must not start a cooldown. Deliberately NOT a general "is this a code host" guess —
#: an unrecognised forge stays outside the gate rather than being delayed by a pattern that
#: happened to match, because a cooldown nobody can explain is worse than none.
_PR_URL_MARKERS = ("/pull/", "/pull-requests/", "/merge_requests/")


def is_pr_url(url: str) -> bool:
    """Does this URL point at a pull/merge request?"""
    return any(marker in (url or "").lower() for marker in _PR_URL_MARKERS)


ENGINE_EVIDENCE_RULE = "build.engine_evidence"
# The prose fallback, for `test` receipts written before the structured fields existed. A skip
# count is a pytest summary ("6402 passed, 38 skipped"), not a partial run; a file count is
# partial only in the "on N files" form ("120 test files, all passed" is a full run described).
_PARTIAL_RUN = re.compile(
    r"\bonly\b|\bpartial(?:ly)?\b|\bsubset\b|\bselected\b|\bsmoke\b|\bnot (?:run|ran|executed)\b"
    r"|\bon \d+ (?:test )?(?:files?|modules?)\b|\s-k\s|\b[1-9]\d* (?:failed|errors?)\b", re.I)
_FULL_RUN = re.compile(r"\b(?:full|entire|whole|complete)\b|\ball (?:tests|files)\b", re.I)


def _full_engine_run(receipts, engine: str) -> bool:
    """Whether a `test` receipt records a FULL, green run on `engine`.

    Read from the structured fields (PRD-63 D2: free text never decides): `engine` names it,
    `scope` is `full`, and `failed` is recorded as 0. A skip count never makes a run partial.
    A receipt carrying none of the fields predates them and falls back to its prose, where a
    partial run ("Postgres on 4 files only"), a denial ("Postgres NOT run"), a failure count or
    a line that never says it was the whole suite does not count: absence never reads as clean.
    """
    from app.services import fleet as fleet_svc

    for e in receipts or []:
        if not isinstance(e, dict) or e.get("kind") != "test":
            continue
        if any(k in e for k in ("engine", "scope", *_TEST_COUNTS)):
            if (e.get("engine") == engine and e.get("scope") == "full"
                    and e.get("failed") == 0 and not isinstance(e.get("failed"), bool)):
                return True
            continue
        line = str(e.get("detail") or "")
        if (engine in line.lower() and _FULL_RUN.search(line) and not _PARTIAL_RUN.search(line)
                and not fleet_svc.denial_in(engine, line)):
            return True
    return False


def require_engine_evidence(db: Session, item: Item, incoming, head: str | None = None) -> None:
    """The `evidence_required` primitive, bound to `build.engine_evidence` (GRPH-1271).

    Read through the layered resolution (`rule_toggles.resolve`), never a constant: a rule absent
    from the loaded library, or off, is no gate. When on, an item of the class (a touchpoint
    containing `path`, default `alembic/versions/`) needs a test receipt recording a full run on
    `engine` (default `postgres`) before it moves to review. Other items are unaffected.

    **The project's test-scope mode decides who may supply the run (GRPH-1404).** In
    `agent_targeted` CI runs the full suites, Postgres job included (the `ci` gate job needs
    it), so a PASSED `suite_green` attestation at the item's CURRENT head satisfies the gate;
    a local full receipt still does too. An attestation at another commit, or a failing one,
    counts for nothing, and with no head named there is nothing to bind it to. The other
    modes keep the local full receipt.
    """
    from app.services import rule_toggles

    if ENGINE_EVIDENCE_RULE not in rule_toggles.LIBRARY:
        return
    state = rule_toggles.resolve(db, ENGINE_EVIDENCE_RULE, item.project_id)
    if not state["on"]["value"]:
        return
    params = state["params"]["value"] or {}
    path = params.get("path") or "alembic/versions/"
    engine = (params.get("engine") or "postgres").lower()
    if not any(path in (t or "") for t in item.touchpoints or []):
        return
    merged = append_evidence(item.evidence, incoming or [])
    if _full_engine_run(merged, engine):
        return
    waiting = ""
    if rule_toggles.test_scope_mode(db, item.project_id) == "agent_targeted":
        at = (head or item.head_commit or "").strip()
        if at and CI_PREDICATE in attested_predicates(merged, commit=at):
            return
        waiting = (f" The project's test scope is agent_targeted, so this item is waiting for "
                   f"CI's {engine} run: {CI_PREDICATE} must be attested at its current head "
                   + (f"{at[:12]}" if at else "(none is recorded; push and let CI report)")
                   + ", and an attestation at another commit or a failing one does not count.")
    if state.get("state") == "observing":
        # GRPH-1777: observing records what it would have refused, and lets the item through.
        from app.services import rule_observations
        rule_observations.record(db, item.project_id, ENGINE_EVIDENCE_RULE, "would_refuse",
                                 item_id=item.id, commit=(head or item.head_commit),
                                 detail={"path": path, "engine": engine})
        return
    raise EvidenceRequired(
        f"{item.key} cannot move to review while {ENGINE_EVIDENCE_RULE} is on: it touches "
        f"{path} and carries no test receipt recording a full {engine} suite run.{waiting} Record one "
        f"as {{kind: test, engine: {engine!r}, scope: 'full', passed, failed: 0, skipped}}; a "
        "partial run, or one with failures, does not count")


def reviewable_handoff(item: Item, incoming_evidence=None) -> bool:
    """Can a reviewer reach this item's work? (GRPH-946)

    A recorded `branch`, or a PR url in its evidence (stored, or arriving in this call). An
    item with neither went to review on SA-575 before its branch was pushed; the reviewer
    bounced it for "no remote branch" and the real defects went unreviewed.
    """
    if (item.branch or "").strip():
        return True
    rows = list(item.evidence or []) + list(incoming_evidence or [])
    return any(is_pr_url(e.get("url", "")) for e in rows if isinstance(e, dict))


def _submitter_branch(db: Session, item: Item, submitted_by: str) -> str:
    """The branch the calling agent registered, when the item has none of its own yet."""
    if (item.branch or "").strip():
        return ""
    agent = db.get(Agent, submitted_by)
    return ((agent.branch if agent is not None else "") or "").strip()


def _effective_review_branch(db: Session, item: Item, submitted_by: str, fields: dict) -> str:
    """The branch a move to review would hand a reviewer.

    The incoming `branch` wins, then the item's own, then the submitter's registered
    branch. That last one is stamped onto the row later in this same call, so the
    gate has to read it now. An empty string is not a write.
    """
    incoming = fields.get("branch")
    if isinstance(incoming, str) and incoming.strip():
        return incoming.strip()
    own = (item.branch or "").strip()
    if own:
        return own
    return _submitter_branch(db, item, submitted_by)


def pr_linked_at(evidence, existing=None):
    """When this item's PR was FIRST linked, or None.

    `existing` wins whenever it is set. Re-posting the same URL — or posting a second one —
    must not restart the clock, because the cooldown would then be escaped by the cheapest
    available action, and an agent that wanted to escape it would not have to know it exists.
    """
    if existing is not None:
        return existing
    if any(is_pr_url(e.get("url", "")) for e in evidence or [] if isinstance(e, dict)):
        return utcnow()
    return None


#: The predicate that says CI ran and passed. Its presence at the PR's head or merge commit
#: is the thing the cooldown was waiting for, so once it is there the wait is over.
CI_PREDICATE = "suite_green"


def pr_commits(evidence, *named) -> list[str]:
    """The commits this item's PR is known by, most specific first (GRPH-947).

    `named` is what the caller says it is completing at — a reported head, or the SHA a
    reviewer passes to sign_off — and comes first. After that, any commit a PR `url` receipt
    names: the fleet's merge receipt carries the squash SHA there (GRPH-846), and that is the
    merge commit CI attests after the PR lands. Empty means nobody named one, which is not
    the same as "nothing to wait for" — see `refuse_if_pr_cooling_down`.
    """
    out: list[str] = []
    for c in named:
        c = (c or "").strip()
        if c and c not in out:
            out.append(c)
    for e in evidence or []:
        if not isinstance(e, dict) or e.get("kind") != "url" or not is_pr_url(e.get("url", "")):
            continue
        c = str(e.get("commit") or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _range_receipts(db, item, commits) -> list[dict]:
    """CI receipts on OTHER items of this project whose `range` lists one of `commits`
    (GRPH-1411). Each is a copy tagged `_carried_by` with the carrying item's key.

    A PR run attests its head onto the items its text names, but vouches for every commit in
    the PR; work merged inside another item's PR has no receipt of its own. The server has no
    git, so the range CI recorded is the only way to tell that head H contains commit c.
    """
    wanted = [c for c in commits if c]
    if not wanted:
        return []
    # Every item in the project, read only on a gated `done` whose item has no CI answer at
    # its own commit — the rare path, so a scan is acceptable. Narrow it (an index of range
    # commits) if that path ever becomes common.
    rows = db.execute(select(Item).where(Item.project_id == item.project_id,
                                         Item.id != item.id)).scalars().all()
    out: list[tuple[tuple, dict]] = []
    for other in rows:
        for pos, a in enumerate(attestation_receipts(other.evidence)):
            if not any(isinstance(q, dict) and q.get("name") == CI_PREDICATE
                       for q in a.get("predicates") or []):
                continue
            covered = list(a.get("range") or [])
            if not any(same_commit(r, c) for r in covered for c in wanted):
                continue
            out.append((_run_order(a, pos), {**a, "_carried_by": other.key}))
    # Oldest first, so the caller's `[-1]` is the latest run.
    return [a for _, a in sorted(out, key=lambda t: t[0])]


#: `.../actions/runs/<id>` and optional `/attempts/<n>` in a receipt's `run_ref`.
_RUN_REF = re.compile(r"/runs/(\d+)(?:/attempts/(\d+))?")


def _run_order(receipt: dict, pos: int) -> tuple:
    """Sort key for CI runs across items: GitHub's run id (monotonic), then the attempt, then
    the receipt's place on its item. Receipts carry no timestamp, and a re-run keeps its run
    id and raises its attempt, so this is the order the runs happened in."""
    m = _RUN_REF.search(str(receipt.get("run_ref") or ""))
    if not m:
        return (0, 0, pos)
    return (int(m.group(1)), int(m.group(2) or 0), pos)


def _covered_here(receipt: dict, commits) -> str:
    """The first of `commits` that `receipt`'s range lists, shortened for a message."""
    for c in commits:
        if c and any(same_commit(r, c) for r in receipt.get("range") or []):
            return c[:12]
    return "?"


def _carried_note(receipt: dict, commits) -> dict:
    """The receipt that says WHY a carried-green item was let through (GRPH-1411): the run,
    its head, and the item that holds the attestation. A note, never an attestation — the
    pass is the carrying item's; this records only that the gate relied on it."""
    run = receipt.get("run_ref") or "an unnamed run"
    return {
        "kind": "note",
        "detail": (f"pr_ci_green carried: {CI_PREDICATE} by {receipt.get('adapter')} run "
                   f"{run} at head {str(receipt.get('commit'))[:12]}, attested on "
                   f"{receipt.get('_carried_by')}, whose PR range covered "
                   f"{_covered_here(receipt, commits)}"),
    }


#: GRPH-1619. An item whose deliverable lives in ANOTHER repository declares it with a tag
#: `repo:<host/owner/name>` (or `repo:<owner/name>`, read as GitHub). That repo's CI does not
#: report to Graphban, so no `suite_green` can ever arrive for its head; a reviewer cites the
#: external run instead, and `fleet.sign_off` records it under this adapter and predicate —
#: never as `suite_green`, which stays this repo's CI's word alone.
REPO_TAG_PREFIX = "repo:"
EXTERNAL_CI_ADAPTER = "fleet.sign_off.external_ci"
EXTERNAL_CI_PREDICATE = "external_ci_green"


def declared_repo(item) -> str | None:
    """The repo an item's `repo:` tag names, normalized (`github.com/owner/name`), or None.

    A tag that does not read as a repo is not a declaration: the gate falls back to the
    ordinary in-repo rules rather than guessing what was meant."""
    from app.services.projects import normalize_repo_url

    for t in item.tags or []:
        t = str(t or "").strip()
        if not t.lower().startswith(REPO_TAG_PREFIX):
            continue
        raw = t[len(REPO_TAG_PREFIX):].strip()
        if raw.count("/") == 1 and "://" not in raw and ":" not in raw:
            raw = f"github.com/{raw}"
        try:
            return normalize_repo_url(raw)
        except ValueError:
            return None
    return None


def external_repo(db, item) -> str | None:
    """The OTHER repo this item's deliverable lives in, or None for an in-repo item.

    None when nothing is declared, and None when the declared repo IS this project's own
    `repo_url`: that repo's CI reports here, so `suite_green` is the only proof it gets."""
    repo = declared_repo(item)
    if repo is None:
        return None
    project = db.get(Project, item.project_id)
    own = (getattr(project, "repo_url", None) or "").strip().lower() if project else ""
    if own and own == repo:
        return None
    return repo


def external_ci_green(db, item, evidence, commits) -> dict | None:
    """The reviewer-cited external CI receipt at one of `commits`, for an external item only.

    Read only through `external_repo`: an in-repo item carrying such a receipt (a tag
    removed after sign_off, a hand-posted attestation) is NOT let through on it."""
    repo = external_repo(db, item)
    if repo is None:
        return None
    for a in valid_attestations(evidence):
        if a.get("adapter") != EXTERNAL_CI_ADAPTER:
            continue
        names = {q.get("name") for q in a.get("predicates") or [] if q.get("passed") is True}
        if EXTERNAL_CI_PREDICATE not in names:
            continue
        if any(same_commit(str(a.get("commit") or ""), c) for c in commits if c):
            return a
    return None


def effective_required_predicates(db, project_id) -> list[str]:
    """The predicates `done` needs: the deployment's `required_predicates`, read live, plus the
    names the project added with the `merge.done_predicates` rule (GRPH-1426)."""
    from app.services import rule_toggles

    return rule_toggles.done_predicate_names(db, project_id)


def refuse_if_predicates_missing(db, item, merged, head) -> None:
    """Raise MissingAttestation when a required predicate has no passing attestation at
    `head`. Shared by `update_item` and `fleet.sign_off` (GRPH-1426): a sign-off passes the
    same gate. A predicate that did not run is not a pass."""
    required = effective_required_predicates(db, item.project_id)
    # GRPH-1619: an item whose deliverable lives in another repo can never get this repo's
    # `suite_green`; the reviewer's cited external run at the same head stands in for it.
    if CI_PREDICATE in required and head and external_ci_green(db, item, merged, [head]):
        required = [n for n in required if n != CI_PREDICATE]
    absent = missing_predicates(merged, required, commit=head or None)
    if not absent:
        # GRPH-1777: an observing `merge.done_predicates` records what it would have refused.
        from app.services import rule_observations, rule_toggles
        watched = missing_predicates(
            merged, rule_toggles.observed_done_predicates(db, item.project_id),
            commit=head or None)
        if watched:
            rule_observations.record(db, item.project_id, rule_toggles.DONE_PREDICATES_RULE,
                                     "would_refuse", item_id=item.id, commit=head or None,
                                     detail={"missing": watched})
    if absent:
        attested = sorted(attested_predicates(merged, commit=head or None))
        failed = [n for n in absent
                  if n in failing_predicates(merged, commit=head or None)]
        never_run = [n for n in absent if n not in failed]
        # Split here, not in missing_predicates. A GREEN suite_green receipt makes
        # has_valid_attestation true, so the gate above never sees the failing
        # conformance receipt — and without this split the CALL always said
        # "nobody has run" for a check that ran and failed (GRPH-569 bounce).
        bits: list[str] = []
        if failed:
            bits.append(
                f"{', '.join(failed)} ran and failed — that is a check that "
                "failed, so read what it found rather than running the adapter again"
            )
        if never_run:
            bits.append(
                f"nothing has attested {', '.join(never_run)}. That is a check "
                "nobody has run rather than a check that failed — the adapter that "
                "answers it has not reported, so run it rather than looking for a "
                "problem in what did"
            )
        what = ". ".join(bits)
        if failed and not never_run:
            detail = f"{', '.join(failed)} ran and failed."
        elif never_run and not failed:
            detail = f"nothing has attested {', '.join(never_run)}."
        else:
            detail = (
                f"{', '.join(failed)} ran and failed; "
                f"nothing has attested {', '.join(never_run)}."
            )
        record_refusal(db, item, predicate="attestation_incomplete", detail=detail)
        raise MissingAttestation(
            f"{item.key} cannot move to done: this project requires the predicate(s) "
            f"{', '.join(required)}. "
            + (f"What IS attested: {', '.join(attested)}. " if attested else "")
            + what
        )


#: The adapter `fleet.sign_off` writes its receipt under.
SIGN_OFF_ADAPTER = "fleet.sign_off"


def signed_off_at(evidence, head: str | None) -> bool:
    """A passing `fleet.sign_off` receipt bound to `head` (any commit when no head is known)."""
    return any(a.get("adapter") == SIGN_OFF_ADAPTER
               for a in valid_attestations(evidence, commit=head or None))


def refuse_if_unreviewed(db, item, merged, head) -> None:
    """Raise MissingAttestation when `done` has no independent sign-off at `head` and the
    project's `review.sign_off_before_done` rule is on (GRPH-1940; on unless a person turned it
    off). The message names the rule and the way to loosen it — a recommendation an agent can
    relay, not a switch it can flip: agents only tighten (GRPH-P91)."""
    from app.services import rule_toggles
    if signed_off_at(merged, head):
        return
    if not rule_toggles.sign_off_before_done(db, item.project_id):
        return
    rule = rule_toggles.SIGN_OFF_BEFORE_DONE_RULE
    detail = f"no independent sign-off ({rule} is on)."
    record_refusal(db, item, predicate="sign_off_missing", detail=detail)
    raise MissingAttestation(
        f"{item.key} cannot move to done: {detail} A reviewer signs it off, or a person "
        "closes it with owner-close; to allow done without review, a person can switch this "
        "project to the exploratory level on the Rules page."
    )


def ci_run_in_progress(evidence, commits) -> dict | None:
    """GRPH-1416. The newest CI run announced as started at one of `commits` and not answered since.

    CI posts a `ci_in_progress` attestation when a run begins (scripts/attest_ci.py
    `--mode started`). It is an announcement, not a verdict: it carries no `suite_green`, so it
    never satisfies the gate. A later `suite_green` receipt (pass or fail) at the same commit
    answers it; the evidence array only grows, so list order is time order.
    """
    live = None
    for e in _ci_run_records(evidence):
        names = {q.get("name") for q in e.get("predicates") or [] if isinstance(q, dict)}
        at = str(e.get("commit") or "")
        if not any(same_commit(at, c) for c in commits):
            continue
        if CI_RUNNING_PREDICATE in names:
            live = e
        elif CI_PREDICATE in names and live is not None and same_commit(
                str(live.get("commit") or ""), at):
            live = None
    return live


def recorded_pr_url(item) -> str:
    """A PR url recorded on the item outside its evidence (`github_url`, or a legacy `pr`
    dict's `url`), or "". Only a pull/merge request counts: an issue link is not a PR."""
    url = (getattr(item, "github_url", "") or "").strip()
    if is_pr_url(url):
        return url
    pr = getattr(item, "pr", None)
    url = str(pr.get("url") or "").strip() if isinstance(pr, dict) else ""
    return url if is_pr_url(url) else ""


#: The paths a no-PR refusal names, so "no PR" never reads as "no CI needed" (GRPH-1633).
NO_PR_PATHS = (
    "Open a PR for the branch (gbfleet proposes branches on `until`) or link its URL on the "
    "item, let CI attest suite_green at its head, then retry. Work that landed some other way "
    "(merged by hand, or in another repository) is closed by a project owner naming the merge "
    "commit (owner close), not by a sign-off. An item with no code at all carries no branch "
    "and no head commit, and this gate does not apply to it")


def _refuse_if_branch_has_no_pr(db, item, incoming_evidence, commit=None) -> None:
    """GRPH-1457, GRPH-1633: an item whose work is on a branch needs a linked PR to reach `done`.

    A missing PR used to read as "no PR needed": the cooldown and `suite_green` gate apply
    only to an item with a linked PR, so a branch nobody proposed went `done` with no CI
    attestation and its code on no PR. GRPH-1457 closed that only for a branch carrying the
    supervisor's salvage note; GRPH-1615 and GRPH-1614 went `done` from ordinary built
    branches the same way. So the trigger is the work itself: a recorded `branch`, or a
    `head_commit` the builder reported (or the commit the caller is completing at). Only ``linked is None`` reaches here, so no PR url is
    in the evidence or on the item; an owner's close is exempt (the owner names the merge).
    """
    branch = (item.branch or "").strip()
    head = (commit or item.head_commit or "").strip()
    if not branch and not head:
        return
    merged = append_evidence(item.evidence, incoming_evidence)
    if any(a.get("adapter") == OWNER_CLOSE_ADAPTER for a in valid_attestations(merged)):
        return
    where = f"branch {branch}" if branch else f"commit {head[:12]}"
    record_refusal(db, item, predicate="pr_missing",
                   detail=f"work is on {where} and no PR is linked")
    raise NoPRForBranch(
        f"{item.key} cannot move to done: its work is on {where} and no PR is linked, so no CI "
        f"run has attested {CI_PREDICATE} on it and nothing has reached the base. A missing PR "
        f"is not a PR that needs no CI. " + NO_PR_PATHS)


def refuse_if_pr_cooling_down(db, item, incoming_evidence=None, *,
                              commit: str | None = None) -> list[dict]:
    """Raise PRCooldown when `done` is asked for too soon after a PR link (GRPH-567).

    One function, two writers. `update_item` is tested; `fleet.sign_off` is the other
    allowed writer of `done` and used to skip this entirely — a reviewer who linked a
    PR and signed it off in the same minute is the defect the test file names.

    **The wait is for CI, so CI answering ends it (GRPH-947).** A `suite_green` attestation
    at the PR's head or merge commit is the outcome the cooldown exists to give time for;
    refusing an already-merged, already-attested PR for ten minutes protected nothing. Bound
    to a COMMIT, never "any green receipt": a pass at an older head says nothing about the
    code being completed. No commit named means no skip. Since GRPH-1345 the cooldown no
    longer clears itself: once it has expired without a `suite_green` the refusal stands
    until one arrives.

    **A first link that arrives WITH the verdict is stored before the refusal.** Before, the
    stamp was written only by a successful `update_item`, so a `done`/`sign_off` carrying the
    PR's first link measured ~0s, refused, and every retry measured ~0s again. The stamp is
    kept even though the call fails; first link wins, so no later URL moves it.

    **A run on another item can answer (GRPH-1411).** With no CI answer at the item's own
    commit, a run whose recorded PR `range` covered it — attested on whichever item that PR
    named — answers instead; the latest such run decides. Returns the receipts the caller
    must store with the `done` (a note naming the carrying run when one let it through), so
    the acceptance is as readable as a refusal would be.
    """
    cooldown = max(0, int(getattr(settings, "pr_cooldown_seconds", 0) or 0))
    linked = pr_linked_at(incoming_evidence or [], item.pr_linked_at)
    if linked is None and (recorded_pr_url(item) or pr_linked_at(item.evidence)):
        # GRPH-1633: a PR recorded on the item without the stamp (on `github_url`, or a url
        # receipt written before the stamp existed) is a linked PR and gets the CI gate. It
        # used to exempt the item from the no-PR gate AND skip this one.
        linked = utcnow()
    if cooldown and linked is None:
        _refuse_if_branch_has_no_pr(db, item, incoming_evidence or [], commit)
    if not cooldown or linked is None:
        return []
    merged = append_evidence(item.evidence, incoming_evidence or [])
    # An owner.close receipt is the owner saying the work is already on the base.
    # The cooldown waits for CI after a PR is linked; that wait does not apply to a
    # human closing something they already merged (GRPH-1030).
    if any(is_owner_close_merge(a) for a in valid_attestations(merged)):
        return []
    commits = pr_commits(merged, commit, item.head_commit)
    if any(CI_PREDICATE in attested_predicates(merged, commit=c) for c in commits):
        return []
    # GRPH-1619: the PR is in another repo whose CI does not report here; a reviewer's cited
    # external run at the head is the CI answer the cooldown was waiting for.
    if external_ci_green(db, item, merged, commits):
        return []
    # GRPH-1404: `agent_full` says the builder's full suites are the proof and the PR's CI is
    # not required, so silence from CI does not block when full receipts exist on BOTH engines.
    # CI saying NO at the item's commit still does.
    from app.services import rule_toggles
    if (rule_toggles.test_scope_mode(db, item.project_id) == "agent_full"
            and all(_full_engine_run(merged, e)
                    for e in rule_toggles.test_scope_engines(db, item.project_id))
            and not any(same_commit(str(a.get("commit") or ""), c) for c in commits
                        for a in attestation_receipts(merged)
                        if any(isinstance(q, dict) and q.get("name") == CI_PREDICATE
                               and q.get("passed") is False for q in a.get("predicates") or []))):
        return []
    waited = (utcnow() - _aware(linked)).total_seconds()
    runs = [a for a in attestation_receipts(merged)
            if any(isinstance(q, dict) and q.get("name") == CI_PREDICATE
                   for q in a.get("predicates") or [])]
    target = commits[0][:12] if commits else None
    if runs:
        last = runs[-1]
        ok = CI_PREDICATE in attested_predicates([last])
        run = (f"the last CI run reported is {last.get('adapter')} "
               + (f"run {last['run_ref']} " if last.get("run_ref") else "")
               + f"at {str(last.get('commit'))[:12]}, which "
               + ("passed there" if ok else f"did not pass {CI_PREDICATE}"))
    else:
        run = f"no CI run has reported {CI_PREDICATE} for it yet"
    # GRPH-1822. The pull loop writes no receipt while checks are absent, still
    # running, or unreadable, so this sentence is the only place that state shows.
    # Empty when the loop has not looked at this head: the old wording stays, and
    # a head nobody asked GitHub about does not grow a reason the box never had.
    from app.services import ci_pull
    clause = ci_pull.refusal_clause(commits, db, item=item, evidence=incoming_evidence)
    if clause:
        run = f"{run}. {clause}"
    # CI ran the suite at the PR's head and it did not pass: that is an answer, not silence,
    # so there is nothing to wait for (GRPH-1345).
    failed_at_head = [a for a in runs
                      if any(same_commit(str(a.get("commit") or ""), c) for c in commits)
                      and CI_PREDICATE not in attested_predicates([a])]
    if not failed_at_head:
        # GRPH-1411: no answer of its own at the commit, so ask the runs on OTHER items whose
        # PR range covered it. Only after the item's own red check: a red run at its own
        # commit is the item's answer and a green run elsewhere does not overrule it. Among
        # the carrying runs the LATEST answers — a PR re-run green after a red one has fixed
        # what the red one saw, and the reverse has broken it.
        carried = _range_receipts(db, item, commits)
        if carried:
            latest = carried[-1]
            if CI_PREDICATE in attested_predicates([latest]):
                return [_carried_note(latest, commits)]
            failed_at_head = [latest]
    if failed_at_head:
        if item.pr_linked_at is None:
            item.pr_linked_at = linked
        bad = failed_at_head[-1]
        failed = (f"{bad.get('adapter')} "
                  + (f"run {bad['run_ref']} " if bad.get("run_ref") else "")
                  + f"at {str(bad.get('commit'))[:12]} did not pass {CI_PREDICATE}"
                  + (f" (carried by {bad['_carried_by']}, whose PR range covered "
                     f"{_covered_here(bad, commits)})" if bad.get("_carried_by") else ""))
        # GRPH-1410: the failure CI named (jobs, first failing tests) rides on the refusal, so
        # a reviewer has the bounce reason in hand rather than reading this as "CI pending".
        why = "; ".join(str(q.get("detail")) for q in bad.get("predicates") or []
                        if isinstance(q, dict) and q.get("name") == CI_PREDICATE
                        and q.get("passed") is False and q.get("detail"))
        record_refusal(db, item, predicate="pr_ci_not_green",
                       detail=f"{failed}: {why}" if why else failed)
        raise PRCINotGreen(
            f"{item.key} cannot move to done: CI failed at {str(bad.get('commit'))[:12]}"
            + (f": {why}" if why else "")
            + f" — {failed}. This is a BOUNCE, not a wait: CI has answered and the answer is "
            f"no, so bounce the item with this failure as the reason. Waiting will not change "
            f"it; the PR needs a fix and a CI run that attests {CI_PREDICATE} at its head")
    if waited >= cooldown:
        # GRPH-1416: past the cooldown, but CI announced a run at the head and has not
        # answered. That is a wait, not the "needs an attestation or an owner" dead end below.
        going = ci_run_in_progress(merged, commits)
        if going is not None:
            if item.pr_linked_at is None:
                item.pr_linked_at = linked
            ref = going.get("run_ref") or "(no run reference)"
            at = str(going.get("commit"))[:12]
            record_refusal(db, item, predicate="pr_ci_in_progress",
                           detail=f"waiting on CI run {ref}, in progress at {at}")
            raise PRCooldown(
                f"{item.key} cannot move to done yet: waiting on CI run {ref}, in progress at "
                f"commit {at}. The cooldown is over but the run has not finished, so this is a "
                f"WAIT, not a bounce: retry once {CI_PREDICATE} is attested at the PR's head "
                "— nothing needs changing")
        # The cooldown gave CI time to answer. Silence after it is not a pass.
        if commits or not any(CI_PREDICATE in attested_predicates([a]) for a in runs):
            if item.pr_linked_at is None:
                item.pr_linked_at = linked
            where = (f"commit {target}" if target
                     else "any commit (none is named for its PR)")
            record_refusal(db, item, predicate="pr_ci_not_green",
                           detail=f"cooldown over, no {CI_PREDICATE} at {where}: {run}")
            raise PRCINotGreen(
                f"{item.key} cannot move to done: its PR was linked {int(waited)}s ago, the "
                f"cooldown is over, and {CI_PREDICATE} is not attested for {where} — {run}. "
                f"Waiting will not change this; CI must attest {CI_PREDICATE} at the PR's "
                "head or merge commit")
        return []
    if item.pr_linked_at is None:
        # Persisted by `record_refusal`'s commit below. Nothing else on the item has been
        # written by either caller at this point, so this is the only field that lands.
        item.pr_linked_at = linked
    remaining = int(cooldown - waited)
    waiting_on = (f"waiting on CI for commit {target}: {run}" if target
                  else f"no commit is named for its PR, so no {CI_PREDICATE} attestation can "
                       f"end the wait early ({run})")
    record_refusal(db, item, predicate="pr_cooldown",
                   detail=f"its PR was linked {int(waited)}s ago; "
                          f"{remaining}s of the cooldown remain; {waiting_on}.")
    raise PRCooldown(
        f"{item.key} cannot move to done yet: its PR was linked {int(waited)}s "
        f"ago and the cooldown is {cooldown}s, so CI has not had time to run — "
        f"{waiting_on}. Try again in {remaining}s, or once {CI_PREDICATE} is attested at "
        "the PR's head or merge commit — this refusal clears itself, and nothing "
        "needs changing"
    )


def set_reach(db: Session, item: Item, value: str) -> Item:
    """Declare where this item's work lands (GRPH-832). A PERSON's call, by construction.

    Deliberately NOT a field in `update_item`'s whitelist, and that omission is the whole
    control. `update_item` is the verb every agent holds; this function is reached only from
    `PATCH /api/items/{id}`, which takes a bearer JWT — so setting `deploy`, and clearing it
    again, requires somebody signed in. A worker cannot mark its own ops item as ordinary and
    proceed, which is the only way a declaration is worth anything.

    Unknown values raise. Silently keeping `repo` would be the worst outcome available here:
    the caller believes the item is marked, the divvy hands it to a child, and the refusal
    that should have fired reads as an absence.
    """
    from app.services import reach as reach_svc

    if value not in reach_svc.REACHES:
        raise ValueError(
            f"invalid reach: {value!r}; one of {', '.join(reach_svc.REACHES)}. "
            "`repo` is work that changes files in a worktree; `deploy` is work on a running "
            "system, which no agent may be handed."
        )
    item.reach = value
    db.commit()
    db.refresh(item)
    return item


#: How an item can be closed without delivering anything (GRPH-1671).
RESOLUTION_KINDS = ("duplicate", "superseded")
#: The rule a refused resolution cites (services/rules.py).
RESOLUTION_RULE = "close.resolution_points_at_the_work"


class ResolutionRefused(ValueError):
    """A close-as-duplicate/superseded the item or its pointer does not allow (GRPH-1671).

    `reason` is a stable code the boundary carries into the refusal: `already_done`,
    `bad_kind`, `target_missing`, `target_self`, `target_other_project`,
    `target_not_done`, `reason_required`, `claim_holder`, `typed_wait`."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def resolve_item(db: Session, item_id: str, *, kind: str, of: str, reason: str = "",
                 actor: str, actor_agent: str | None = None, defer=None) -> Item | None:
    """Close an item as a duplicate of another, or as superseded, WITHOUT an attestation
    (GRPH-1671).

    The completion gate asks for proof that work was checked. A duplicate did no work, so
    the gate could never open and the item sat in `blocked` forever. This is the one path
    that closes without that proof, and it is narrow on purpose:

    - `duplicate` points at another item in the same project that is itself `done` — its
      own attestation (or its own resolution) is what vouches for the work. Pointing at
      unfinished work is refused, or "duplicate of" would be a way round review.
    - `superseded` points at the item or PRD that replaced it, which need not be done yet,
      and needs a reason.
    - `actor_agent`, when an agent is calling, must not hold this item's build or review
      claim: the builder cannot close its own work this way. WHO may call at all (a
      planner, or the project owner) is the boundary's check — services stay
      principal-free, as `owner_close` does.

    The item is `done` but never `delivered` (`Item.delivered`): completeness and the close
    report count the item `of` points at, not this one.
    """
    item = get_item(db, item_id)
    if item is None:
        return None
    if item.status == "done":
        raise ResolutionRefused("already_done", f"{item.key} is already done")
    if kind not in RESOLUTION_KINDS:
        raise ResolutionRefused(
            "bad_kind", f"resolution kind must be one of {', '.join(RESOLUTION_KINDS)}, "
                        f"not {kind!r}")
    from app.services.waits import wait_tags_on
    if wait_tags_on(item):
        raise ResolutionRefused(
            "typed_wait", f"{item.key} is a typed human wait; clear it the way a wait clears "
                          "(answer it, or mark it done when the person has acted)")
    if actor_agent and actor_agent in {item.claimed_by, item.review_claimed_by}:
        raise ResolutionRefused(
            "claim_holder", f"{actor_agent} holds {item.key}'s "
            f"{'build' if actor_agent == item.claimed_by else 'review'} claim; the agent "
            "working an item cannot close it as a duplicate or superseded. Release it, and "
            "let a planner or the project owner close it")
    why = (reason or "").strip()
    if kind == "superseded" and not why:
        raise ResolutionRefused(
            "reason_required", "superseded needs a one-line reason: what replaced it and why")
    ref = (of or "").strip()
    target_item = None
    target_prd = None
    tid = keys.resolve_item(db, ref) if ref else None
    if tid:
        target_item = db.get(Item, tid)
    elif ref and kind == "superseded":
        from app.models import Prd
        pid = keys.resolve_prd(db, ref)
        target_prd = db.get(Prd, pid) if pid else None
    if target_item is None and target_prd is None:
        raise ResolutionRefused(
            "target_missing",
            f"{ref or '(nothing)'} is not an item" + (" or PRD" if kind == "superseded" else "")
            + " — name the one that carries the work")
    target_project = (target_item or target_prd).project_id
    if target_project != item.project_id:
        raise ResolutionRefused(
            "target_other_project", f"{ref} is in another project; a resolution points at "
                                    f"work in {item.key}'s own project")
    if target_item is not None and target_item.id == item.id:
        raise ResolutionRefused("target_self", f"{item.key} cannot resolve to itself")
    if kind == "duplicate" and target_item is None:
        raise ResolutionRefused("target_missing", f"a duplicate points at an item, not {ref}")
    if kind == "duplicate" and target_item.status != "done":
        raise ResolutionRefused(
            "target_not_done",
            f"{target_item.key} is {target_item.status}, not done. A duplicate points at work "
            "that has been finished and checked; until it is, close this one as superseded "
            "or leave it open")
    resolution = {
        "kind": kind,
        "of": target_item.key if target_item is not None else target_prd.key,
        "reason": why,
        "by": actor,
        "at": utcnow().isoformat(),
    }
    out = update_item(db, item.id, defer=defer, status="done", resolution=resolution)
    if out is None:
        return None
    from app.services import delegation as delegation_svc
    from app.services import fleet as fleet_svc
    fleet_svc.release_reservations(db, item_id=out.id)
    out.claimed_by = None
    out.claimed_at = None
    out.review_claimed_by = None
    out.review_claimed_at = None
    out.review_takes = 0
    delegation_svc.on_outcome(db, out, "released")
    db.commit()
    db.refresh(out)
    return out


OWNER_CLOSE_KINDS = ("merged", "verified")
OWNER_CLOSE_NOTE_MAX = 200


def is_owner_close_merge(attestation: dict) -> bool:
    """An `owner.close` attestation that carries a passed `on_base` predicate (GRPH-1908).

    A Verified close writes `walk_verified` instead, and says nothing about the base.
    """
    return attestation.get("adapter") == OWNER_CLOSE_ADAPTER and any(
        isinstance(q, dict) and q.get("name") == "on_base" and q.get("passed") is True
        for q in attestation.get("predicates") or [])


def owner_close(db: Session, item_id: str, *, closer: dict, commit: str,
                pr_url: str | None = None, defer=None, kind: str | None = None,
                note: str | None = None, url: str | None = None) -> Item | None:
    """Close an item whose work is on the base, as a signed-in project owner (GRPH-1030).

    Not `fleet.sign_off`. Completes through `update_item` so the completion gate is the
    one writer of `done` that is not the reviewer path. The receipt's adapter is
    `owner.close`, `reviewed_by` is not stamped, and harness review verdicts are not
    recorded — this is not a model sign-off.

    `closer` is `{id, handle, name}` of the JWT user, passed in rather than looked up:
    services stay principal-free. Authority (project owner, not an agent key) is the
    REST boundary.

    `kind` is `merged` (default) or `verified`. Verified records that the owner checked the
    work somewhere other than the base: it needs a one-line `note` naming where, takes an
    optional `url` (a walk log or artifact, never a PR url), and writes `walk_verified`
    in place of `on_base`.
    """
    kind = (kind or "merged").strip().lower()
    if kind not in OWNER_CLOSE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(OWNER_CLOSE_KINDS)}, not {kind!r}")
    verified = kind == "verified"
    note_text = (note or "").strip()
    if verified:
        if not note_text:
            raise ValueError("note is required for a verified close: one line naming where "
                             "the check happened")
        if "\n" in note_text or "\r" in note_text:
            raise ValueError("note must be a single line")
        if len(note_text) > OWNER_CLOSE_NOTE_MAX:
            raise ValueError(f"note is {len(note_text)} characters; the limit is "
                             f"{OWNER_CLOSE_NOTE_MAX}")
    item = get_item(db, item_id)
    if item is None:
        return None
    if item.status == "done":
        raise ValueError(f"{item.key} is already done")
    sha = (commit or "").strip().lower()
    if not _COMMIT.fullmatch(sha):
        raise ValueError(
            "commit must be 7–40 hex characters — the revision "
            + ("that was checked" if verified else "that landed on the base")
            + ", not a branch name (owner close needs the merge commit)"
        )
    if verified:
        url = (url or "").strip()
    else:
        url = (pr_url or "").strip()
        if url and not is_pr_url(url):
            raise ValueError(f"{url!r} is not a pull/merge request URL")
    cut = cut_from_commits(item.evidence)
    if any(same_commit(sha, base) for base in cut):
        raise ValueError(
            f"{item.key}: commit {sha[:12]} is the commit this branch was cut from, so it "
            "carries none of the work — name a later commit"
        )
    handle = str(closer.get("handle") or closer.get("name") or closer.get("id") or "owner")
    uid = str(closer.get("id") or "").strip()
    existing_pr = url or (item.github_url or "").strip() or next(
        (str(e.get("url") or "") for e in (item.evidence or [])
         if isinstance(e, dict) and is_pr_url(e.get("url") or "")),
        "",
    )
    pr_detail = existing_pr or "no PR url recorded"
    if verified:
        first = {"name": "walk_verified", "passed": True,
                 "detail": f"verified by {handle}: {note_text}"}
    else:
        first = {"name": "on_base", "passed": True,
                 "detail": f"owner {handle} closed as merged at {sha[:12]}; PR {pr_detail}"}
    receipt = {
        "kind": "attestation",
        "adapter": OWNER_CLOSE_ADAPTER,
        "commit": sha,
        "predicates": [
            first,
            {"name": "owner_close", "passed": True,
             "detail": (f"closed by {handle}"
                        + (f" ({uid})" if uid else "")
                        + ", not a model sign-off")},
        ],
    }
    incoming: list[dict] = [receipt]
    if url:
        incoming.append({
            "kind": "url", "url": url, "commit": sha,
            "detail": (f"walk log recorded by owner {handle}" if verified
                       else f"merge recorded by owner {handle}"),
        })
    item.evidence = append_evidence(item.evidence, incoming)
    if url and not verified and not (item.github_url or "").strip():
        item.github_url = url
    db.flush()
    out = update_item(db, item.id, defer=defer, status="done", closed_by_owner=True)
    if out is None:
        return None
    from app.services import delegation as delegation_svc
    from app.services import fleet as fleet_svc
    fleet_svc.release_reservations(db, item_id=out.id)
    out.claimed_by = None
    out.claimed_at = None
    out.review_claimed_by = None
    out.review_claimed_at = None
    out.review_takes = 0
    # `released`, not `signed_off`: builder/reviewer model metrics key on sign-off.
    # The attempt ended because a human took the item off the board, not because a
    # model reviewed it.
    delegation_svc.on_outcome(db, out, "released")
    db.commit()
    db.refresh(out)
    return out


def _check_fix_transition(db: Session, item: Item, fields: dict) -> None:
    """An update that turns a row INTO a filed fix meets the create rules (GRPH-1683).

    An update that changes neither kind nor scope leaves an old row alone.
    """
    new_kind = fields["kind"] if fields.get("kind") is not None else item.kind
    new_line = (fields["line_id"] or None) if "line_id" in fields else item.line_id
    new_prd = fields["prd_id"] if fields.get("prd_id") is not None else item.prd_id
    if fields.get("severity") is not None:
        _check_severity(fields["severity"], new_kind)
    sets_kind = fields.get("kind") == "fix" and item.kind != "fix"
    sets_line = bool(fields.get("line_id")) and not item.line_id and not item.prd_id
    sets_prd = (bool(fields.get("prd_id")) and new_kind == "fix" and not item.line_id
                and not item.prd_id)
    if not (sets_kind or sets_line or sets_prd):
        return
    section = fields["prd_section"] if fields.get("prd_section") is not None else item.prd_section
    check_line(new_line, new_prd, new_kind)
    check_fix_scope(kind=new_kind, line_id=new_line, prd_id=new_prd, prd_section=section or "")
    desc = fields["description"] if fields.get("description") is not None else item.description
    tps = list(item.touchpoints or []) + list(fields.get("touchpoints") or [])
    check_fix_shape(description=desc, touchpoints=tps)
    if new_prd and not new_line:
        stored = _stored_prd_id(db, new_prd, item.project_id)
        probe = Item(id=item.id, prd_id=stored)
        if not has_blocking_dependency(db, probe):
            raise ValueError(
                f"a blocking fix needs a dependency from an item on its PRD: "
                f"link_items(a=<an item on that PRD>, b={item.key}, type=dependency). {FIX_EXITS}")


def update_item(db: Session, item_id: str, defer=None, submitted_by: str | None = None,
                *, closed_by_owner: bool = False, **fields) -> Item | None:
    """Patch fields or advance status.

    `submitted_by` is who is making THIS call — an agent id, or `key:<name>` for a bare
    credential (`fleet.caller_identity`). It is read in exactly one place: an item entering
    `review` with no author gets it as `built_by`. See the stamp below for why.
    """
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']}")
    if fields.get("fidelity") is not None and fields["fidelity"] not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fields['fidelity']}")
    if fields.get("kind") is not None:
        _check_kind(fields["kind"])
    if fields.get("effort") is not None:
        effort_update = int(fields["effort"])
        if effort_update < 0:
            raise ValueError(f"negative effort: {effort_update}")
    prev_status = item.status
    if fields.get("status") == "blocked" and prev_status != "blocked":
        # PRD-35 D9: a linked delegation's attempt ended here.
        from app.services import delegation as delegation_svc
        delegation_svc.on_outcome(db, item, "blocked")
    # THE CALL (GRPH-636). Observe answers live on this write, not a helper the
    # handler might forget. Deleting this leaves PATCH 200 and siblings still waiting.
    from app.services import gitops as gitops_svc
    gitops_svc.refuse_observe_progress(db, item, fields)
    gitops_svc.refuse_already_progress(db, item, fields)
    if (fields.get("status") in ("review", "done")
            and fields["status"] != prev_status):
        # P30 D11. Filing a wait is not finishing the work. An unmet `wait:` dependency
        # means a human has not acted; `review`/`done` would read as delivered.
        from app.services.waits import unfinished_wait_deps, wait_tags_on

        waiting_on = unfinished_wait_deps(db, item)
        if waiting_on:
            raise ValueError(
                f"{item.key} is waiting on {', '.join(waiting_on)} — a typed human wait "
                "is not finished work. Leave this item blocked; do not move it to "
                f"{fields['status']}"
            )
        # P75 D7. A decision wait clears only by an answer: `answer_wait` stores it, then
        # calls here. Every other caller, a person moving the card included, is refused.
        if (fields["status"] == "done" and item.decision
                and "wait:decision" in {str(t).lower() for t in item.tags or []}
                and not item.decision.get("answer")):
            raise ValueError(
                f"{item.key} is a decision wait with no stored answer. Answer it with the "
                "Answer control (POST /api/items/{id}/answer) or, as an agent, `answer_wait`")
        # The wait ITEM itself going to `done` is the operator clearing it (not refused).
        # A wait item going to `review` is also not the original pretending to be done.
        if fields["status"] == "review" and wait_tags_on(item) and prev_status == "blocked":
            raise ValueError(
                f"{item.key} is a typed human wait. Mark it done when the human has "
                "acted; do not send it to review as if it were built work"
            )
        # GRPH-946: review is a handoff, and a handoff needs somewhere to look. Asked of an
        # agent or credential (`submitted_by` set — the MCP path), not of a person moving a
        # card on the board, whose item may have no branch at all. A typed wait is exempt:
        # it carries no built work, so there is no branch to point at.
        #
        # GRPH-1843: a branch that IS the measured gitops base has no commits for a
        # reviewer to read. Same caller rule as the handoff — an agent, not a person.
        if fields["status"] == "review" and submitted_by and not wait_tags_on(item):
            effective = _effective_review_branch(db, item, submitted_by, fields)
            from app.services import fleet as fleet_svc
            matched = fleet_svc.branch_equals_base(db, item.project_id, effective)
            if matched:
                raise ValueError(
                    f"{item.key} cannot move to review: branch `{effective}` equals its "
                    f"base `{matched}`, so it has no commits for a reviewer to read"
                )
        if (fields["status"] == "review" and submitted_by and not wait_tags_on(item)
                and not _submitter_branch(db, item, submitted_by)
                and not reviewable_handoff(item, fields.get("evidence"))):
            raise ValueError(
                f"{item.key} has no `branch` recorded and no PR url in `evidence`, so a "
                "reviewer has nothing to read. Claim it from a registered agent with a "
                "branch, or attach the PR url as evidence, then move it to review"
            )
    if fields.get("status") == "review" and prev_status != "review":
        refuse_review_over_open_fix(db, item)
        require_engine_evidence(db, item, fields.get("evidence"),
                                head=fields.get("head_commit"))
    # THE COMPLETION GATE (GRPH-543). Before this, `update_item` validated a status
    # transition for MEMBERSHIP IN A LIST and nothing else, so an agent working without a
    # reviewer reached `done` by writing the string — and the result was indistinguishable
    # from a placeholder move.
    #
    # Placed before anything is assigned, so a refusal leaves the row exactly as it was.
    #
    # PRINCIPAL-FREE, deliberately. This asks what the ITEM carries, never who is calling —
    # which is what lets it live in a service at all, given `authz`'s rule that services stay
    # principal-free so domain logic keeps one owner. Authority over who may ATTACH an
    # attestation is the `gate` scope, enforced at the boundary (GRPH-541). The two halves
    # are what make the guarantee hold: an agent cannot write the proof, and cannot complete
    # without it.
    #
    # Gated on the TRANSITION, not the state. Items that were already `done` predate any
    # attestation and are left alone; re-saving one does not re-ask the question.
    #
    # Evidence arriving in THIS call counts, so an adapter can attest and complete in one
    # write rather than needing two round trips to satisfy a gate it is itself satisfying.
    from app.services.waits import wait_tags_on as _wait_tags_on
    _completing = fields.get("status") == "done" and prev_status != "done"
    # A typed wait is a human act, not a delivery claim (P30 D11). The operator marks
    # it done from Tracker; requiring an attestation would make the wait un-clearable
    # without CI, which is the opposite of "a human emptied it".
    if _completing and _wait_tags_on(item):
        _completing = False
    # GRPH-1671. A duplicate or superseded item delivered nothing, so there is nothing to
    # attest. Only `resolve_item` passes `resolution`, after checking what it points at;
    # neither the MCP `update_item` field list nor the REST PATCH schema carries it.
    if _completing and fields.get("resolution"):
        _completing = False
    if _completing:
        merged_for_gate = append_evidence(item.evidence, fields.get("evidence") or [])
        # THE STALENESS CHECK (GRPH-555). An attestation names the commit it vouches for,
        # and until now nothing read it — so the gate asked "was this ever attested" rather
        # than "is this attested as it stands". Attest at A, push B, and A's receipt still
        # opened the gate. The dangerous case is not a passing run: it is CI FAILING on B,
        # writing no attestation, and leaving A's standing.
        #
        # `head_commit` is what an adapter last observed, written whether its run passed or
        # failed. Comparing against it is what makes a receipt expire.
        #
        # NO HEAD REPORTED falls back to the weaker check, deliberately. Refusing instead
        # would make completion impossible for every install without such an adapter —
        # including the offline one this product promises — and `fleet.sign_off` reports no
        # head either. The guarantee is therefore opt-in, and NOT silently so: an item
        # completed under the weak check is exactly one whose `head_commit` is empty, which
        # is a query rather than a receipt on every row.
        head = (fields.get("head_commit") or item.head_commit or "").strip()
        if not has_valid_attestation(merged_for_gate, commit=head or None):
            failing = [a for a in attestation_receipts(merged_for_gate)
                       if not all(q.get("passed") for q in (a.get("predicates") or []))]
            failed_names = sorted({q["name"] for a in failing
                                   for q in a.get("predicates") or []
                                   if not q.get("passed")})
            # A receipt that PASSED but names another commit is its own case, and saying
            # "nothing has attested it" there would send the author looking for a missing
            # run when what they actually need is to re-run on the head they just pushed.
            stale = [a for a in valid_attestations(merged_for_gate) if head] if head else []
            if stale:
                predicate, detail = "attestation_stale", (
                    f"it is attested at {stale[0]['commit'][:12]} but the head is "
                    f"{head[:12]} — the code moved after it was checked.")
            elif failing:
                predicate, detail = "attestation_failing", (
                    f"its attestation reports {', '.join(failed_names)} as failing.")
            else:
                predicate, detail = "attestation_missing", "nothing has attested it."
            # GRPH-1822. An item with no passing receipt never reaches the cooldown,
            # so the pull loop's reason has to be on THIS refusal. Empty when the
            # loop has not looked: the old sentence stays, and a head nobody asked
            # GitHub about does not grow a reason the box never had.
            if predicate == "attestation_missing":
                from app.services import ci_pull
                clause = ci_pull.refusal_clause(
                    [head] if head else [], db, item=item,
                    evidence=fields.get("evidence") or [])
                if clause:
                    detail = f"{detail} {clause}"
            record_refusal(db, item, predicate=predicate, detail=detail)
            raise MissingAttestation(
                f"{item.key} cannot move to done: {detail} `done` needs an "
                "`attestation` receipt naming the adapter, the commit it binds to, and at "
                "least one predicate that passed — written by a key with the `gate` scope "
                "(CI, or a reviewer via sign_off), not by the agent that built it"
                + (f" — {len(failing)} recorded attestation(s) carry a FAILING predicate: "
                   + "; ".join(f"{a['adapter']} says "
                               + ", ".join(q["name"] for q in a["predicates"]
                                           if not q.get("passed")) + " failed"
                               for a in failing)
                   if failing else "")
            )
        # THE REQUIRED PREDICATES (GRPH-569, PRD-26 §Conformance and adversarial gates).
        # Reached only once a valid attestation exists, so this asks a narrower question than
        # the gate above: not "was this checked" but "was it checked FOR THE THINGS THIS
        # DEPLOYMENT REQUIRES". Empty by default, so an install with only the CI adapter is
        # unaffected — that is the PRD's requirement, not a concession.
        #
        # Deliberately NOT folded into `has_valid_attestation`: a missing required predicate
        # and no attestation at all are different states, and the message an operator needs
        # differs. The one above says "get it attested"; this one names WHICH check has never
        # run, because "attestation_missing" would send them looking for an adapter that is
        # already working.
        refuse_if_predicates_missing(db, item, merged_for_gate, head)
        # THE REVIEW (GRPH-1940). Attested is not reviewed: a planner key with no fleet on it
        # took an item from `review` to `done` with nobody signing it off, while the same call
        # on a fleet key was refused — so whether review was skipped depended on the key, not
        # on a rule the project adopted. `owner_close` passes `closed_by_owner`; a resolution
        # close and `fleet.sign_off` never reach this line.
        if not closed_by_owner:
            refuse_if_unreviewed(db, item, merged_for_gate, head)
        # THE PR COOLDOWN (GRPH-567, PRD-26). Shared with `fleet.sign_off` — that path
        # used to write `done` directly and skip this, which is the reviewer CALL the
        # bounce found. Incoming evidence is read as well as the stored stamp so linking
        # and completing in ONE call is the case this catches.
        carried = refuse_if_pr_cooling_down(db, item, fields.get("evidence") or [],
                                            commit=fields.get("head_commit"))
        if carried:
            item.evidence = append_evidence(item.evidence, carried)
    # Captured BEFORE the status moves. `intent_hold` is about work in flight and goes
    # quiet once an item is done, so asking after the transition always answers None —
    # which silently turned the completion receipt into dead code.
    hold_at_completion = (
        _pending_hold(db, item)
        if fields.get("status") == "done" and prev_status != "done" else None
    )
    _check_fix_transition(db, item, fields)
    if fields.get("line_id") is not None or item.line_id:
        # Checked against the row as it WILL be, so setting line_id on a feature, and
        # setting prd_id or kind on a line item, are refused alike. An empty string clears.
        new_line = fields["line_id"] if "line_id" in fields else item.line_id
        if fields.get("line_id"):
            from app.services import lines as lines_svc
            new_line = lines_svc.resolve_for_item(db, fields["line_id"], item.project_id)
        check_line(
            new_line or None,
            fields["prd_id"] if fields.get("prd_id") is not None else item.prd_id,
            fields["kind"] if fields.get("kind") is not None else item.kind,
        )
        fields = {**fields, "line_id": new_line or None}
    if fields.get("prd_id") is not None:
        # The ITEM's project, not the caller's: an update must be scoped to where the
        # row actually lives, or a caller whose key defaults elsewhere could re-open
        # the cross-project reference this closes.
        fields = {**fields, "prd_id": _stored_prd_id(db, fields["prd_id"], item.project_id)}
        # Stamped when the link CHANGES, never on an ordinary edit. Re-saving an item
        # already on this PRD must not restamp it, or every touch after approval would
        # look like freshly added scope and the drift number would climb on activity
        # alone — a metric that rises when you work is one people learn to ignore.
        if fields["prd_id"] != item.prd_id:
            item.prd_linked_at = utcnow()
    for key in ("title", "description", "status", "tags", "effort", "blocker", "pr", "date",
                "head_commit",
                "github_url", "assignee", "prd_id", "prd_section", "fidelity", "kind",
                "severity"):
        if key in fields and fields[key] is not None:
            setattr(item, key, fields[key])
    if "blocker" in fields:
        # GRPH-1867. Links derived from the old text go when the new text no longer names them.
        drop_stale_blocker_dependencies(db, item)
    if "blocker" in fields and (item.blocker or "").strip():
        # GRPH-1530. The text is not the only record: item ids named in it become
        # dependency links in this same commit, so a later `done` can clear the block.
        record_blocker_dependencies(db, item)
    if "line_id" in fields:
        item.line_id = fields["line_id"]
    # GRPH-1671. Written only with the `done` it justifies (`resolve_item`), and dropped the
    # moment the item leaves `done`: a reopened duplicate is open work again, and must face
    # the completion gate like any other item when it next closes.
    if fields.get("resolution") and item.status == "done":
        item.resolution = fields["resolution"]
    elif item.status != "done" and item.resolution:
        item.resolution = None
    # GRPH-1031. `branch` is identity, not branding: a worker rewriting it under a live
    # reviewer is how GRPH-946's "reviewer has nothing to read" comes back. The CALL
    # gate (planner/supervisor only) lives in `fleet.check_tool_role`; this half is
    # what the field DOES. Empty is not a write — the same rule as touchpoints — so a
    # PATCH that sends `branch=""` cannot clear a pointer a reviewer is holding.
    if "branch" in fields and fields["branch"] is not None:
        incoming = str(fields["branch"]).strip()
        if incoming:
            held = (item.branch or "").strip()
            # GRPH-1395. Read the status the item had BEFORE this call: the loop above has
            # already written `status`, so `update_item(status=review, branch=…)` on an
            # `in_progress` item used to be refused as "in review". And refuse only while a
            # reviewer actually HOLDS it — the reviewer is who the pointer protects. With no
            # live hold there is nobody to bounce and nobody reading, so a planner routing
            # finished work to review must be able to name the branch it lives on.
            if held and incoming != held and prev_status == "review":
                from app.services import fleet as fleet_svc
                reviewer = fleet_svc.review_claim_holder(item)
                if reviewer:
                    raise ValueError(
                        f"{item.key} is `{prev_status}` on `{held}` and reviewer "
                        f"{reviewer} holds it; rewriting the branch would leave the "
                        "reviewer with nothing to read. Bounce it first, or leave the "
                        "pointer alone"
                    )
            item.branch = incoming
    # THE AUTHORSHIP STAMP (GRPH-848). `built_by` was written in one place — the claim — so
    # an item built inline and sent to review here carried no author at all, and
    # `independent(reviewer, None)` read that as "human-authored": measured on 2026-09-10,
    # 17 of the last 20 signed-off items had an empty `built_by` and a receipt claiming a
    # comparison that was never made. "Human-authored" and "an agent that never claimed"
    # were one value; they are not one case, because the second is the builder's own
    # credential unless something says otherwise.
    #
    # On the TRANSITION into review, never on a re-save: stamping a row that is already in
    # review would attribute somebody else's old work to whoever touched it next. And
    # NEVER over an existing author — a bounce-and-resubmit, or a supervisor moving its
    # child's item, keeps the real one. Rows that predate this stay empty on purpose
    # (backfill is out of scope); the sign-off receipt says "author unrecorded" for them.
    if (fields.get("status") == "review" and prev_status != "review"
            and not item.built_by and submitted_by):
        item.built_by = submitted_by
    # The branch, derived the way `claim_item` derives it (GRPH-752): an item built inline
    # and sent to review by an agent that registered a branch lands on THAT branch. Never
    # over an existing one, for the same reason `claim_item` never clears it (GRPH-946).
    if fields.get("status") == "review" and prev_status != "review" and submitted_by:
        derived = _submitter_branch(db, item, submitted_by)
        if derived:
            item.branch = derived
    pending_lesson_applies: list[str] = []
    # Rows `return_on_green` moved besides this one. Filled only when evidence arrived.
    # Their telemetry runs after the commit, with this item's own review transition.
    returned_on_green: list = []
    if fields.get("touchpoints") is not None:
        # Unions. See `union_touchpoints` — a write here must never remove a declared or
        # earlier-measured path, because the next partition reads this list as the item's
        # actual areas (P30 D10). Empty is not a write: `[]` would otherwise replace
        # predicted/declared paths and read as "no collision".
        incoming_tps = fields["touchpoints"]
        if incoming_tps:
            item.touchpoints = union_touchpoints(item.touchpoints, incoming_tps)
    if fields.get("touchpoints_remove"):
        # GRPH-1398. The one explicit way off the list; see `remove_touchpoints`.
        fields["removed_touchpoints"] = remove_touchpoints(
            db, item, fields["touchpoints_remove"], by=submitted_by,
            planner=bool(fields.get("remove_as_planner")))
    if fields.get("evidence") is not None:
        # Appends. See `append_evidence` — a write here must never remove a receipt somebody
        # else left, because `sign_off` gates on the stored ones (GRPH-494).
        old_len = len(item.evidence or [])
        item.evidence = append_evidence(item.evidence, fields["evidence"])
        # Stamped from the INCOMING rows, not the merged record, so the clock starts when the
        # link actually arrived (GRPH-567). First link wins — see `pr_linked_at`.
        item.pr_linked_at = pr_linked_at(fields["evidence"], item.pr_linked_at)
        pending_lesson_applies = _prepare_lesson_evidence(db, item, old_len)
        gitops_svc.apply_observe_answer(db, item)
        gitops_svc.apply_already_evidence(item)
        # `merge.reopen_on_red` (GRPH-1272): a red suite at the signed-off head sends the
        # item back to its builder on this same write. `reopened` below sees it leave `done`.
        # `return_on_green` is the other direction: a passing suite returns rows the fleet
        # bounced with `gbfleet CI red:`. Same new receipts, still uncommitted.
        from app.services import fleet as fleet_svc
        arrived = (item.evidence or [])[old_len:]
        fleet_svc.reopen_on_red(db, item, arrived)
        returned_on_green = fleet_svc.return_on_green(db, item, arrived)
    if fields.get("ack_section_drift"):
        # "I have read what changed in the PRD and this item is right as it stands"
        # (GRPH-360). A FLAG, not a value: the caller cannot supply the hash, so it can only
        # ever acknowledge the text that is there right now — an agent cannot pre-acknowledge
        # an edit it has not seen, and a later edit produces a new hash and flags again.
        from app.services import prds as prd_svc

        prd = db.get(Prd, item.prd_id) if item.prd_id else None
        if prd is None or not item.prd_section:
            raise ValueError("ack_section_drift needs an item linked to a PRD section")
        item.prd_section_ack = prd_svc.section_fingerprint(prd.body or "", item.prd_section)
    db.commit()
    db.refresh(item)

    # GRPH-1530. A blocker set onto items that are already done does not wait for
    # the next drain pass. Open dependencies leave it exactly as written.
    if "blocker" in fields and (item.blocker or "").strip():
        clear_satisfied_blocker(db, item)

    if pending_lesson_applies:
        _record_applied_lessons(db, item, pending_lesson_applies)

    # Reopening counts as a touchpoint change: a done item holds no `code` links (GRPH-1068),
    # so leaving `done` is when it regains them, to whatever open work it now overlaps.
    reopened = prev_status == "done" and item.status != "done"
    from app.services import observations as observations_svc
    if reopened:
        observations_svc.on_reopen(db, item)
    if item.status == "review" and prev_status != "review":
        observations_svc.on_rules_passed(db, item, (ENGINE_EVIDENCE_RULE,))
    for sib in returned_on_green:
        # This item is covered by the transition above. A sibling mutated in the same
        # session never entered `update_item`, so its hold closes here.
        if sib.id != item.id and sib.status == "review":
            observations_svc.on_rules_passed(db, sib, (ENGINE_EVIDENCE_RULE,))
    if fields.get("evidence"):
        observations_svc.on_evidence(db, item)
    if (("touchpoints" in fields and fields["touchpoints"] is not None) or reopened
            or fields.get("removed_touchpoints")) and item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)

    # Work can reach `in_progress` without ever taking a lease — a human moving a card, an
    # agent that edits rather than claims. Stamping here too is what keeps the hold from
    # covering only the subset of work that happens to use the claim path.
    if item.status == "in_progress" and prev_status != "in_progress":
        stamp_baseline_at_start(db, item)

    if item.status == "done" and prev_status != "done":
        from app.services.waits import (
            release_waiters, release_when_blocking_fix_done, wait_tags_on,
        )

        if wait_tags_on(item):
            # The wait is the human act. Clearing it unblocks the original unless
            # another unmet dep remains. Does not rewrite `in_progress`.
            release_waiters(db, item)
        # GRPH-1772. `sign_off` writes `done` itself and calls this same helper.
        release_when_blocking_fix_done(db, item)
        # GRPH-1530. A blocker that names this item, and nothing still open, clears.
        release_when_named_blockers_done(db, item)
        # A stored `code` link to finished work is noise every read pays for (GRPH-1068);
        # `related_items` still finds it by live overlap.
        # Committed even when nothing matched: the DELETE alone opens a write transaction, and
        # left open it locks SQLite against the enrichment session below.
        from app.services.clustering import prune_code_links
        prune_code_links(db, item)
        db.commit()
        _record_superseded_intent(db, item, hold_at_completion)
        # The two MODEL calls, moved off the response path (GRPH-399). Measured on the live
        # instance: a trivial prompt to its 24B chat model takes 20s, each call is bounded by
        # `llm_timeout_seconds = 90`, and completion ran both — so `update_item` could block
        # for ~180s against a presence TTL of 150s. A fleet agent is single-threaded, so
        # completing an item could push it past its own TTL and take it offline, releasing the
        # rest of its work. Completing is the call every agent makes at the end of every item.
        #
        # `defer` defaults to INLINE, which is today's behaviour, so every existing caller and
        # test keeps its ordering. The web callers pass a scheduler; under Starlette a
        # background task still runs before the test client returns, so the tests that drive
        # this through the status transition stay deterministic — and they must, because
        # catching a caller that quietly skips extraction is the reason they exist.
        run = defer or (lambda fn: fn())
        run(lambda: enrich_completed_item(item.id))
    if any(isinstance(t, str) and t.strip().lower() == "bug" for t in (item.tags or [])):
        from app.services import harness as harness_svc
        harness_svc.on_bug_updated(db, item)
        db.commit()
    return item


def enrich_completed_item(item_id: str) -> None:
    """The judge and the lesson extractor, on their own session (GRPH-399).

    Opens its own because it can outlive the request that scheduled it — the caller's session
    is closed by then, and reusing it is a use-after-free that only shows up under load.

    Neither result is needed by whoever completed the item: the judge writes a classification
    and the extractor writes candidate shards for human review. Both were already exception-
    isolated so a failure could not break the completion; this is about the DELAY, which was
    never isolated at all.
    """
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        item = s.get(Item, item_id)
        if item is None:
            return
        _classify_against_goal(s, item)
        _auto_extract_lessons(s, item)
    finally:
        s.close()


def _classify_against_goal(db: Session, item: Item) -> None:
    """Fire the platform judge on completion (GRPH-249).

    Here rather than at link time on purpose: at link time an item is an intention with
    nothing delivered to judge, and a judgement of an intention is a judgement of a
    sentence somebody typed. At completion it has evidence, touchpoints, and work behind
    it.

    Never allowed to break the completion. A judge that errors, times out, or is not
    configured must not stop an agent marking work done — the classification is a read on
    the work, not a gate on it, and making delivery depend on a model being reachable is
    how a feature gets routed around.
    """
    from app.services import prds as prd_svc  # local: prds imports this module

    try:
        prd_svc.classify_work(db, item)
    except Exception:  # noqa: BLE001
        logger.warning("platform judge: classification failed for %s", item.id, exc_info=True)


def _pending_hold(db: Session, item: Item) -> dict | None:
    from app.services import prds as prd_svc  # local: prds imports this module

    return prd_svc.intent_hold(db, item)


def _record_superseded_intent(db: Session, item: Item, hold: dict | None) -> None:
    """Stamp a completion that happened against intent which has since moved (GRPH-312).

    The hold is delivered on every read, but an agent can complete an item without ever
    looking — and then its work is classified against superseded intent and the resulting
    drift is blamed on delivery rather than on the invalidation nobody saw. Recording the
    mismatch at the moment of completion makes it attributable afterwards, which is the
    part that survives the agent walking away.

    `hold` is passed in because it has to be read before the status moves: the hold is
    about work in flight and goes quiet on `done`, so computing it here would always find
    nothing.

    Written as `evidence`, not a new column: this is a receipt about the work, which is
    exactly what that field already holds, and it travels wherever the item's evidence
    travels.
    """
    if hold is None:
        return
    item.evidence = append_evidence(item.evidence, [{
        "kind": "note",
        "detail": (f"Completed against superseded intent: work started under "
                   f"{hold['started_against']}, the governing baseline is now "
                   f"{hold['baseline_version']}. Classify against the baseline in force "
                   f"when this was built, not the current one."),
    }])
    db.commit()


def _auto_extract_lessons(db: Session, item: Item) -> None:
    """On completion, distill lessons into memory shards (respects project.auto_extract).

    **Delegates to `insights.extract_lessons` rather than reimplementing it (GRPH-358).**
    There were two extraction paths — the explicit MCP tool and this one — and they drifted:
    GRPH-358 taught the tool to read an item's OUTCOME before its description, and this path,
    which is the one that actually fires on completion and the one that produced the wrong
    lesson in the first place, kept distilling the raw description. Worse, the fix's test
    called the tool directly, so it passed while this stayed broken.

    One implementation, so the next change to what a lesson is made of cannot miss a caller.
    Imported inside the function because `insights` imports this module.
    """
    from app.models import Project
    from app.services import insights as insights_svc
    from app.services import memory as memory_svc

    project = db.get(Project, item.project_id)
    if project is not None and not project.auto_extract:
        return
    # Skip if we've already extracted for this item.
    existing = [s for s in memory_svc.list_shards(db, project_id=item.project_id)
                if s.source == f"lesson from {item.id}"]
    if existing:
        return
    try:
        insights_svc.extract_lessons(db, item.id)
    except Exception:  # noqa: BLE001 — extraction must never block a status change
        return


def reorder_items(db: Session, ordered_ids: list[str]) -> list[Item]:
    """Persist a new drag order. `ordered_ids` is the full desired top→bottom order."""
    for idx, iid in enumerate(ordered_ids):
        item = db.get(Item, keys.resolve_item(db, iid) or iid)
        if item is not None:
            item.sort_order = idx
    db.commit()
    return list_items(db)


def search_items(
    db: Session,
    query: str = "",
    status: str | None = None,
    limit: int = 25,
    project_id: str | None = None,
    tags: list[str] | None = None,
    prd_id: str | None = None,
) -> list[Item]:
    # Status/project/PRD are simple column filters (SQL); the free-text query and tag
    # matching run in Python so `query` can match a tag too and stay dialect-agnostic
    # (tags is a JSON column). `prd_id` is the stored id (`_prd_ref` resolved the
    # caller's key). Filtering it in Python would still load every item in the
    # project, which is the idle-drain scan GRPH-1645 removes.
    stmt = select(Item)
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    if status:
        stmt = stmt.where(Item.status == status)
    if prd_id:
        stmt = stmt.where(Item.prd_id == prd_id)
    rows = list(db.scalars(stmt.order_by(Item.sort_order.asc())).all())

    q = query.lower().strip()
    want_tags = {t.lower() for t in (tags or [])}

    def matches(it: Item) -> bool:
        item_tags = {t.lower() for t in (it.tags or [])}
        if want_tags and not (want_tags & item_tags):
            return False
        # `blocker` is searched too (GRPH-806). It was the one field carrying text somebody
        # WROTE and could not then find: a wave that cannot be scoped is bounded by parking
        # items, parking writes a sentence into `blocker`, and querying that exact sentence
        # returned nothing. Ten items said "PARKED by planner" and `search_items` found none
        # of them, so only their author could tell a scoping park from a real blocker.
        #
        # Less acute since `--prd` (GRPH-797) made parking stop being the only lever, and
        # still wrong: a field the product writes prose into and cannot search is a field
        # whose contents exist only for whoever typed them.
        if q and not any(q in hay for hay in (
            it.title.lower(), (it.description or "").lower(), (it.blocker or "").lower(),
        )) and not any(q in t for t in item_tags):
            return False
        return True

    return [it for it in rows if matches(it)][:limit]


def get_backlog(db: Session, limit: int = 20, project_id: str | None = None) -> list[Item]:
    stmt = select(Item).where(Item.status.in_(["backlog", "next"]))
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    stmt = stmt.order_by(Item.sort_order.asc()).limit(limit)
    return list(db.scalars(stmt).all())


def bounce_fields(item) -> dict:
    """What a bounce left on this item, for whoever reads it next (GRPH-378/379).

    Two facts, with different lifetimes and different readers:

    `bounce_reason` lasts. It is what the AUTHOR must act on, and they read it after they have
    reclaimed the item — by which point the pin may well have lapsed — so tying it to the
    reservation would delete it just before it is needed.

    `reserved_for` / `reserved_until` last only as long as the pin, because they are a claim
    about right now: who may take this item, and when everyone else may. A stale reservation
    shown to a worker is worse than none, since it argues against a claim the server would
    actually allow.

    Both ABSENT rather than null when they do not apply, matching `intent_hold` — an
    unbounced item should not carry three empty fields suggesting a bounce that never was.
    """
    out: dict = {}
    if item.bounce_reason:
        out["bounce_reason"] = item.bounce_reason
    from app.services import fleet as fleet_svc
    holder = fleet_svc.bounce_pin_holder(item)
    if holder:
        out["reserved_for"] = holder
        out["reserved_until"] = item.bounce_pinned_until.isoformat()
    return out


def reserved_elsewhere(db: Session, agent_id: str, project_id: str | None = None,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[dict]:
    """Ready items this agent was refused because they are pinned to somebody else.

    Without this an empty `claim_next` is byte-identical whether the backlog is empty or every
    ready item is reserved — so a worker that should idle two minutes and retry concludes the
    project is finished and stops asking. Absence read as clean, in the one response an idle
    agent makes its next decision from.

    The `holder != agent_id` guard is correctness by construction rather than a case that
    arises: a ready item pinned to the caller is one the caller simply claims, so the branch
    cannot run. Sabotaging it leaves the suite green — recorded here so the next reader does
    not go looking for the test that covers it.
    """
    from app.services import fleet as fleet_svc

    out = []
    for cand in _ready_candidates(db, project_id, lease_seconds):
        holder = fleet_svc.bounce_pin_holder(cand)
        if holder and holder != agent_id:
            out.append({"id": cand.key, "reserved_for": holder,
                        "reserved_until": cand.bounce_pinned_until.isoformat()})
    return out


def below_needs_tier(db: Session, agent_id: str, project_id: str | None = None,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[dict]:
    """Ready items this agent was skipped past because its declared tier is too low.

    The same job as `reserved_elsewhere` (GRPH-1647). An empty `claim_next` is otherwise
    byte-identical whether the board is empty or every ready item needs a higher tier, so
    a cheap worker concludes the project is finished and the frontier item is never seated.
    An agent that declared no tier is absent from this list: it is not below anything, and
    listing it would be the silent refusal the mismatch rule forbids.
    """
    from app.services import fleet as fleet_svc

    declared = fleet_svc.agent_declared_tier(db, agent_id)
    if declared is None:
        return []
    scope = seat_scope(db, agent_id)
    seated = held_by_a_seat(db, agent_id)
    out = []
    for cand in _ready_candidates(db, project_id, lease_seconds):
        # The same filters as `claim_next`, in the same order. An item the sweep skipped
        # for scope, deploy, a pin or headroom was not skipped for tier, and naming it
        # here would point the caller at the wrong fact.
        if not in_scope(cand, scope):
            continue
        if (cand.reach or "repo") == "deploy" and seated:
            continue
        if pinned_elsewhere(cand, agent_id):
            continue
        if routing_svc.held_back(db, cand, agent_id):
            continue
        needs = fleet_svc.bounce_needs_tier(cand)
        if fleet_svc.tier_is_below(declared, needs):
            out.append({"id": cand.key, "needs_tier": needs, "declared_tier": declared})
    return out


def _found_during_for(item: Item) -> str | None:
    from sqlalchemy.orm import object_session
    db = object_session(item)
    return found_during_of(db, item) if db is not None else None


def item_dict(item: Item) -> dict:
    """The fields every item read shares — the ONE renderer (GRPH-821).

    `get_backlog`, `search_items`, the claim and review replies and `get_item_details` all
    start here, so a field added for one read is on every read. `id`/`prd_id` render from the
    project's CURRENT tag; the stored id is frozen and internal, and an agent that quotes a
    rendered key back is resolved by services/keys (PRD-13). Emitting the stored id would leak a
    retired tag straight into agent memory, where it would outlive the rename by months.
    """
    out = {
        "id": item.key,
        "project_id": item.project_id,
        "title": item.title,
        "status": item.status,
        "tags": item.tags,
        "touchpoints": item.touchpoints or [],
        "effort": item.effort,
        "assignee": item.assignee,
        "claimed_by": item.claimed_by,
        # GRPH-850: unix timestamp so `choose_resume` can tell a live lease from a stale
        # one. Empty string when nobody holds. Reply-only, like `review_claimed_by` —
        # do not add it to `_ITEM_SCHEMA`; that copies onto every item tool and blows the
        # MCP token ceiling (PR #774: 82 tokens, 80 over CEILING).
        "claimed_at": int(item.claimed_at.timestamp()) if item.claimed_at else "",
        # The reviewer's hold, distinct from the lease above (GRPH-429: a different column,
        # deliberately). `claimed_by` stays the BUILDER's through `review`, so a supervisor
        # that read it as "somebody is reviewing this" never spawned a reviewer for any real
        # review row — found on the PRD-39 acceptance walk. Reply-only: the outputSchema is
        # manifest, and the footprint has seventeen tokens of headroom.
        "review_claimed_by": item.review_claimed_by,
        "prd_id": item.prd_key,
        # GRPH-1681. Reply-only; null for an item that is not on the maintenance line.
        "line_id": item.line_key,
        # GRPH-1683. Reply-only and omitted when empty.
        **({"severity": item.severity} if item.severity else {}),
        # GRPH-1695. A fix's provenance; null is "not recorded". Reply-only.
        **({"found_during": _found_during_for(item)} if item.kind == "fix" else {}),
        "prd_section": item.prd_section,
        "fidelity": item.fidelity,
        # GRPH-1358. Always present: "" is the honest "not recorded", which a release must be
        # able to tell apart from a field that was never sent. Reply-only, like `claimed_at`.
        "kind": item.kind or "",
        "evidence": item.evidence or [],
        # Authorship, distinct from the lease above (GRPH-379). It is the input the review
        # independence rule is decided on, and it was readable nowhere — so an agent could not
        # tell whose work it was about to review, nor explain a refusal it received.
        "built_by": item.built_by,
        "reviewed_by": item.reviewed_by,
    }
    if (item.branch or "").strip():
        # PRESENT ONLY WHEN SET (GRPH-1031), the same rule bounce_reason follows: an
        # empty branch on every row would spend the list payload saying "nobody has
        # claimed this" a thousand times, and a supervisor resuming after a bounce
        # needs the value that IS there, not a field that is always present and
        # usually blank.
        out["branch"] = item.branch
    if item.review_claimed_by:
        # The column stays set after the holder dies (GRPH-1254). Printing it as the holder
        # is how a planner, reading `get_item_details`, concluded a lapsed claim would never
        # lapse (GRPH-1290). The field a reader sees is `review_claim_holder`: a live holder,
        # or nobody. A lapsed hold carries `review_claim_lapse` and does not name a holder.
        # Reply-only, like the column itself.
        from app.services.fleet import review_hold_read
        hold = review_hold_read(item)
        out["review_claimed_by"] = hold["holder"]
        if hold["lapse"]:
            out["review_claim_lapse"] = hold["lapse"]
    if item.status == "review":
        # GRPH-1830. Present only when set: the row waits on a person, and a supervisor must
        # start no reviewer for it. Reply-only, like `review_claim_lapse`.
        from app.services.fleet import review_needs_person
        why = review_needs_person(item)
        if why:
            out["review_needs_person"] = why
    if item.decision:
        # The agent's question and options, and the answer once given (GRPH-1137). Present
        # only on a decision wait; `notify` bookkeeping stays server-side. Reply-only.
        from app.services.waits import decision_dict
        out["decision"] = decision_dict(item)
    if item.resolution:
        # Closed as a duplicate or superseded (GRPH-1671): `{kind, of, reason, by, at}`.
        # Present only when set, like `decision`, so a done item that delivered work and one
        # that delivered nothing never read alike. Reply-only.
        out["resolution"] = dict(item.resolution)
    qs = item.question_state
    if qs:
        # Present only when set (GRPH-P75 D1), like `branch`: one function, every surface.
        out["question_state"] = qs
    if item.done_at is not None:
        # When it last reached `done` (GRPH-1134), a unix timestamp like `claimed_at`. Shown
        # only when set, like `branch`: every open row would otherwise carry an empty field.
        # Reply-only, for the same token-ceiling reason as `claimed_at`.
        out["done_at"] = int(item.done_at.timestamp())
    if item.reviewed_by_capabilities:
        # The reviewer's tier, vendor and model (GRPH-945), so a cheap-on-cheap sign-off reads
        # differently from a frontier or human one. Present only once somebody signed it off,
        # the same rule `reach` follows: nothing to say on an unreviewed row.
        out["reviewer"] = item.reviewed_by_capabilities
    if (item.reach or "repo") != "repo":
        # PRESENT ONLY WHEN IT IS NOT THE DEFAULT (GRPH-832), the same rule the rebaseline
        # notice below follows: on the overwhelming majority of items this costs nothing, and
        # on the ones where a reader's next move changes it is impossible to miss. Emitting
        # `"reach": "repo"` on every row would spend the manifest's remaining headroom saying
        # "ordinary" a thousand times.
        out["reach"] = item.reach
    out.update(bounce_fields(item))
    # In-flight invalidation (GRPH-242/312). Present only when this item's PRD rebaselined
    # after work on it started — so it costs nothing on the overwhelming majority of reads
    # and is impossible to miss on the ones that matter. Delivered here rather than on the
    # claim path because an agent can complete an item without ever claiming it, and that
    # was the hole: its work then gets classified against intent it never saw move.
    from sqlalchemy.orm import object_session

    from app.services import prds as prd_svc  # local: prds imports this module

    # Same degradation as `models._key_of`: a detached object has no session to ask, and
    # serialization must not raise over a field that is absent on nearly every row.
    session = object_session(item)
    hold = prd_svc.intent_hold(session, item) if session is not None else None
    if hold:
        out["intent_hold"] = hold
    return out


#: What a link endpoint renders as for a viewer who cannot read its project (GRPH-1063). Not
#: the key: a key names the project and proves the item exists. Not dropped either — a hidden
#: blocker that vanished from the read would make its dependent look unblocked.
HIDDEN_REF = "(an item in a project you cannot see)"


def render_ref(db: Session, stored_id: str, readable: list[str] | None = None) -> str:
    """Render a link endpoint, which may be an item OR a request (`links.a`/`b` are untyped
    strings), under the project's current tag. Falls back to the stored id so a dangling edge
    still serializes.

    `readable` is the viewer's project scope. Given, an endpoint in a project outside it renders
    as `HIDDEN_REF` — links may cross projects (GRPH-1063), reads may not. None means the
    caller already decided the viewer may see everything."""
    for kind in ("item", "request"):
        row = db.get(keys.MODELS[kind], stored_id)
        if row is not None:
            if readable is not None and row.project_id not in readable:
                return HIDDEN_REF
            return row.key
    return stored_id


# Shard statuses an item read and a brief leave out: memory a person or migration took out of
# retrieval (GRPH-1234).
HIDDEN_SHARD_STATUSES = ("retired", "rejected")


def get_item_details(db: Session, item_id: str, readable: list[str] | None = None) -> dict | None:
    from app.models import Link, MemoryShard, Request

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    # Query by the RESOLVED id, never the caller's string. `item_id` may be any form
    # that resolves — a key rendered under the project's current tag, a retired tag, or
    # a pre-tag legacy id — while these columns hold the frozen stored id. Using the
    # caller's string made linked memory and requests silently vanish the moment a
    # project was retagged and an agent looked the item up by its new key (PRD-13).
    # A retired or rejected shard is out of memory; it must not ride the item read either
    # (GRPH-1234: retired pr_cooldown refusals kept showing up as linked memory).
    shards = db.scalars(select(MemoryShard).where(
        MemoryShard.item_id == item.id,
        MemoryShard.status.notin_(HIDDEN_SHARD_STATUSES))).all()
    reqs = db.scalars(select(Request).where(Request.linked_to == item.id)).all()
    links = db.scalars(select(Link).where(or_(Link.a == item.id, Link.b == item.id))
                       .order_by(Link.id)).all()
    # Built ON the shared renderer, not beside it. This read used to build its own dict, which
    # is how the intent hold, the bounce (GRPH-378/379), the branch (GRPH-752) and then the
    # project, touchpoints and PRD link (GRPH-821) each went missing from the read that calls
    # itself the full record. `test_item_details_full_record` asserts the superset now, so the
    # next field added to `item_dict` cannot skip this read again.
    from app.services import documents as documents_svc

    out = item_dict(item)
    out.update({
        "reference_documents": documents_svc.references_for(db, item_id=item.id),
        "description": item.description,
        "blocker": item.blocker,
        "pr": item.pr,
        # PRD-17 D3's handoff: WHERE the work landed. "" means nobody has claimed it yet, which
        # is different from the concept not existing.
        "branch": item.branch or "",
        # Every edge touching this item, both directions, endpoints rendered. The tool promises
        # "dependencies"; `brief.blocked_by` is one direction of one type, unfinished only.
        # A far endpoint the viewer cannot read stays in the list as `HIDDEN_REF` (GRPH-1063).
        "links": [{"type": link.type, "a": render_ref(db, link.a, readable),
                   "b": render_ref(db, link.b, readable),
                   "reason": link.reason or ""} for link in links],
        "linked_shards": [{"id": sh.id, "text": sh.text, "source": sh.source} for sh in shards],
        "linked_requests": [{"id": r.key, "title": r.title, "type": r.type} for r in reqs],
    })
    return out


def suggest_next(db: Session, project_id: str | None = None) -> Item | None:
    """The best item to start now: dependency-ready backlog/next, ranked by the composite
    priority score (status, unblocks-many, votes, effort, staleness)."""
    from app.services import prioritization as prio

    ranked = prio.prioritized(db, project_id, statuses=("next", "backlog"), include_blocked=False)
    return ranked[0]["item"] if ranked else None


# ---- Assignment / agent claiming (feature A) ----

def _aware(dt):
    """SQLite hands datetimes back naive; treat a naive value as UTC for comparisons."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_claimable(it: Item, cutoff) -> bool:
    """An item is claimable if it isn't blocked and is either fresh unclaimed backlog/next,
    or work with a stale (abandoned) lease."""
    # Status first (GRPH-1032). A planner "Do not launch" blocker is the usual pair, but
    # `status=blocked` with an empty blocker used to fall through to the status-in-list
    # check only on the unclaimed path — name the status so claim_next / claim_cluster /
    # a bound seat cannot launch it even when a supervisor asks.
    if it.status == "blocked":
        return False
    if it.blocker:
        return False
    # GRPH-1650. A decision wait is a question for a person, never build work, whatever
    # status it was filed in: a `backlog` wait was claimable and a drain delegated the QUESTION.
    if any(str(t).lower() == "wait:decision" for t in (it.tags or [])):
        return False
    stale = it.claimed_by is not None and it.claimed_at is not None and _aware(it.claimed_at) < cutoff
    if stale:
        return it.status in ("next", "backlog", "in_progress")
    if it.claimed_by is None:
        # GRPH-1685. A line item with no acceptance clause or no touchpoint is in the line's
        # pool for reporting and out of it for claim. Fresh work only: a stale reclaim above
        # was ready when it was taken.
        if it.line_id and unready_reasons(it):
            return False
        return it.status in ("next", "backlog")
    return False  # someone holds a live lease


def claimable(item: Item, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, now=None) -> bool:
    """Could some agent take this item right now? (GRPH-397)

    Public because the answer must be the SAME everywhere it is asked, and it was not: this
    predicate governed `claim_next`, while `clusters_for_project` decided the divvy's pool with
    its own rule — `status in ("backlog", "next")`. An item whose holder died stays
    `in_progress` (the lease expires lazily; nothing rewrites the row), so it satisfied this
    and failed that, and `claim_cluster` could never offer it again.

    That was survivable while `claim_cluster` was a fleet-worker tool. It stopped being
    survivable when it became what every posture is taught, because then a crashed agent's item
    is offered to nobody at all — the queue silently losing work.
    """
    cutoff = (now or utcnow()) - timedelta(seconds=lease_seconds)
    return _is_claimable(item, cutoff)


def _ready_candidates(db: Session, project_id: str | None, lease_seconds: int) -> list[Item]:
    from app.services import prioritization as prio

    ctx = prio.context(db, project_id)
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    out = []
    for it in ctx.items:
        if not _is_claimable(it, cutoff):
            continue
        # An unfinished dependency is not claimable on any status this sweep offers,
        # including a stale in_progress reclaim (GRPH-1530). A status write back to
        # `next` used to make the dependent look free while the dependency was still
        # being built. A free-text blocker is already refused by `_is_claimable`.
        if prio.blocked_by(ctx, it):
            continue
        out.append(it)
    out.sort(key=lambda it: (-prio.score(ctx, it), it.sort_order))
    return out


class TierBelowNeeds(Exception):
    """The claimant declared a tier below the one the last bounce named (GRPH-1647).

    Not a subclass of `OutOfScope`. Those loops skip and carry on, which is right for one
    neighbour and wrong for a named claim: `claim_item` has to say `needs_tier`. The sweep
    paths catch this and skip, and an empty reply names the tier. A bare None would read
    as an empty board.
    """

    def __init__(self, item_key: str, needs_tier: str, declared_tier: str):
        self.item_key = item_key
        self.needs_tier = needs_tier
        self.declared_tier = declared_tier
        super().__init__(
            f"{item_key} needs_tier={needs_tier}; declared tier {declared_tier} is below it")


def tier_block(db: Session, item: Item, agent_id: str) -> TierBelowNeeds | None:
    """The refusal when `agent_id`'s declared tier is below this item's, else None.

    An agent that declared no tier is not blocked: that is `undeclared`, and the mismatch
    rule records it as a decision rather than a refusal (PRD-35 D8, GRPH-1647).
    """
    from app.services import fleet as fleet_svc

    declared = fleet_svc.agent_declared_tier(db, agent_id)
    needs = fleet_svc.bounce_needs_tier(item)
    if declared is None or not fleet_svc.tier_is_below(declared, needs):
        return None
    return TierBelowNeeds(item.key, needs, declared)


class ReachesOutsideTheRepo(Exception):
    """A seat asked for an item declared to act on a running system (GRPH-832).

    Separate from `OutOfScope` because the remedies have nothing in common: a scope refusal
    means "ask your wave for work it chose", and this one means "no seat may take this, ever;
    a person does it". Collapsing them would put both behind one message and send half the
    readers to the wrong fix.
    """


class OutOfScope(Exception):
    """This agent's SEAT was minted for one scope and the item is outside it (GRPH-827).

    An exception rather than a `None`, because `claim_item` already returns `None` for "lost
    the race" and the two are opposite instructions: lose a race and the right move is to try
    the next candidate, ask for work outside your scope and there is no next candidate — the
    answer will not change however many you try. That is the flattening this codebase keeps
    finding, so it is not repeated here.
    """


class BoundSeatBusy(OutOfScope):
    """A bound seat asked for a second item while its own is still open (GRPH-948).

    A subclass of `OutOfScope` so every path that already skips an out-of-scope neighbour
    (`next_cluster`, the bound registration) skips this one the same way; the message says
    which remedy applies.
    """


class ReviewOnlySeat(Exception):
    """This agent's SEAT may review work and may never TAKE build work (GRPH-988).

    Deliberately NOT a subclass of `OutOfScope`, and the difference is the whole design.
    Every `except OutOfScope` in this codebase SKIPS the candidate and carries on —
    `next_cluster`'s neighbour loop, the bound seat's registration — which is right for one
    item outside a scope and wrong here: a review-only seat is outside EVERY item's scope, so
    a skip loop would sweep the whole board and hand back an empty batch. That reads as
    "nothing was ready", the reassuring answer, in place of "you may not take any". Inheriting
    the scope hierarchy would make that the behaviour of every path that already handles
    scope, and nothing would fail.
    """


#: One sentence for both claim writes (GRPH-988), for the reason `claim_item`'s docstring
#: gives for there being only two of them: the message a child acts on should not depend on
#: which door it knocked at. It names the remedy the child actually has — `claim_review` —
#: and says that an empty answer there is a normal exit rather than a failure, because a
#: review-only child that reads the refusal as "try something else" has nothing else to try.
REVIEW_ONLY_REFUSAL = (
    "this seat is review-only: it was minted to drain the review queue and may not take build "
    "work. Call claim_review; when that answers claimed=false, exit — an empty review queue "
    "is the normal end of a review-only run, not a failure."
)


def open_bound_item(db: Session, agent_id: str | None) -> str | None:
    """The key of the item this agent's seat is BOUND to, while it is not yet in review or
    done — else None (GRPH-948). An unbound seat, or none, has no bound item."""
    if not agent_id:
        return None
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return None
    seat = db.get(Enrolment, agent.enrolment_id)
    if seat is None or not seat.item_id:
        return None
    bound = db.get(Item, seat.item_id)
    if bound is None or bound.status in ("review", "done"):
        return None
    return bound.key


def seat_scope(db: Session, agent_id: str | None) -> str:
    """The PRD this agent's seat was minted for, or "" for an unscoped seat (GRPH-827).

    Resolved through the AGENT rather than passed in, because the caller that would pass it is
    the child, and the whole point is that the child does not get a vote. `--prd` on the
    supervisor bounds what is delegated; this bounds what the delegate can then go and take for
    itself, and one wave measured the gap at three delegated items against six self-claimed
    ones outside the scope.

    Unscoped is the honest default and stays the default: a fleet key with no enrolment, a
    human's credential, and every seat minted before this column existed all read "". An
    unscoped seat behaves exactly as it did.
    """
    if not agent_id:
        return ""
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return ""
    seat = db.get(Enrolment, agent.enrolment_id)
    if seat is None:
        return ""
    # GRPH-1685. A seat has at most one scope, the PRD or the maintenance line, and this
    # returns whichever is set. Both are frozen ids from different key namespaces, so
    # `in_scope` can match either against an item without a second parameter.
    return seat.prd_id or seat.line_id or ""


def seat_line(db: Session, agent_id: str | None) -> str:
    """The maintenance line this agent's seat is scoped to, or "" (GRPH-1685)."""
    if not agent_id:
        return ""
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return ""
    seat = db.get(Enrolment, agent.enrolment_id)
    return (seat.line_id or "") if seat is not None else ""


def held_by_a_seat(db: Session, agent_id: str | None) -> bool:
    """Is this agent a SPAWNED CHILD rather than somebody's own session? (GRPH-832)

    Answered by whether it registered on an enrolment. A seat is minted for a child by a
    planner; a person's own agent — the one behind a Claude Code window, registered on a
    plain project key — has none. So this is the same lookup `seat_scope` makes, asking the
    other question that credential answers.

    NOT keyed on role. A worker role is what almost every agent holds, including the human's
    own, and refusing on it would stop a person doing ops work through their own tools —
    which is precisely the thing this is meant to leave possible.
    """
    if not agent_id:
        return False
    agent = db.get(Agent, agent_id)
    return bool(agent is not None and agent.enrolment_id)


class BoundOnlySeat(Exception):
    """This agent's seat was minted for one item and that item has left in_progress (GRPH-1089).

    Deliberately NOT a subclass of `OutOfScope`, for the reason `ReviewOnlySeat` gives. A skip
    loop would sweep the board and hand back an empty batch, and "nothing was ready" is the
    reassuring reading of "you were told to stop". The flag is opt-in: a seat minted without
    it reviews other items after its own, which is what a `gbfleet until` wave relies on.
    """


def _bound_only_item(db: Session, agent_id: str | None) -> tuple[Enrolment | None, Item | None]:
    """The seat and its bound row for an agent on a `bound_only` seat, else (None, None).

    The row may itself be None — a bound item that has been deleted — and the seat is still
    returned so the caller can say which item it was.
    """
    if not agent_id:
        return None, None
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return None, None
    seat = db.get(Enrolment, agent.enrolment_id)
    if seat is None or not seat.bound_only or not seat.item_id:
        return None, None
    return seat, db.get(Item, seat.item_id)


def reclaimable_bound_item(db: Session, agent_id: str | None, *,
                           lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """The bound item, when a `bound_only` seat may take it back right now (GRPH-1101).

    A lease lapse requeues the item to `next` with `claimed_by` empty — or leaves it
    `in_progress` under a stale lease until something rewrites it. Either way the seat's
    work is not delivered and nobody live holds it, so the seat may reclaim exactly this
    row. Review and done are delivered; a live lease, a blocker or a bounce pin for somebody
    else make it not this seat's to take.
    """
    _seat, bound = _bound_only_item(db, agent_id)
    if bound is None or bound.status in ("review", "done"):
        return None
    if not claimable(bound, lease_seconds=lease_seconds) or pinned_elsewhere(bound, agent_id):
        return None
    return bound


def bound_only_refusal(db: Session, agent_id: str | None, item: Item | None = None, *,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS) -> str | None:
    """Why this seat may not take another item or another review, or None when it may.

    Only a seat minted `bound_only` is ever refused. For one, keyed on its bound item:

    - `in_progress` under this agent: None. The seat is doing the work it was minted for,
      and reviewing someone else's item is still allowed.
    - review or done: refused. The work is delivered; the run is over (GRPH-1089).
    - held under a live lease by another agent: refused, naming the holder.
    - reclaimable — requeued by a lease lapse, or a stale lease (GRPH-1101): refused for
      everything EXCEPT `item` being the bound row itself. The seat reclaims its own item
      and nothing else. `claim_next` resolves to it; see `reclaimable_bound_item`.
    - anything else (blocked, pinned to another agent): refused.

    An unbound seat, a seat without the flag, and an agent with no enrolment all read None
    and behave exactly as they did.

    The sentence is one, and every door says it: `claim_review`, `claim_next`, `claim_item`
    (which is where `claim_cluster`'s members and `next_cluster`'s neighbours claim), and
    `claim_cluster` itself, which returns the sentence before its loop. A door that does not
    consult this hands the seat more build work once the lease is dropped, because
    `_holds_bound_item` only matches a live `claimed_by` and `open_bound_item` returns None
    for review and done.
    """
    seat, bound = _bound_only_item(db, agent_id)
    if seat is None:
        return None
    doors = ("claim_review, claim_next, claim_item, claim_cluster and next_cluster are "
             "refused. ")
    if bound is None:
        return (
            f"this seat is bound_only to {seat.item_id}, which is gone. " + doors
            + "Exit — that is the normal end of a bound-only run, not a failure."
        )
    if bound.status == "in_progress" and bound.claimed_by == agent_id:
        return None
    if bound.status in ("review", "done"):
        return (
            f"this seat is bound_only to {bound.key}, which has left in_progress "
            f"(status {bound.status}). " + doors
            + "Exit — that is the normal end of a bound-only run, not a failure."
        )
    # Not "left in_progress" (GRPH-1101). A lapsed lease is not a delivered item: before this
    # the seat was refused its OWN item and exited having changed nothing.
    if reclaimable_bound_item(db, agent_id, lease_seconds=lease_seconds) is not None:
        if item is not None and item.id == bound.id:
            return None
        return (
            f"this seat is bound_only to {bound.key}, which is back in {bound.status} with "
            "no live claimant — its lease lapsed. Reclaim it: claim_item on "
            f"{bound.key}, or claim_next, which resolves to it. Any other item, "
            "claim_review, claim_cluster and next_cluster are refused until it is delivered."
        )
    if bound.claimed_by and bound.claimed_by != agent_id:
        return (
            f"this seat is bound_only to {bound.key}, which is now held by "
            f"{bound.claimed_by}. " + doors
            + "Exit — the item is somebody else's to deliver."
        )
    return (
        f"this seat is bound_only to {bound.key}, which has left in_progress "
        f"(status {bound.status}) and cannot be reclaimed. " + doors
        + "Exit — that is the normal end of a bound-only run, not a failure."
    )


def bound_only_seat(db: Session, agent_id: str | None) -> bool:
    """Is this agent on a seat minted BOUND-ONLY (GRPH-1089)?

    Echoed on `register_agent`, always, so a child can tell "the flag is false" from "this
    server has never heard of it". The gate is `bound_only_refusal`; this only reports the
    flag, including while the bound item is still `in_progress` and the gate is silent.
    """
    if not agent_id:
        return False
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return False
    seat = db.get(Enrolment, agent.enrolment_id)
    return bool(seat is not None and seat.bound_only)


def review_only_seat(db: Session, agent_id: str | None) -> bool:
    """Is this agent on a seat minted REVIEW-ONLY (GRPH-988)?

    Answered through the AGENT's enrolment rather than passed in, for the reason `seat_scope`
    gives: the caller that would pass it is the child, and the whole point is that the child
    does not get a vote. A supervisor told to build nothing mints these for the children it
    spawns to drain the review queue; without a server-side flag the only thing saying
    "review, do not build" was the child's prompt, which is not a control.

    NOT keyed on role. `reviewer` merged into `worker` in S3 (PRD-39) precisely so that one
    agent claims, builds and reviews, and the self-review ban is keyed on authorship — so a
    role check here would either refuse every worker (including a person's own agent, which
    `held_by_a_seat` exists to leave alone) or nothing at all. An agent with no enrolment
    reads False and behaves exactly as it did.
    """
    if not agent_id:
        return False
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.enrolment_id:
        return False
    seat = db.get(Enrolment, agent.enrolment_id)
    return bool(seat is not None and seat.review_only)


def in_scope(item: Item, scope: str) -> bool:
    """Is this item inside `scope`? An empty scope admits everything, including PRD-less work.

    An item with no `prd_id` is OUTSIDE every non-empty scope, and that is a real cost rather
    than an oversight: most bug reports carry no PRD, so a scoped wave cannot reach them at
    all. GRPH-828 adds the second axis (a tag) so a wave can be scoped to work that was never
    part of a PRD. Until then the operator's choice is a scoped wave or a reachable backlog,
    and it is better that this is written down than discovered.
    """
    return not scope or scope in ((item.prd_id or ""), (item.line_id or ""))


def unready_reasons(item: Item) -> list[str]:
    """What a LINE item lacks before it may be claimed (GRPH-1685): any of `acceptance`
    (no parsed clause) and `touchpoints` (none). Empty for a ready item and for any item that
    is not on the line, whose readiness is the ordinary rules' business."""
    if not item.line_id:
        return []
    from app.services import fleet as fleet_svc

    missing = []
    if not fleet_svc.parse_acceptance(item.description or "")[0]:
        missing.append("acceptance")
    if not [t for t in (item.touchpoints or []) if str(t).strip()]:
        missing.append("touchpoints")
    return missing


def unready_line_items(db: Session, project_id: str | None) -> list[dict]:
    """`[{id, missing}]` for every open line item that is out of the claim pool for lacking an
    acceptance clause or a touchpoint. Empty means it looked and found none."""
    stmt = select(Item).where(Item.line_id.is_not(None),
                              Item.status.in_(("backlog", "next")),
                              Item.claimed_by.is_(None))
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    out = []
    for it in db.scalars(stmt.order_by(Item.number)).all():
        missing = unready_reasons(it)
        if missing:
            out.append({"id": it.key, "missing": missing})
    return out


class AlreadyHolding(Exception):
    """This agent already holds a live claim, so it may not take a second (GRPH-504).

    PRD-17 D-g is one worker, one worktree. Two items in one worktree is two lots of work on
    one branch, which is the shape the whole cluster model exists to prevent — and it happens
    without anybody choosing it: `claim_next` stamped `built_by` every time it was called, and
    nothing looked at whether the caller was already holding something.

    FOUND BY CLEANING UP AFTER THE PRD-24 S7 WALK, where a confused local model was holding two
    items: the one it was working and a second it had claimed and never opened. The second was
    `in_progress`, invisible to the divvy, and stamped with an author who did nothing.
    """


def live_claim(db: Session, agent_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """The item this agent is holding right now, or None.

    "Live" means exactly what the reclaim path means by it — a lease inside `lease_seconds` —
    and it is computed with the same comparison rather than a second one that could drift. An
    agent whose lease lapsed is holding nothing and must be able to claim again without an
    operator intervening, which is the whole reason the cutoff is not simply `claimed_by`.

    The reviewer's hold is `review_claimed_by`, a different column, and is deliberately not
    this: a reviewer holding a review claim may still be a worker elsewhere (GRPH-429).
    """
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    for item in db.scalars(select(Item).where(Item.claimed_by == agent_id)).all():
        if item.claimed_at is not None and _aware(item.claimed_at) >= cutoff:
            return item
    return None


def keys_named_in(text: str) -> list[str]:
    """Item-key tokens in `text`, in order, without duplicates.

    The grammar is the rendered key (`GRPH-1685`, `GBC-77`), not a prefix of a
    longer token. The fleet drain uses the same expression.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _BLOCKER_KEY.finditer(text or ""):
        key = match.group(1)
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def blocker_names_person_or_decision(text: str) -> bool:
    """A person or a decision, named in the blocker text. Those are left alone."""
    return bool(_PERSON_OR_DECISION.search(text or ""))


def unfinished_dependency_ids(db: Session, item: Item) -> list[str]:
    """Dependency targets that exist and are not `done`. A missing row does not block."""
    ids = db.scalars(select(Link.b).where(
        Link.type == "dependency", Link.a == item.id)).all()
    open_ids: list[str] = []
    for dep_id in dict.fromkeys(ids):
        dep = db.get(Item, dep_id)
        if dep is not None and dep.status != "done":
            open_ids.append(dep_id)
    return open_ids


def _hold_live(item: Item) -> bool:
    if not item.claimed_by or item.claimed_at is None:
        return False
    cutoff = utcnow() - timedelta(seconds=DEFAULT_LEASE_SECONDS)
    claimed = _aware(item.claimed_at)
    return claimed is not None and claimed >= cutoff


def drop_stale_blocker_dependencies(db: Session, item: Item) -> int:
    """Remove this item's `blocker`-reason dependency links its blocker text no longer names.

    Links with any other reason are left alone (GRPH-1867).
    """
    named: set[str] = set()
    for token in keys_named_in((item.blocker or "").strip()):
        stored = keys.resolve_item(db, token)
        if stored:
            named.add(stored)
    stale = db.scalars(select(Link).where(
        Link.type == "dependency", Link.a == item.id,
        Link.reason == BLOCKER_DEP_REASON)).all()
    dropped = 0
    for link in stale:
        if link.b not in named:
            db.delete(link)
            dropped += 1
    return dropped


def _depends_through_links(db: Session, start: str, target: str) -> bool:
    """True when `start` reaches `target` through dependency links."""
    seen = {start}
    frontier = [start]
    while frontier:
        nxt = db.scalars(select(Link.b).where(
            Link.type == "dependency", Link.a.in_(frontier))).all()
        frontier = []
        for b in nxt:
            if b == target:
                return True
            if b not in seen:
                seen.add(b)
                frontier.append(b)
    return False


def record_blocker_dependencies(db: Session, item: Item) -> list[str]:
    """Link this item to every item its blocker text names (GRPH-1530).

    The free-text field is what a worker types; the link is what a later `done`
    can see. A person, a decision, a typed wait, and a blocking-fix filing are
    not this record — those already have their own release rules. Returns the
    stored ids linked, including ones that already had a dependency edge.
    """
    text = (item.blocker or "").strip()
    if (not text or blocker_names_person_or_decision(text)
            or text.endswith(BLOCKING_FIX_BLOCKER_SUFFIX)):
        return []
    from app.services.waits import wait_tags_on
    if wait_tags_on(item):
        return []
    linked: list[str] = []
    for token in keys_named_in(text):
        stored = keys.resolve_item(db, token)
        if not stored or stored == item.id:
            continue
        other = db.get(Item, stored)
        if other is None:
            continue
        existing = db.scalar(select(Link).where(
            Link.type == "dependency", Link.a == item.id, Link.b == other.id))
        if existing is None:
            # GRPH-1867. A link that closes a cycle would stall the drain: skip it.
            if _depends_through_links(db, other.id, item.id):
                continue
            db.add(Link(
                project_id=item.project_id, a=item.id, b=other.id,
                type="dependency", reason=BLOCKER_DEP_REASON, confidence=1.0,
            ))
        elif not (existing.reason or "").strip():
            existing.reason = BLOCKER_DEP_REASON
        linked.append(other.id)
    return linked


def _satisfied_blocker_keys(db: Session, item: Item) -> list[str] | None:
    """Keys this blocker waits on, when every one of them is `done`.

    None means leave the blocker alone: it names a person or a decision, it is a
    typed wait or a blocking-fix filing, a named token is not a done item, or it
    names nothing this rule can see.
    """
    text = (item.blocker or "").strip()
    if not text or item.status not in ("blocked", "next"):
        return None
    if blocker_names_person_or_decision(text) or text.endswith(BLOCKING_FIX_BLOCKER_SUFFIX):
        return None
    from app.services.waits import wait_tags_on
    if wait_tags_on(item):
        return None
    targets: dict[str, Item] = {}
    for token in keys_named_in(text):
        stored = keys.resolve_item(db, token)
        if not stored:
            return None
        other = db.get(Item, stored)
        if other is None:
            return None
        targets[other.id] = other
    link_ids = db.scalars(select(Link.b).where(
        Link.type == "dependency", Link.a == item.id,
        Link.reason == BLOCKER_DEP_REASON)).all()
    for dep_id in dict.fromkeys(link_ids):
        dep = db.get(Item, dep_id)
        if dep is None:
            return None
        targets.setdefault(dep.id, dep)
    if not targets or any(dep.status != "done" for dep in targets.values()):
        return None
    return [dep.key for dep in targets.values()]


def clear_satisfied_blocker(db: Session, item: Item, *, commit: bool = True) -> bool:
    """Clear a blocker whose named dependencies are all done, and say why.

    `blocked` returns to `next` (or stays `in_progress` on a live lease). A
    `next` item keeps its status; dropping the text is what makes it ready.
    """
    names = _satisfied_blocker_keys(db, item)
    if not names:
        return False
    shown = ", ".join(names)
    item.evidence = append_evidence(item.evidence, [{
        "kind": "note",
        "detail": f"blocker cleared: {shown} done",
    }])
    item.blocker = ""
    if item.status == "blocked":
        item.status = "in_progress" if _hold_live(item) else "next"
    if commit:
        db.commit()
        db.refresh(item)
    return True


def release_when_named_blockers_done(db: Session, finished: Item) -> list[str]:
    """After `finished` reaches `done`, clear blockers that named only done items.

    Covers a dependency link and a blocker text that mentions the key. A person,
    a decision, a typed wait, and a blocking-fix filing are left to their own
    rules (GRPH-1530, GRPH-1772).
    """
    if finished.status != "done":
        return []
    candidate_ids = list(db.scalars(select(Link.a).where(
        Link.type == "dependency", Link.b == finished.id)).all())
    key = (finished.key or "").strip()
    if key:
        mentioned = db.scalars(select(Item.id).where(
            Item.blocker.is_not(None), Item.blocker.contains(key))).all()
        candidate_ids.extend(mentioned)
    released: list[str] = []
    for dep_id in dict.fromkeys(candidate_ids):
        if dep_id == finished.id:
            continue
        item = db.get(Item, dep_id)
        if item is None:
            continue
        if clear_satisfied_blocker(db, item):
            released.append(item.id)
    return released


def claim_next(
    db: Session, agent_id: str, project_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS, skip: list[str] | None = None,
) -> Item | None:
    """Atomically assign the best ready item to `agent_id` and move it to in_progress.

    Concurrency-safe: the UPDATE guard means only one caller wins a given row, so two agents
    never claim the same item. Returns the claimed item, or None if nothing is ready.

    Skips an item bounced back to somebody else while its pin holds (PRD-17 D-f). The author
    still has the worktree, the branch and the review comment in context; handing that to a
    cold agent throws away precisely what cluster assignment exists to preserve. The pin
    LAPSES rather than binding forever — an author who never comes back is the common case,
    and a hard pin would strand the item.

    Skips an item whose last bounce named a tier above the one this agent declared
    (GRPH-1647). `gbfleet until` already re-seats that item; a claim that does not read the
    tier puts a cheap worker back on it. An agent that declared no tier is not skipped.
    """
    # What the caller has already declined this call-round (GRPH-429). Releasing an item does
    # not advance the queue — the released item is top-scored again, so a claim/release loop
    # returns it forever; measured at eight for eight. An agent that cannot take the head of
    # the queue could reach nothing behind it, in either role.
    #
    # Caller-supplied rather than remembered server-side: a decline is a fact about this
    # agent's turn, not about the item, and storing it would mean deciding when it expires.
    declined = {s for s in (skip or [])}
    # GRPH-988. Refused BEFORE any candidate is considered, and refused rather than filtered
    # to an empty result. The scope and `reach=deploy` checks below SKIP a candidate because
    # the caller asked for "whatever is next" and the next one may be fine; here no candidate
    # can ever be fine, so a sweep that returned None would answer "nothing was ready" — the
    # reassuring reading, and the one that sent a reviewer supervisor's child home believing
    # the board was empty when the truth is that it was forbidden. Checked first of all
    # because it is a property of the SEAT: unlike a held lease, no wait and no release makes
    # the answer different.
    if review_only_seat(db, agent_id):
        raise ReviewOnlySeat(REVIEW_ONLY_REFUSAL)
    # GRPH-1089. Same shape as the review-only check, and checked BEFORE the live-lease
    # refusal below. A bound child that has just moved its item to review still holds that
    # lease, so `AlreadyHolding` would answer first and the reason would name the lease
    # instead of the binding — and deleting this check would then leave the suite green,
    # because the lease refusal looks like a refusal. The binding is a property of the SEAT:
    # no release makes the answer different once the item has left in_progress.
    #
    # GRPH-1101. Before the refusal: a seat whose lease lapsed and whose item was requeued
    # asks for "whatever is next", and the only answer it may have is its own item. Unless
    # it declined it this round — then the refusal says why there is nothing else.
    own = reclaimable_bound_item(db, agent_id, lease_seconds=lease_seconds)
    if own is not None and own.id not in declined and own.key not in declined:
        return claim_item(db, own.id, agent_id, lease_seconds=lease_seconds)
    if reason := bound_only_refusal(db, agent_id, lease_seconds=lease_seconds):
        raise BoundOnlySeat(reason)
    # ONE AT A TIME (GRPH-504). Checked before any candidate is considered, so a caller that
    # already holds something is refused rather than quietly given a second item to abandon.
    held = live_claim(db, agent_id, lease_seconds=lease_seconds)
    if held is not None:
        raise AlreadyHolding(
            f"{agent_id} already holds {held.key} — call `release_item` on it before "
            "claiming another. One worker, one worktree (PRD-17 D-g): a second item claimed "
            "into the same tree is two lots of work on one branch.\n"
            # GRPH-809: the verb, by name. The refusal said "release it" and left the reader
            # to find the tool, and `update_item` LOOKS like the way — it moves the status and
            # leaves `claimed_by` exactly where it was, so an agent that moved its item on
            # believes it let go and is refused again with the same sentence.
            "Moving it with `update_item` does NOT release it: the status changes and the "
            "claim stays."
        )
    # The seat's scope, resolved ONCE for the whole sweep rather than per candidate: it is a
    # property of the caller, and reading it inside the loop would be a query per item.
    scope = seat_scope(db, agent_id)
    seated = held_by_a_seat(db, agent_id)
    # Resolved once: the tier is a property of the caller, and reading it per candidate
    # would be a query per item for an answer that cannot change mid-sweep.
    from app.services import fleet as fleet_svc
    declared_tier = fleet_svc.agent_declared_tier(db, agent_id)
    for cand in _ready_candidates(db, project_id, lease_seconds):
        if cand.id in declined or cand.key in declined:
            continue
        # Filtered, not refused. An empty result here is the ordinary "nothing ready" — the
        # caller asked for whatever was next, and outside its scope there was nothing. The
        # refusal belongs to `claim_item`, where the caller named the item it wanted.
        if not in_scope(cand, scope):
            continue
        # GRPH-832, same reasoning: the caller asked for "the next thing", so a `deploy` item
        # is skipped rather than refused. The one it named is where the sentence belongs.
        if (cand.reach or "repo") == "deploy" and seated:
            continue
        if pinned_elsewhere(cand, agent_id):
            continue
        # PRD-62 S4: capacity routing. Skipped like the pin, for the same reason: the caller
        # asked for "whatever is next" and the next may be held for a runner with headroom.
        if routing_svc.held_back(db, cand, agent_id):
            continue
        # GRPH-1647. Skipped, like the pin: the caller asked for whatever is next, and the
        # next item may not be the one a bounce held for a higher tier. The empty reply
        # names what was skipped (`below_needs_tier`); a bare None would say the board
        # was empty. An undeclared tier is not below, so it is not skipped.
        if fleet_svc.tier_is_below(declared_tier, fleet_svc.bounce_needs_tier(cand)):
            continue
        claimed = _try_claim(db, cand, agent_id)
        if claimed is not None:
            routing_svc.record_claim(db, claimed, agent_id)
            return claimed
        # Lost the race for this candidate — try the next.
    return None


def _try_claim(db: Session, cand: Item, agent_id: str) -> Item | None:
    """Atomically claim `cand` for `agent_id`. Optimistic-concurrency guard: only win the row
    if `claimed_by` is still what we observed (None for fresh, the stale holder for a reclaim),
    so two agents never take the same item. Dialect-safe — no time math in SQL."""
    stmt = update(Item).where(Item.id == cand.id)
    stmt = (
        stmt.where(Item.claimed_by.is_(None))
        if cand.claimed_by is None
        else stmt.where(Item.claimed_by == cand.claimed_by)
    )
    # `built_by` is written HERE and nowhere else, and never CLEARED — which is not the same
    # as never changed, and the comment used to read as if it were. A later claimant overwrites
    # it, so this is the CURRENT builder rather than the first one. That is what the self-review
    # ban needs (the agent who wrote the code now is the one who must not pass it), and it does
    # mean an item passed between agents keeps only its most recent author. Observed on the
    # walk: after the bounce pin lapsed and a second worker took FA-12, `built_by` moved with it.
    # The reservation is SPENT by the claim, whoever made it. It exists to give the author
    # first refusal on work they still have in context; once anybody holds the item there is
    # nothing left to reserve. Leaving it set outlived its meaning in a way that reads as
    # current: `built_by` moves to the new holder while `bounce_pinned_to` still names the old
    # author, so `get_item_details` renders a live reservation for an agent that does not hold
    # the item — a wrong answer to the question the field exists to answer.
    # ONE timestamp for both, so "nothing has been written since the claim" is an equality
    # rather than a race against the flush clock. `updated_at` is otherwise stamped by
    # `onupdate` a few microseconds after `claimed_at` is computed here, which made the
    # untouched case indistinguishable from a worked one (GRPH-434).
    now = utcnow()
    values = dict(claimed_by=agent_id, claimed_at=now, assignee=agent_id,
                  built_by=agent_id, status="in_progress", updated_at=now,
                  bounce_pinned_to=None, bounce_pinned_until=None)
    # PRD-17 D3 says this column IS the handoff: "Where the work landed. Travels to the
    # reviewer, who otherwise cannot see the diff." It existed and nothing ever filled it, so
    # a reviewer handed an item had no pointer to the branch carrying the work and read the
    # base branch instead — measured on the deployed instance, where a branch sitting on the
    # remote at 8778e9dd was reported as "absent from the reviewed tree" (GRPH-752).
    #
    # Derived, not asked for. The child already registers with its branch, so the server knows
    # it; an instruction to set it is one more thing a child can forget, which is how the two
    # links before this one broke (#639, #641). It moves with the claimant for the same reason
    # `built_by` does — the work will land on THIS agent's branch — but an agent that reports
    # no branch never CLEARS one, because losing the pointer is the defect, not the fix.
    claimant = db.get(Agent, agent_id)
    branch = ((claimant.branch if claimant is not None else "") or "").strip()
    if branch:
        values["branch"] = branch
    stmt = stmt.values(**values)
    if db.execute(stmt).rowcount == 1:
        db.commit()
        # Holding a lease outranks having been dismissed: the roster must never hide work.
        from app.services import fleet as fleet_svc
        fleet_svc.restore_on_work(db, agent_id)
        db.expire_all()
        item = db.get(Item, cand.id)
        stamp_baseline_at_start(db, item)
        # PRD-35 D7: every claim path ends here, so this is the one place a delegation is
        # linked (a declared child) or superseded (anyone else).
        from app.services import delegation as delegation_svc
        delegation_svc.on_claim(db, item, agent_id)
        return item
    db.commit()
    return None


def stamp_baseline_at_start(db: Session, item: Item) -> None:
    """Record which agreed intent this item's work started against (GRPH-242).

    Written once and never overwritten: the question it answers is "what was agreed when
    work began", and restamping on a later claim would erase exactly the mismatch the
    in-flight hold is derived from. An item with no PRD, or on a PRD with no baseline, has
    no agreed intent to have started against and stays NULL.
    """
    if item is None or item.baseline_at_claim or not item.prd_id:
        return
    from app.services import prds as prd_svc  # local: prds imports this module

    base = prd_svc.baseline_of(db, item.prd_id)
    if base is None:
        return
    item.baseline_at_claim = base.version
    db.commit()


def pinned_elsewhere(item: Item, agent_id: str) -> bool:
    """Is this item reserved for a DIFFERENT agent right now?

    Lives here, beside every claim path, because it used to live inside one of them: only
    `claim_next` consulted the pin, so `claim_cluster` and `next_cluster` — which claim through
    `claim_item` — handed a bounced item to a stranger while its author's reservation was still
    live. Verified on the fleet: an item pinned with 592 seconds remaining, taken by another
    agent through `claim_cluster`. A guarantee that holds on one of three paths is not one.
    """
    from app.services import fleet as fleet_svc

    holder = fleet_svc.bounce_pin_holder(item)
    return holder is not None and holder != agent_id


def claim_item(db: Session, item_id: str, agent_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """Claim one specific item if it's currently claimable. Used to grab a related cluster.

    **The scope gate lives here and in `claim_next`, and nowhere else** (GRPH-827). These two
    are the only writes that put an item into a holder's hands, so every path above them —
    `claim_cluster`'s members, `next_cluster`'s neighbours, the bound seat's item at
    registration — inherits the rule instead of restating it. A rule restated per tool is a
    rule that one tool will be added without. The review-only seat gate (GRPH-988) lives in
    exactly the same two places for exactly that reason.

    `OutOfScope` rather than `None`: see the class. A caller that treats the refusal as a lost
    race will loop through every candidate in the project and take none of them, which is a
    slow way to say what one sentence says.
    """
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    it = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if it is None or not _is_claimable(it, cutoff) or pinned_elsewhere(it, agent_id):
        return None
    # GRPH-1530. The cluster pool keeps a stale in_progress row even when a dependency
    # is still open (collision.clusters_for_project includes every claimable
    # in_progress item, ready or not). None, like the other "not claimable" answers:
    # the cluster loop tries the next member and still reserves the area from the one
    # it does claim. A fresh backlog/next row is not this case — claim_next and that
    # pool's ready() filter already withhold it, and refusing it here also refuses an
    # explicit claim of a named item. The seed's AL-12 depends on AL-08, which is not
    # done, and a lease test that claims it by id is not a handout.
    stale_reclaim = (
        it.status == "in_progress"
        and it.claimed_by is not None
        and it.claimed_at is not None
        and _aware(it.claimed_at) < cutoff
    )
    if stale_reclaim and unfinished_dependency_ids(db, it):
        return None
    # GRPH-988, and first among the seat checks: the seat's KIND is the one fact here that no
    # wait, release or retry can change, so it is the one worth answering before anything that
    # can. `claim_next` says the same sentence; see REVIEW_ONLY_REFUSAL.
    if review_only_seat(db, agent_id):
        raise ReviewOnlySeat(REVIEW_ONLY_REFUSAL)
    # GRPH-1089. Beside the review-only check, and for the same reason it lives in
    # `claim_next`: this is the write `claim_cluster`'s members and `next_cluster`'s
    # neighbours share. A check that exists only on `claim_next` leaves both open once the
    # bound item has left in_progress — `_holds_bound_item` stops matching when the lease is
    # released, and `open_bound_item` below returns None for review and done. Raised rather
    # than returned as None, for the reason `ReviewOnlySeat` gives: None is "not claimable".
    #
    # Passed the candidate, because the bound item itself is the one claim that must land
    # while it is `next`: registration claims it through here, and so does a seat taking it
    # back after its lease lapsed (GRPH-1101). Refusing that refuses the seat's only job. A
    # different item is the hole the bounce reproduced.
    if reason := bound_only_refusal(db, agent_id, it, lease_seconds=lease_seconds):
        raise BoundOnlySeat(reason)
    if (it.reach or "repo") == "deploy" and held_by_a_seat(db, agent_id):
        # GRPH-832. The reported escape was SELF-CLAIMED, not delegated: the ops item was the
        # highest-scored row in the project, so it sat at the top of every worker's queue and
        # a child took it on its own. A refusal that only guarded `delegate` would have missed
        # the path the incident actually used.
        raise ReachesOutsideTheRepo(
            f"{it.key} is declared `reach=deploy` — it acts on a running system, and you are "
            "a spawned child with a worktree and a shell. Leave it; a person does this one."
        )
    # GRPH-948. A seat bound to one item delivers that item on its own branch. A second claim
    # while the first is open puts two items in one worktree, and from there into one salvage
    # commit and one PR — the reported one carried four items and needed four fix PRs.
    bound = open_bound_item(db, agent_id)
    if bound and bound != it.key:
        raise BoundSeatBusy(
            f"this seat is bound to {bound}, which is not in review or done. One branch "
            f"carries one item: move {bound} to review before claiming {it.key}.")
    scope = seat_scope(db, agent_id)
    if not in_scope(it, scope):
        raise OutOfScope(
            f"{it.key} is not part of {scope}, and this seat was minted for {scope}. "
            f"The wave that provisioned you is scoped; ask it for more work rather than "
            f"taking work it did not choose."
            + ("" if it.prd_id else
               f" ({it.key} carries no PRD at all, so no scoped wave can reach it.)"))
    # PRD-62 S4: held for a runner with headroom. None, like the pin above — not a lost race.
    if routing_svc.held_back(db, it, agent_id):
        return None
    # GRPH-1647. Raised, not None: None is "lost the race" or "not claimable", and a
    # caller that treats a tier refusal as one of those retries or reports the item held.
    # The message names `needs_tier`. An undeclared tier does not raise — see `tier_block`.
    if block := tier_block(db, it, agent_id):
        raise block
    claimed = _try_claim(db, it, agent_id)
    if claimed is not None:
        routing_svc.record_claim(db, claimed, agent_id)
    return claimed


def heartbeat(db: Session, item_id: str, agent_id: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Item | None:
    """Extend the lease on a claimed item. Returns the item, or None if the agent isn't the holder.

    Also recomputes area-reservation expiry for this agent and this item (P30 D5).
    `claimed_at` alone left reservations dying at the original `lease_seconds` while
    the worker was still building. The server clock is the one that moves `expires_at`.
    """
    from app.services import fleet as fleet_svc

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None or item.claimed_by != agent_id:
        return None
    now = utcnow()
    item.claimed_at = now
    fleet_svc.extend_reservations(
        db, agent_id=agent_id, item_id=item.id, now=now, lease_seconds=lease_seconds)
    db.commit()
    db.refresh(item)
    return item


def _record_handback(db: Session, item: Item, agent_id: str, reason: str | None,
                     *, review: bool) -> None:
    """Record a hold handed back UNDECIDED, on the item (GRPH-991).

    This is the control behind "an unreadable revision is not a bounce". A reviewer whose fetch
    of the branch 404s has two verbs, and until now the manifest advertised one of them:
    `bounce`, which returns the item to `next` AND writes `outcome="bounced"` into the builder's
    cell of the preference matrix — so a supervisor that had not published the branch yet was
    charged to the vendor that built the work. GRPH-987's fleet brief tells a reviewer NOT_YET
    instead, but a sentence in a prompt is not a control: the server has to make the
    non-punitive outcome reachable and legible, or the punitive one stays the default.

    Deliberately NOT a `harness.record_review_verdict` value. `_recompute_check` treats every
    verdict that is not `bounced` as a sign-off, so a third verdict in there would certify an
    undecided review as a confirmed one — the absence reading as clean, wearing the clothes of
    the thing that removes it. Nor a `delegation.on_outcome`: an outcome CLOSES the attempt, and
    this attempt has not ended, it has been handed to somebody else.

    Written as `evidence`, not a new column: it is a receipt about the work, which is what that
    field already holds, and it is the first thing the next reviewer reads. `review_takes` is
    left alone on purpose — it counts takes without a verdict, and this is one of them.
    """
    text = (reason or "").strip()
    if not text:
        # Optional, and stays optional. A reviewer that correctly refused an item (its own work,
        # say) has no reason to give, and requiring one would send it back to the only verb that
        # does not ask — `bounce`, the punitive one. The count survives either way.
        return
    detail = f"{'NOT YET' if review else 'RELEASED'} ({agent_id}): {text}"
    if review:
        # GRPH-1830: the head and acceptance this verdict was given on, so the review queue
        # can stop re-issuing an item nobody can move on until one of them changes.
        from app.services import fleet as fleet_svc
        detail += fleet_svc.not_yet_suffix(item)
    item.evidence = append_evidence(item.evidence, [{"kind": "note", "detail": detail}])


def release_refusal(db: Session, item_id: str) -> tuple[str, str]:
    """(message, hint) when `release_item` returned None because the caller holds nothing.

    The message keeps the words ``not the lease holder``. gbfleet classifies a moved lease
    by that phrase, and a reword that drops it turns a non-event into a wave failure.

    What the old hint did not do is name anyone. ``another agent holds it`` reads as a live
    holder whether `claimed_by` is empty, points at a row that is gone, or points at an
    agent the roster already calls offline. Those are different next steps, and the
    reassuring one — someone is on it — is the one a planner takes when the child has exited
    (GRPH-1057).

    The offline word is `fleet.sweep_offline`, the predicate `requeue_offline_items` uses,
    so the hint cannot describe a holder the sweep will release as still working, or the
    reverse. A missing Agent row is not that predicate: the sweep never sees it.
    """
    from app.services import fleet as fleet_svc

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return (
            f"not the lease holder for {item_id!r}",
            "no such item; search_items for a valid id",
        )
    label = item.key
    holder = item.claimed_by or ""
    if not holder:
        review = item.review_claimed_by or ""
        if review:
            return (
                f"not the lease holder for {label!r}: no build lease; review held by {review}",
                "release_item hands a review hold back only for the agent that holds it",
            )
        return (
            f"not the lease holder for {label!r}: nobody holds it",
            "claimed_by is empty, so there is no lease to hand back. fleet_status's offline "
            "sweep and retire_wave only release a lease an agent still holds",
        )
    agent = db.get(Agent, holder)
    if agent is None:
        return (
            f"not the lease holder for {label!r}: claimed_by {holder} has no agent row",
            "the offline sweep only releases holders it can find on the roster, so "
            "fleet_status will not clear this. It needs a person to clear claimed_by",
        )
    bound = agent.enrolment_id is not None
    if fleet_svc.sweep_offline(db, agent):
        hint = (
            f"{holder} is offline. fleet_status runs the offline sweep "
            f"(requeue_offline_items), which releases items that agent holds"
        )
        if bound:
            hint += ". A planner force-releases that bound seat with retire_wave"
        return (
            f"not the lease holder for {label!r}: held by {holder}, who is offline",
            hint,
        )
    state = fleet_svc.presence_state(agent)
    if state == "quarantined":
        hint = (
            f"{holder} is quarantined. The offline sweep does not release a quarantined holder"
        )
        if bound:
            hint += ". A planner force-releases the bound seat with retire_wave"
        return (
            f"not the lease holder for {label!r}: held by {holder}, who is quarantined",
            hint,
        )
    hint = f"{holder} is {state} and holds the lease; only that agent can release_item"
    if bound:
        hint += (
            ". A planner force-releases the bound seat with retire_wave, which does not "
            "wait for the holder to go offline"
        )
    return (
        f"not the lease holder for {label!r}: held by {holder} ({state})",
        hint,
    )


def release_item(db: Session, item_id: str, agent_id: str, to_status: str = "next",
                 reason: str | None = None) -> Item | None:
    """Give a claimed item back to the queue. Returns the item, or None if not the holder.

    Also drops the item's area reservations (PRD-17 D-d). They expire lazily anyway, but an
    area held for the rest of a lease that nobody is editing is a cluster the divvy will not
    hand out — so the fleet would idle for up to ten minutes on work that had already stopped.

    `reason` is recorded on the item either way it is handed back, so the argument is never
    silently dropped — see `_record_handback`.
    """
    from app.services import fleet as fleet_svc

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None

    # A REVIEW claim is a hold too, and until now only a worker could hand one back — so a
    # reviewer that correctly refused an item (its own work, say) was stuck holding it for a
    # full lease while `claim_review` handed it the same item on every call (GRPH-429).
    # Releasing whichever hold the caller actually has keeps one release verb instead of two.
    #
    # It returns BEFORE `on_outcome` and before any verdict, which is what makes it the
    # non-punitive path (GRPH-991): the item stays in `review` for the next reviewer and
    # nothing at all is written about its builder.
    if item.review_claimed_by == agent_id and item.claimed_by != agent_id:
        item.review_claimed_by = None
        item.review_claimed_at = None
        _record_handback(db, item, agent_id, reason, review=True)
        db.commit()
        db.refresh(item)
        return item

    if item.claimed_by != agent_id:
        return None
    if to_status == "done":
        # `item.status = to_status` is not `.status = "done"`, so the original ratchet
        # missed this writer (GRPH-543 bounce). Releasing is handing work back, not
        # completing — that is update_item / sign_off.
        raise MissingAttestation(
            f"{item.key} cannot move to done via release_item: `done` needs an "
            "`attestation` receipt naming the adapter, the commit it binds to, and at "
            "least one predicate that passed — written by a key with the `gate` scope "
            "(CI, or a reviewer via sign_off), not by handing the lease back"
        )
    # Asked BEFORE the reservations are released, because releasing them destroys the
    # evidence the guard below needs (GRPH-435).
    reserved = fleet_svc.holds_reservation(db, agent_id=agent_id, item_id=item.id)
    fleet_svc.release_reservations(db, item_id=item.id)
    # An item in `review` has been handed in: the attempt is finished and waits on a verdict.
    # Handing back the build lease afterwards (gbfleet reap at child exit) must not close it as
    # `released` — `on_outcome` keeps the FIRST outcome, so that would pre-empt the `signed_off`
    # / `bounced` the reviewer writes (GRPH-1226). Only unfinished work handed back is released.
    if item.status != "review":
        from app.services import delegation as delegation_svc
        delegation_svc.on_outcome(db, item, "released")
    # AUTHORSHIP IS NOT EARNED BY CLAIMING (GRPH-434). `built_by` is written at claim and never
    # cleared, which is right — releasing a lease must not destroy the record of who made the
    # thing (GRPH-376/377). But an agent that claimed, wrote nothing and handed the item back
    # made nothing, and stamping it anyway had two costs: the item named an author who never
    # opened it, and `independent()` then barred that agent from ever REVIEWING what it
    # declined. With no way to claim a specific item, claim-and-release is the only way to see
    # what the queue holds, so the mechanism punished the only available move.
    #
    # "Wrote nothing" is decided by the clock rather than by judgement: `updated_at` moves on
    # any write to the row, so an untouched item still carries the timestamp it had when the
    # lease was taken. A single substantive write — a status move, touchpoints, evidence —
    # keeps the authorship, which is what the ban needs.
    # ...and `updated_at` is not the whole story (GRPH-435). `claim_cluster` — the primary
    # claim path since GRPH-380 made the all-in-one posture claim through the divvy — records
    # its work as AREA RESERVATIONS, rows carrying this agent and this item, written by the
    # act of taking it. They do not touch `items.updated_at`, so the clock read an untouched
    # item and cleared the authorship of an agent that had demonstrably taken the work. With
    # `built_by` NULL, `independent()` had nothing to refuse and that same agent could sign
    # the item off: a walk around the self-review ban through the ordinary claim path.
    #
    # `heartbeat` and `link_items` also leave `updated_at` alone and are deliberately NOT
    # counted. A heartbeat is about the agent's liveness rather than the item, and linking two
    # items is work but not necessarily work on this one. The reservation is different in
    # kind: the row names this agent and this item and nothing else.
    #
    # A git commit is the third place work hides (GRPH-1116). It does not touch this row, so
    # the clock still says "wrote nothing" after the sha landed, and clearing `built_by` here
    # is what lets the planner who next moves the item to review become the author. The
    # supervisor records the sha once — it may not call `update_item` — and `committed_by`
    # is that note. A note naming a different agent does not save the holder who is leaving.
    if (item.built_by == agent_id and item.claimed_at
            and item.updated_at <= item.claimed_at and not reserved
            and not committed_by(item.evidence, agent_id)):
        item.built_by = None
    # AFTER the authorship decision, never before: a handback note is a write to `evidence`, so
    # it moves `updated_at`, and the test above reads `updated_at` to decide whether this agent
    # wrote anything at all. Recording first would let an agent that claimed, typed a reason and
    # wrote no code keep the authorship — and with `built_by` set, `independent()` bars it from
    # reviewing the item it just handed back (GRPH-434/435).
    _record_handback(db, item, agent_id, reason, review=False)
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    if item.status == "in_progress" and to_status in STATUSES:
        item.status = to_status  # done refused above via MissingAttestation
    db.commit()
    db.refresh(item)
    return item
