"""Item (tracker) service — shared by REST routers and the MCP server."""
from __future__ import annotations

import logging

from datetime import timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import Item, Prd, Project, utcnow
from app.services import keys

logger = logging.getLogger(__name__)

STATUSES = ["backlog", "next", "in_progress", "review", "done", "blocked"]
FIDELITIES = ["low", "high"]  # low = specifiable now; high = needs a prototype (AL-68)
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


def get_item(db: Session, item_id: str) -> Item | None:
    return db.get(Item, keys.resolve_item(db, item_id) or item_id)


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
) -> Item:
    """Create a tracked item.

    ``commit=False`` leaves the row flushed but uncommitted so a caller can make the item
    and something that references it one transaction — `accept_request` needs the item and
    its link to land together, because an item that exists while its request still sits in
    the triage queue reads as untriaged work that is already on the board.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    if fidelity not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fidelity}")
    if db.get(Project, project_id) is None:
        raise ValueError(f"unknown project: {project_id!r}")
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
        status=status,
        sort_order=max_order + 1,
        reporter=reporter or {},
        date=date,
        prd_id=_stored_prd_id(db, prd_id, project_id),
        prd_linked_at=utcnow() if prd_id else None,
        prd_section=prd_section or "",
        fidelity=fidelity,
    )
    db.add(item)
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()  # assigns the row without ending the caller's transaction
    if item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)
    return item


_EVIDENCE_KINDS = {"test", "url", "screenshot", "health", "note", "sabotage"}

# What a `sabotage` receipt must carry to be one (GRPH-321). Without these it is a `note`
# wearing a stronger name, and a gate that counted it would be satisfied by prose.
_SABOTAGE_FIELDS = ("claim", "mutation", "tests_failed")


def normalize_evidence(raw) -> list[dict]:
    """Coerce evidence receipts to {kind, detail, url}; drop empties (AL-53).

    `kind` is advisory (test | url | screenshot | health | note) and falls back to `note`; a
    receipt with neither detail nor url is dropped.

    **`sabotage` is the one kind that is not advisory (GRPH-321).** It carries the claim under
    test, the mutation applied, and how many tests failed — and a receipt claiming to be one
    without that structure is demoted to `note` rather than accepted. A structured kind that
    accepts unstructured input is the free-text field with a new name, and anything gating on
    it would be checking a label rather than a fact.

    Graphban owns the RECEIPT, not the run. It cannot verify the mutation happened; what it
    can do is make the claim falsifiable and queryable, which is the same trade PRD-12 already
    accepts for citations.
    """
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "note").lower()
        if kind not in _EVIDENCE_KINDS:
            kind = "note"
        detail = str(e.get("detail") or "").strip()
        url = str(e.get("url") or "").strip()
        row = {"kind": kind, "detail": detail, "url": url}
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
        if not row["detail"] and not row["url"]:
            continue
        out.append(row)
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
    leaves both the error and the correction readable. If a genuine need to delete ever
    turns up, it wants an explicit destructive operation that says so — not the default
    behaviour of the verb every agent calls to add a note.

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
    """
    return any(e.get("tests_failed") for e in sabotage_receipts(evidence))


def update_item(db: Session, item_id: str, defer=None, **fields) -> Item | None:
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in STATUSES:
            raise ValueError(f"invalid status: {fields['status']}")
    if fields.get("fidelity") is not None and fields["fidelity"] not in FIDELITIES:
        raise ValueError(f"invalid fidelity: {fields['fidelity']}")
    if fields.get("effort") is not None:
        effort_update = int(fields["effort"])
        if effort_update < 0:
            raise ValueError(f"negative effort: {effort_update}")
    prev_status = item.status
    # Captured BEFORE the status moves. `intent_hold` is about work in flight and goes
    # quiet once an item is done, so asking after the transition always answers None —
    # which silently turned the completion receipt into dead code.
    hold_at_completion = (
        _pending_hold(db, item)
        if fields.get("status") == "done" and prev_status != "done" else None
    )
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
                "github_url", "assignee", "touchpoints", "prd_id", "prd_section", "fidelity"):
        if key in fields and fields[key] is not None:
            setattr(item, key, fields[key])
    if fields.get("evidence") is not None:
        # Appends. See `append_evidence` — a write here must never remove a receipt somebody
        # else left, because `sign_off` gates on the stored ones (GRPH-494).
        item.evidence = append_evidence(item.evidence, fields["evidence"])
    db.commit()
    db.refresh(item)

    if "touchpoints" in fields and fields["touchpoints"] is not None and item.touchpoints:
        from app.services.clustering import sync_code_links
        sync_code_links(db, item)

    # Work can reach `in_progress` without ever taking a lease — a human moving a card, an
    # agent that edits rather than claims. Stamping here too is what keeps the hold from
    # covering only the subset of work that happens to use the claim path.
    if item.status == "in_progress" and prev_status != "in_progress":
        stamp_baseline_at_start(db, item)

    if item.status == "done" and prev_status != "done":
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
) -> list[Item]:
    # Status/project are simple column filters (SQL); the free-text query and tag
    # matching run in Python so `query` can match a tag too and stay dialect-agnostic
    # (tags is a JSON column). The result set here is small.
    stmt = select(Item)
    if project_id:
        stmt = stmt.where(Item.project_id == project_id)
    if status:
        stmt = stmt.where(Item.status == status)
    rows = list(db.scalars(stmt.order_by(Item.sort_order.asc())).all())

    q = query.lower().strip()
    want_tags = {t.lower() for t in (tags or [])}

    def matches(it: Item) -> bool:
        item_tags = {t.lower() for t in (it.tags or [])}
        if want_tags and not (want_tags & item_tags):
            return False
        if q and q not in it.title.lower() and q not in (it.description or "").lower() and not any(
            q in t for t in item_tags
        ):
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


def get_item_details(db: Session, item_id: str) -> dict | None:
    from app.models import MemoryShard, Request

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None
    # Query by the RESOLVED id, never the caller's string. `item_id` may be any form
    # that resolves — a key rendered under the project's current tag, a retired tag, or
    # a pre-tag legacy id — while these columns hold the frozen stored id. Using the
    # caller's string made linked memory and requests silently vanish the moment a
    # project was retagged and an agent looked the item up by its new key (PRD-13).
    shards = db.scalars(select(MemoryShard).where(MemoryShard.item_id == item.id)).all()
    reqs = db.scalars(select(Request).where(Request.linked_to == item.id)).all()
    return {
        # Rendered, not stored — every other read surface renders, and this one didn't.
        "id": item.key,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "tags": item.tags,
        "effort": item.effort,
        "fidelity": item.fidelity,
        "blocker": item.blocker,
        "pr": item.pr,
        # This read builds its own dict rather than going through `_item_dict` — which is how
        # the intent hold went missing from the most important read, and how the whole bounce
        # went missing from it too (GRPH-378/379). An author reclaiming a bounced item comes
        # HERE to find out what to fix.
        "claimed_by": item.claimed_by,
        "built_by": item.built_by,
        # Who signed it off. Together with `built_by` this is where a self-review is visible:
        # equal values mean one agent was the only thing that ever looked at the work.
        "reviewed_by": item.reviewed_by,
        # The proof-on-done receipts. This read calls itself the FULL record and omitted them,
        # so an agent could not see what a completion was justified by — including the
        # danger-mode self-review note, whose entire purpose is to be visible. The web item
        # panel has rendered them all along; the agent-facing read had not.
        "evidence": item.evidence or [],
        **bounce_fields(item),
        "linked_shards": [{"id": s.id, "text": s.text, "source": s.source} for s in shards],
        "linked_requests": [{"id": r.key, "title": r.title, "type": r.type} for r in reqs],
        # In-flight invalidation (GRPH-242/312). This is the read an agent makes right
        # before starting work, so it is the one place the hold most needs to appear — and
        # it was the one place it did not: this builds its own dict rather than going
        # through `_item_dict`, so "the hold rides on every item an agent reads" was true
        # of every surface except the most important one. Absent (not null) when there is
        # no hold, so it costs nothing on the overwhelming majority of reads.
        **({"intent_hold": hold} if (hold := _pending_hold(db, item)) else {}),
    }


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
    if it.blocker:
        return False
    stale = it.claimed_by is not None and it.claimed_at is not None and _aware(it.claimed_at) < cutoff
    if stale:
        return it.status in ("next", "backlog", "in_progress")
    if it.claimed_by is None:
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
        # Fresh backlog/next must be dependency-ready; a stale in-progress reclaim is already
        # underway, so we don't re-gate it on dependencies.
        if it.status in ("backlog", "next") and not prio.ready(ctx, it):
            continue
        out.append(it)
    out.sort(key=lambda it: (-prio.score(ctx, it), it.sort_order))
    return out


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
    """
    # What the caller has already declined this call-round (GRPH-429). Releasing an item does
    # not advance the queue — the released item is top-scored again, so a claim/release loop
    # returns it forever; measured at eight for eight. An agent that cannot take the head of
    # the queue could reach nothing behind it, in either role.
    #
    # Caller-supplied rather than remembered server-side: a decline is a fact about this
    # agent's turn, not about the item, and storing it would mean deciding when it expires.
    declined = {s for s in (skip or [])}
    for cand in _ready_candidates(db, project_id, lease_seconds):
        if cand.id in declined or cand.key in declined:
            continue
        if pinned_elsewhere(cand, agent_id):
            continue
        claimed = _try_claim(db, cand, agent_id)
        if claimed is not None:
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
    stmt = stmt.values(claimed_by=agent_id, claimed_at=now, assignee=agent_id,
                       built_by=agent_id, status="in_progress", updated_at=now,
                       bounce_pinned_to=None, bounce_pinned_until=None)
    if db.execute(stmt).rowcount == 1:
        db.commit()
        # Holding a lease outranks having been dismissed: the roster must never hide work.
        from app.services import fleet as fleet_svc
        fleet_svc.restore_on_work(db, agent_id)
        db.expire_all()
        item = db.get(Item, cand.id)
        stamp_baseline_at_start(db, item)
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
    """Claim one specific item if it's currently claimable. Used to grab a related cluster."""
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    it = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if it is None or not _is_claimable(it, cutoff) or pinned_elsewhere(it, agent_id):
        return None
    return _try_claim(db, it, agent_id)


def heartbeat(db: Session, item_id: str, agent_id: str) -> Item | None:
    """Extend the lease on a claimed item. Returns the item, or None if the agent isn't the holder."""
    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None or item.claimed_by != agent_id:
        return None
    item.claimed_at = utcnow()
    db.commit()
    db.refresh(item)
    return item


def release_item(db: Session, item_id: str, agent_id: str, to_status: str = "next") -> Item | None:
    """Give a claimed item back to the queue. Returns the item, or None if not the holder.

    Also drops the item's area reservations (PRD-17 D-d). They expire lazily anyway, but an
    area held for the rest of a lease that nobody is editing is a cluster the divvy will not
    hand out — so the fleet would idle for up to ten minutes on work that had already stopped.
    """
    from app.services import fleet as fleet_svc

    item = db.get(Item, keys.resolve_item(db, item_id) or item_id)
    if item is None:
        return None

    # A REVIEW claim is a hold too, and until now only a worker could hand one back — so a
    # reviewer that correctly refused an item (its own work, say) was stuck holding it for a
    # full lease while `claim_review` handed it the same item on every call (GRPH-429).
    # Releasing whichever hold the caller actually has keeps one release verb instead of two.
    if item.review_claimed_by == agent_id and item.claimed_by != agent_id:
        item.review_claimed_by = None
        item.review_claimed_at = None
        db.commit()
        db.refresh(item)
        return item

    if item.claimed_by != agent_id:
        return None
    fleet_svc.release_reservations(db, item_id=item.id)
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
    if item.built_by == agent_id and item.claimed_at and item.updated_at <= item.claimed_at:
        item.built_by = None
    item.claimed_by = None
    item.claimed_at = None
    item.assignee = ""
    if item.status == "in_progress" and to_status in STATUSES:
        item.status = to_status
    db.commit()
    db.refresh(item)
    return item
