"""PRD tracker service (Phase 3): CRUD, version snapshots, item links, AI commands."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collections import Counter
from datetime import timezone

from app.models import (
    CodeNode, Event, GrillDimension, GrillTurn, Item, Prd, PrdVersion, Verdict,
    WorkClassification, utcnow,
)
from app.services import items as items_svc
from app import errors
from app.config import settings
from app.services import keys
from app.services import events as events_svc
from app.services import platform as platform_svc

logger = logging.getLogger("graphban.prds")

STATUSES = ["draft", "review", "approved", "closed"]
# How a section with nothing delivered may be accounted for at close (GRPH-244).
DISPOSITIONS = ("promoted", "deferred")

TEMPLATES: dict[str, str] = {
    "blank": "# {title}\n\n",
    "standard": (
        "# {title}\n\n"
        "## Overview\n\n_What is this and why does it matter?_\n\n"
        "## Goals\n- \n\n"
        "## Non-Goals\n- \n\n"
        "## Key Features\n- \n\n"
        "## Success Metrics\n- \n\n"
        "## Risks & Open Questions\n- \n"
    ),
}


def _bump(version: str) -> str:
    m = re.match(r"v(\d+)\.(\d+)", version or "v0.0")
    if not m:
        return "v0.1"
    return f"v{m.group(1)}.{int(m.group(2)) + 1}"


def _promote(version: str) -> str:
    """Approval promotes v0.x to v1.0 (AL-239). `_bump` only ever increments minor, so it
    cannot express "this stopped being a draft" — which is the whole point of the moment.
    An already-major version bumps minor instead: a re-approval is v1.1, not a second v1.0."""
    m = re.match(r"v(\d+)\.(\d+)", version or "v0.0")
    if not m:
        return "v1.0"
    major = int(m.group(1))
    return "v1.0" if major == 0 else f"v{major}.{int(m.group(2)) + 1}"


def list_prds(db: Session, project_id: str | None = None) -> list[Prd]:
    stmt = select(Prd)
    if project_id:
        stmt = stmt.where(Prd.project_id == project_id)
    return list(db.scalars(stmt.order_by(Prd.updated_at.desc())).all())


def get_prd(db: Session, prd_id: str) -> Prd | None:
    return db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)


def create_prd(
    db: Session,
    *,
    title: str,
    template: str = "standard",
    project_id: str = "core",
    body: str | None = None,
) -> Prd:
    # An imported markdown body wins over the template.
    imported = body is not None
    if imported:
        content = body
        note = "Imported from markdown."
    else:
        content = TEMPLATES.get(template, TEMPLATES["blank"]).format(title=title)
        note = "Initial draft."
    # See items.create_item: the id is frozen identity, `number` renders the key.
    prd_id, number = keys.mint(db, project_id, "prd")
    prd = Prd(id=prd_id, number=number, project_id=project_id, title=title, status="draft",
              version="v0.1", body=content, linked=[], updated="just now")
    db.add(prd)
    db.flush()
    db.add(PrdVersion(prd_id=prd.id, version="v0.1", date="just now", note=note, body=content))
    db.commit()
    db.refresh(prd)
    return prd


class ApprovalNotEarned(ValueError):
    """`approved` was set by hand instead of reached by finishing the grill (AL-300)."""


class PrdClosed(ValueError):
    """A closed PRD was edited, reopened, or rebaselined. Terminal is terminal (GRPH-244);
    post-close work becomes a successor PRD carrying a lineage link back."""


def update_prd(db: Session, prd_id: str, **fields) -> Prd | None:
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    if prd is None:
        return None
    if fields.get("status") is not None and fields["status"] not in STATUSES:
        raise ValueError(f"invalid status: {fields['status']}")
    # Terminal means terminal (GRPH-244). No edit, no reopen, no undo — not even when the
    # close itself turns out to have been wrong; that is corrected by a successor PRD and
    # the lineage shows it. The moment an exception exists the state is decorative, so
    # there is one rule. `closed` is REACHED through `close_prd`, never set here.
    if prd.close_record is not None:
        raise PrdClosed(
            f"{prd.key} is closed and cannot be edited. Post-close changes become a new "
            f"PRD linked back to this one — promote the intent rather than reopening.")
    if fields.get("status") == "closed":
        raise PrdClosed("closed is reached by closing the PRD, not by setting a status")
    # `approved` is REACHED, not set (PRD-15). Refusing here rather than in the routers
    # covers REST and MCP at once — and this call is precisely how an agent could
    # otherwise freeze an intent baseline (AL-239) that nobody had read.
    #
    # Setting it to the value it already holds is allowed: a client echoing back an
    # unchanged status is not trying to approve anything, and 422-ing that would break
    # every "save the whole object" caller for no safety gain.
    if fields.get("status") == "approved" and prd.status != "approved":
        done = completion(db, prd.id)
        raise ApprovalNotEarned(
            "approved is reached by finishing the grill, not set directly. "
            + (f"Still unanswered: {', '.join(done['outstanding'])}. "
               if done["outstanding"] else "No answers are recorded yet. ")
            + "Answer the open dimensions (or defer one explicitly) and it approves itself."
        )
    # A SECTION edit resolves to a body edit here, so every rule below still applies to the
    # result — the rebaseline check, the body_updated_at stamp, all of it. The alternative,
    # a separate write path, is how one of two writers quietly stops being governed.
    section = fields.pop("section", None)
    if section is not None:
        if fields.get("body") is None:
            raise ValueError("section edits need the new contents in `body`")
        fields["body"] = replace_section(prd.body or "", section, fields["body"])
    # Read-before-write on the DESTRUCTIVE form only (GRPH-357). A section edit cannot lose
    # what it did not read — it rewrites one span and splices the rest back verbatim — so
    # it is deliberately not gated. A full-body replace can lose everything, and did.
    base = fields.pop("base_hash", None)
    if base is not None and fields.get("body") is not None:
        current = body_hash(prd.body or "")
        if base != current:
            raise StaleBody(
                f"this PRD has changed since you read it (you saw {base}, it is now "
                f"{current}). Call get_prd again and re-apply your edit — replacing the body "
                f"from a stale read deletes whatever landed in between.")
    # Refuse the edit that introduces the violation, rather than letting the author
    # finish a whole grill and be rejected at the end (GRPH-318).
    if fields.get("body") is not None:
        added = rebaseline_added_sections(db, prd, fields["body"])
        if added:
            raise RebaselineExpandsScope(
                "a rebaseline cannot add sections: " + ", ".join(repr(a) for a in added)
                + ". Rebaselining adjusts a PRD to match reality; new scope belongs in a "
                "sub-PRD or a follow-up PRD linked back to this one."
            )
    # Stamped only when the text actually differs (GRPH-430). A caller that saves the whole
    # object every keystroke, or echoes an unchanged body back, must not read as an edit —
    # otherwise "the body has absorbed the grill" becomes true by autosave.
    if fields.get("body") is not None and fields["body"] != prd.body:
        prd.body_updated_at = utcnow()
    for key in ("title", "status", "body"):
        if fields.get(key) is not None:
            setattr(prd, key, fields[key])
    prd.updated = "just now"
    db.commit()
    db.refresh(prd)
    return prd


def create_version(db: Session, prd_id: str, note: str = "") -> Prd | None:
    """Snapshot the current body as a new version and bump the version number."""
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    if prd is None:
        return None
    prd.version = _bump(prd.version)
    db.add(PrdVersion(prd_id=prd.id, version=prd.version, date="just now",
                      note=note or "Version snapshot.", body=prd.body))
    prd.updated = "just now"
    db.commit()
    db.refresh(prd)
    return prd


def link_item(db: Session, prd_id: str, item_id: str, add: bool = True) -> Prd | None:
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    if prd is None:
        return None
    linked = list(prd.linked or [])
    if add and item_id not in linked:
        linked.append(item_id)
    elif not add and item_id in linked:
        linked.remove(item_id)
    prd.linked = linked
    db.commit()
    db.refresh(prd)
    return prd


# ---- AI commands ----
_COMMANDS = {
    "expand": "Expand the section under the cursor into 1-2 well-written paragraphs. Return only the new markdown.",
    "risks": "Generate a '## Risks & Open Questions' markdown section (3-5 bullets) for this PRD. Return only that section.",
    "summarize": "Write a 2-3 sentence executive summary of this PRD as markdown. Return only the summary.",
    "grill": (
        "You are grilling the author to sharpen this PRD before anyone builds it. Ask 5-8 relentless, "
        "specific clarifying questions that surface unstated assumptions, scope boundaries, failure "
        "modes, data shapes, and decisions still open. Strongly prefer LOW-FIDELITY questions "
        "answerable in words (routes, contracts, error behavior, acceptance criteria) over HIGH-FIDELITY "
        "ones that would need a prototype to answer. Return ONLY a markdown bullet list of questions — "
        "no preamble, no answers."
    ),
}


#: A timeout gets exactly one more go (GRPH-505).
#:
#: MEASURED, and the first diagnosis was wrong. "The PRD is too long" was falsified: 80k
#: characters answer in 57s against the same host and model, and 46k — the size that failed —
#: answered in 51s. Latency here is dominated by output generation, not input length. The
#: surviving hypothesis is contention on a shared single-GPU box where the model occupies
#: 22.3 GB, and it is a hypothesis rather than a finding.
#:
#: One retry, not a loop with backoff. The failure that was observed resolved on a manual
#: retry, and a loop turns "slow" into "hangs for five minutes" — the caller has a budget too.
#:
#: NOT a raised `llm_timeout_seconds`. 90s is ample for a warm model at any realistic PRD size,
#: so raising it would slow the detection of every genuinely broken call to paper over an
#: intermittent one.
CHAT_ATTEMPTS = 2

_TIMEOUT_SIGNS = ("timeout", "timed out")


def _is_timeout(exc: BaseException) -> bool:
    """Whether this failure is worth one more attempt.

    Matched on the exception's NAME as well as its text, because the provider layer is plain
    httpx for some vendors and an SDK for others, and importing every vendor's timeout type
    here to isinstance against would tie this function to the set of providers that exist
    today.
    """
    name = type(exc).__name__.lower()
    return any(sign in name for sign in ("timeout",)) or any(
        sign in str(exc).lower() for sign in _TIMEOUT_SIGNS
    )


def chat_with_retry(chat, *, provider: str, model: str, **kwargs) -> tuple[str, bool]:
    """Ask the model, retrying ONCE on a timeout. Returns (answer, retried).

    Raises `errors.ModelTimedOut` when both attempts time out, naming the provider, the model
    and the budget — enough for a caller to tell "try again" from "your provider is wrong",
    which is the whole defect. Anything that is not a timeout is re-raised untouched: a refused
    key does not become true by waiting.
    """
    budget = settings.llm_timeout_seconds
    last: BaseException | None = None
    for attempt in range(1, CHAT_ATTEMPTS + 1):
        try:
            return chat.chat(**kwargs), attempt > 1
        except Exception as exc:  # noqa: BLE001 — re-raised below unless it is a timeout
            if not _is_timeout(exc):
                raise
            last = exc
            logger.warning(
                "chat timed out on attempt %d/%d (provider=%s model=%s budget=%ss): %s",
                attempt, CHAT_ATTEMPTS, provider, model, budget, exc,
            )
    raise errors.ModelTimedOut(
        f"the chat model did not answer within {budget}s, twice "
        f"(provider={provider!r}, model={model!r}). This is often transient — the host may "
        "have been loading or evicting a model — so trying again may simply work. If it "
        "keeps happening, check that the provider and model are reachable.",
        hint="retry once; if it persists, check the project's chat provider settings",
    ) from last


def ai_command_detail(db: Session, prd_id: str, command: str) -> tuple[str, bool]:
    """`ai_command`, plus whether it took a second attempt (GRPH-505).

    Split rather than changing `ai_command`'s return type, because the REST router wants the
    string and only the MCP surface reports the retry. A caller seeing `retried` knows its
    slow call was contention rather than a hung server.
    """
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    if prd is None:
        raise ValueError(f"prd not found: {prd_id}")
    if command not in _COMMANDS:
        raise ValueError(f"unknown command: {command}")

    _r = platform_svc.resolve_chat(db, prd.project_id)
    provider, chat = _r.provider_id, _r.chat
    if provider == "stub":
        return _stub_command(command, prd), False

    return chat_with_retry(
        chat,
        provider=provider,
        model=getattr(chat, "model", ""),
        system="You are a precise PRD writing assistant. Return only the requested markdown snippet.",
        context=prd.body,
        question=_COMMANDS[command],
    )


def ai_command(db: Session, prd_id: str, command: str) -> str:
    return ai_command_detail(db, prd_id, command)[0]


# ---- Interactive grill mode (AL-67) ----

GRILL_CHAT_SYSTEM = (
    "You are grilling the author to sharpen a PRD before anyone builds it. Based on their "
    "latest answer and the current PRD, ask 1-3 focused clarifying questions that surface "
    "unstated assumptions, scope edges, failure modes, contracts, and open decisions. Strongly "
    "prefer LOW-FIDELITY questions answerable in words over HIGH-FIDELITY ones that need a "
    "prototype (when a question is high-fidelity, say so and suggest prototyping it). Acknowledge "
    "a decision in one line, then keep grilling. Be terse. Do NOT rewrite the PRD here — only "
    "interrogate.\n"
    # AL-298: the grill has to be able to STOP. Until PRD-15 this said "keep grilling"
    # with no terminal state, so "all questions answered" could never become true and
    # approval-by-grilling was unreachable by construction.
    "When every one of scope edges, failure modes, contracts, and open decisions has "
    "either a substantive answer or an explicit decision to defer, say so plainly and "
    "stop asking — a finished grill is a result, not a failure to think of more "
    "questions. Deferring is a legitimate answer; hand-waving is not."
)

GRILL_APPLY_SYSTEM = (
    "You are updating a PRD to fold in the decisions reached during a grilling conversation. "
    "Rewrite the FULL PRD markdown body, integrating the author's answers into the appropriate "
    "`## ` sections and preserving structure and untouched sections. Return ONLY the updated "
    "markdown PRD body — no preamble, no fences."
)


def _transcript(history: list[dict]) -> str:
    lines = []
    for m in history or []:
        who = "Author" if m.get("role") == "user" else "Grill"
        text = (m.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def grill_context(prd: Prd, history: list[dict]) -> str:
    """Light-context grounding for a grill: the PRD itself + the conversation so far.
    Deliberately does NOT pull memory/code (that's the heavy-context code-chat path)."""
    parts = [f"PRD under review — {prd.title} ({prd.status}):", prd.body or "(empty)"]
    t = _transcript(history)
    if t:
        parts += ["", "Conversation so far:", t]
    return "\n".join(parts)


# ---- server-owned grill state (AL-296 / PRD-15 D4) ---------------------------------
# PRD-15 derives approval from whether the grill is finished, so the server has to own
# the conversation rather than receive it. These functions are the whole of that
# ownership; the completion standard (AL-297) reads them and adds nothing to the store.

def grill_turns(db: Session, prd_id: str, *, since: int = 0) -> list[GrillTurn]:
    """The persisted conversation, oldest first.

    `since` is the evidence window (GRPH-322), not a pagination offset. The full
    transcript is history and is never truncated; what narrows is which turns a
    *judgement* may rest on. Default 0 is the whole thing.
    """
    stmt = select(GrillTurn).where(GrillTurn.prd_id == prd_id)
    if since:
        stmt = stmt.where(GrillTurn.seq >= since)
    return list(db.scalars(stmt.order_by(GrillTurn.seq)).all())


def grill_window(db: Session, prd_id: str) -> int:
    """The seq the current interrogation starts at — 0 until a rebaseline moves it."""
    prd = db.get(Prd, prd_id)
    return int(getattr(prd, "grill_from_seq", 0) or 0) if prd is not None else 0


def grill_history(db: Session, prd_id: str, *, since: int = 0) -> list[dict]:
    """The conversation in the `{role, text}` shape the prompts and `_transcript` use,
    so a caller can drop the client-supplied transcript entirely."""
    return [{"role": t.role, "text": t.text} for t in grill_turns(db, prd_id, since=since)]


def _already_stored(existing: list[GrillTurn], history: list[dict], window: int) -> int:
    """How many leading turns of `history` are already recorded.

    Callers send one of two things and both have to work. A client that has been in this
    conversation the whole time replays the FULL transcript; a client that joined after a
    rebaseline replays only the current interrogation, because that is the whole
    conversation as far as it knows. Deciding by length alone gets exactly one of them
    right — measuring against the full transcript silently drops every answer of the
    second (GRPH-322 by a second route), and measuring against the window duplicates the
    entire history of the first.

    With no window there is no ambiguity, so the original positional rule stands
    untouched: everything stored is already stored. That path stays deliberately
    forgiving about a divergent prefix — a client's copy of a streamed agent reply is
    routinely not byte-identical to what was saved, and refusing its genuinely new answer
    over a paraphrase would lose the thing that matters to keep the thing that does not.

    Only once a window exists do the two readings diverge, and there content decides it:
    anchor at the start of the transcript, else at the start of the window. A replay is
    recognised because its prefix matches what is stored; anything unrecognised is treated
    as material for the current round, because after a rebaseline that is what it almost
    certainly is, and the costly mistake here is dropping a new answer rather than
    recording a stray one.
    """
    if not window:
        return len(existing)
    texts = [(m.get("text") or "").strip() for m in history]
    stored_texts = [t.text for t in existing]
    n = len(existing)
    for start in (0, window):
        already = n - start
        if 0 <= already <= len(texts) and texts[:already] == stored_texts[start:n]:
            return already
    return max(n - window, 0)


def record_grill_turns(
    db: Session, prd_id: str, history: list[dict],
    *, via: str = "", actor: str = "",
) -> int:
    """Append whatever part of `history` isn't recorded yet. Returns how many landed.

    The client posts the FULL transcript every round, so this appends only the suffix
    beyond what's stored — otherwise each round would duplicate every earlier one.

    Deliberately does NOT reconcile a divergent prefix. If a caller sends a shorter or
    edited history (a second tab, a lost session), the stored rounds stand and nothing
    is appended. Rewriting history to match the most recent caller would let a client
    silently erase answers that approval is derived from, which is a worse failure than
    a transcript that lags a confused client.
    """
    existing = grill_turns(db, prd_id)
    total = len(existing)
    stored = _already_stored(existing, history, grill_window(db, prd_id))
    added = 0
    for offset, message in enumerate(history[stored:]):
        text = (message.get("text") or "").strip()
        if not text:
            continue  # an empty turn is not a question and not an answer
        role = "user" if message.get("role") == "user" else "agent"
        db.add(GrillTurn(
            prd_id=prd_id,
            seq=total + offset,
            role=role,
            text=text,
            # Only an ANSWER has a supplier; a question comes from the grill itself.
            via=via if role == "user" else "",
            actor=actor if role == "user" else "",
        ))
        added += 1
    if added:
        db.commit()
    return added


# ---- the completion standard (AL-297 / PRD-15 D1) -----------------------------------
# What "the grill ran out of objections" means. Fixed and named, because if it were left
# to whatever chat model is configured then `approved` would denote something different
# on every instance and PRD-12's baselines would stop being comparable to each other.
#
# The four dimensions are the ones GRILL_CHAT_SYSTEM already asks about, so this codifies
# existing behaviour rather than inventing a checklist.
DIMENSIONS: dict[str, str] = {
    "scope_edges": "What is explicitly out of scope for the first version?",
    "failure_modes": "What happens on the failure path — bad input, missing data, timeout?",
    "contracts": "What is the exact shape of the inputs and outputs at the boundary?",
    "open_decisions": "Which decisions are still open, and which need a prototype to settle?",
}

# `deferred` completes rather than blocks. Authors deferring is normal and healthy; the
# failure this standard exists to catch is an IMPLICIT non-answer being counted as an
# answer, which is precisely what having a separate name for deferral makes visible.
OUTCOMES = ("resolved", "deferred", "unanswered")
_BLOCKING = "unanswered"


def set_dimension(
    db: Session, prd_id: str, dimension: str, outcome: str,
    *, note: str = "", turn_seq: int | None = None, graded_by: str = "",
) -> GrillDimension:
    """Record one dimension's outcome. Idempotent per (prd, dimension) — a later round
    revises the verdict rather than stacking a second one."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown grill dimension: {dimension!r} (expected {sorted(DIMENSIONS)})")
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown grill outcome: {outcome!r} (expected {list(OUTCOMES)})")
    row = db.scalar(
        select(GrillDimension).where(
            GrillDimension.prd_id == prd_id, GrillDimension.dimension == dimension
        )
    )
    if row is None:
        row = GrillDimension(prd_id=prd_id, dimension=dimension, outcome=outcome)
        db.add(row)
    row.outcome = outcome
    row.note = note
    row.turn_seq = turn_seq
    row.graded_by = graded_by
    db.commit()
    db.refresh(row)
    return row


def completion(db: Session, prd_id: str, *, graded: bool = True, ungraded_reason: str = "") -> dict:
    """Is this PRD's grill finished, and if not, what is outstanding?

    Two rules, both deliberate:

    - **Completion is zero `unanswered`.** Deferrals do not block.
    - **A grill with no recorded answers is never complete**, whatever any model claims.
      Without this floor an empty conversation could be graded straight to approved,
      which is the one outcome that would make the whole standard theatre.

    `graded=False` says THIS ROUND WAS NOT GRADED — the grader was unreachable or returned
    something unusable — and the outcomes below are therefore the previous round's, not a
    verdict on the answer just given. Before GRPH-485 that case returned this payload
    unchanged, so "the grader is down" and "your answer was too thin" were the same
    response: an author answered three times against a chat model whose name did not exist
    on the host, was told `outstanding` each time, and nothing moved for an hour.
    """
    rows = {
        d.dimension: d
        for d in db.scalars(select(GrillDimension).where(GrillDimension.prd_id == prd_id)).all()
    }
    dimensions = {
        name: {
            "outcome": rows[name].outcome if name in rows else _BLOCKING,
            "note": rows[name].note if name in rows else "",
            "turn_seq": rows[name].turn_seq if name in rows else None,
            "graded_by": rows[name].graded_by if name in rows else "",
            "question": prompt,
        }
        for name, prompt in DIMENSIONS.items()
    }
    outstanding = sorted(n for n, d in dimensions.items() if d["outcome"] == _BLOCKING)
    # Windowed (GRPH-322). The floor asks "has this interrogation been answered at all",
    # so a rebaseline with nothing new must read as zero — counting the previous grill's
    # answers is what let a rebaseline complete on history.
    answered = db.scalar(
        select(func.count()).select_from(GrillTurn).where(
            GrillTurn.prd_id == prd_id, GrillTurn.role == "user",
            GrillTurn.seq >= grill_window(db, prd_id),
        )
    ) or 0
    return {
        "dimensions": dimensions,
        "outstanding": outstanding,
        "deferred": sorted(n for n, d in dimensions.items() if d["outcome"] == "deferred"),
        "answers": answered,
        "complete": bool(answered) and not outstanding,
        # FALSE means the outcomes above are stale — the grader could not be asked this
        # round, so nothing was re-judged. A caller that cannot tell this from a thin
        # answer will keep answering into a void (GRPH-485).
        "graded": graded,
        "ungraded_reason": ungraded_reason,
    }


# ---- concluding the grill (AL-298 / PRD-15 D2) ---------------------------------------
# GRILL_CHAT_SYSTEM tells the model to "keep grilling", so the conversation could never
# end and "all questions answered" was never true. Two paths make it terminable.
#
# The classification is a SEPARATE call from the streamed conversation, not JSON smuggled
# into it. Streaming is for the author to read; classifying is state approval derives
# from, and mixing them would make a malformed token both a broken sentence and a lost
# outcome. Mirrors `memory._llm_judge`: focused prompt, defensive parse, None on failure.
# Built FROM `DIMENSIONS` rather than restating them. The first version of this prompt
# after the citation change described "four fixed dimensions" without naming any of them,
# so the model invented its own from the PRD's subject matter — `intent_baseline`,
# `coverage` — none of which matched, every verdict was discarded, and grading silently
# fell back to the stub. Deriving the list means the prompt cannot drift from the
# standard it is grading against; a test asserts every dimension appears.
GRILL_CLASSIFY_SYSTEM = (
    "You assess whether the AUTHOR HAS BEEN INTERROGATED on exactly these four "
    "dimensions, and no others:\n"
    + "".join(f"  - {name}: {question}\n" for name, question in DIMENSIONS.items())
    + "\nUse these exact keys. Do not invent dimensions from the document's subject "
    "matter.\n\n"
    "CRITICAL: the PRD's own text is the thing under question. It is NEVER evidence that "
    "a question was answered. However thoroughly the document covers a dimension, only "
    "something the AUTHOR SAID in the conversation can resolve it. A well-written PRD "
    "with no answers is `unanswered` on all four.\n\n"
    "For EACH dimension decide: `resolved` (an author answer substantively settled it), "
    "`deferred` (the author deliberately chose not to decide yet — legitimate), or "
    "`unanswered` (never put to them, answered evasively, or addressed only by the "
    "document itself). Hand-waving or 'we'll figure it out later' without explicitly "
    "choosing to defer is `unanswered`.\n\n"
    "For `resolved` and `deferred` you MUST name WHICH author answer settled it, by its "
    "number. If you cannot point at a specific answer, the honest verdict is "
    "`unanswered`.\n\n"
    "Respond with ONLY a compact JSON object whose keys are the four names above, each "
    'value {"outcome": "...", "note": "one short sentence saying what that answer '
    'settled", "answered_by": <answer number>}. Omit answered_by for `unanswered`.'
)


def _numbered_answers(history: list[dict]) -> list[str]:
    """The author's turns, in order. Citations index into this list (1-based)."""
    return [m["text"] for m in history if m.get("role") == "user" and (m.get("text") or "").strip()]


def _classify_context(prd: Prd, history: list[dict]) -> str:
    """Context for classification, which is NOT the context for conversation.

    `grill_context` leads with the PRD body, and that is exactly what went wrong the first
    time this ran for real: the model read a thorough document and reported that the
    author had explained failure modes and contracts, when the author had said nothing
    about either. It graded the artifact instead of the interrogation.

    So the body is labelled here as the thing under question, and the author's answers are
    presented as the only admissible evidence — numbered, so a verdict has to point at
    one."""
    answers = _numbered_answers(history)
    parts = [
        f"PRD UNDER QUESTION — {prd.title}. This document is what is being interrogated. "
        "It is NOT evidence that any question was answered.",
        prd.body or "(empty)",
        "",
        "AUTHOR'S ANSWERS — the only thing that can resolve a dimension:",
    ]
    parts += [f"[answer {i}] {a}" for i, a in enumerate(answers, 1)] or ["(none — the author has not answered anything)"]
    return "\n".join(parts)


def _validated(entry: dict, answers: list[str]) -> dict | None:
    """Accept a verdict only if it points at an author answer that exists.

    That single check buys the property this floor was built for: a model crediting the
    PRD's own prose cannot satisfy it, because the document is not an answer.

    It used to demand a verbatim quote as well. That was dropped — three separate bugs
    came from the validator disagreeing with how models render text (elided middles,
    added terminal stops, non-breaking hyphens for "-"), each rejecting a CORRECT verdict,
    and "text a model produces when quoting" is not a closed set. The quote never caught
    the failure people assume it does either: misattribution survives it, because a real
    quote can be filed under the wrong dimension.

    What replaces it is the note plus the answer number, which a reviewer reads against
    the answer itself. Same reviewability, no string matching.
    """
    outcome = str(entry.get("outcome", "")).strip().lower()
    if outcome not in OUTCOMES:
        return None
    note = str(entry.get("note", "")).strip()
    if outcome == "unanswered":
        return {"outcome": "unanswered", "note": note}

    try:
        idx = int(entry.get("answered_by"))
    except (TypeError, ValueError):
        return {"outcome": "unanswered", "note": "no answer cited for this dimension"}
    if not 1 <= idx <= len(answers):
        return {"outcome": "unanswered", "note": f"cited answer {idx} does not exist"}

    return {"outcome": outcome, "note": note, "answered_by": idx}


def _classify_dimensions(db: Session, prd: Prd, history: list[dict]) -> dict | None:
    """Ask the project's chat model to grade the four dimensions. Returns
    {dimension: {outcome, note, ...}}, or None when no real model is configured or the
    reply can't be parsed — the caller then falls back to the stub rule rather than
    guessing."""
    _r = platform_svc.resolve_chat(db, prd.project_id)
    provider, chat = _r.provider_id, _r.chat
    if provider == "stub":
        return None
    answers = _numbered_answers(history)
    if not answers:
        return None  # nothing citable exists; the floor in `completion` catches this too
    try:
        raw = chat.chat(
            system=GRILL_CLASSIFY_SYSTEM,
            context=_classify_context(prd, history),
            question="Classify the four dimensions. Name the answer number that settled "
                     "each one. Return only the JSON object.",
            # Deterministic: an identical transcript must yield an identical verdict.
            # Measured before this was set — three runs of the same input on the same
            # model gave two different completion states, so whether a PRD approved
            # depended on when the classifier happened to run.
            temperature=0,
        )
    except Exception:  # noqa: BLE001 — a model outage must not break the grill
        logger.exception("grill classify: chat call failed")
        return None
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, dict] = {}
    for name in DIMENSIONS:
        entry = data.get(name)
        if not isinstance(entry, dict):
            continue
        verdict = _validated(entry, answers)
        if verdict is not None:
            out[name] = verdict
    return out or None


def _grader_id(db: Session, prd: Prd) -> str:
    """Which provider is standing behind these verdicts."""
    try:
        return platform_svc.resolve_chat(db, prd.project_id).provider_id or "stub"
    except Exception:  # noqa: BLE001 — provenance must never break a grill
        return "unknown"


def _stub_classification(answers: int) -> dict:
    """The offline bar, and it is deliberately mechanical: the first `answers` dimensions
    count as resolved, in order.

    A stub cannot assess substance, so pretending it can would be worse than admitting
    it doesn't. The alternative — leaving the stub unable to conclude — would mean no PRD
    could ever be approved on the shipped default configuration, which breaks the
    zero-browser install. AL-299 records that the stub set the bar, so a reader can see
    which standard was actually applied.
    """
    names = list(DIMENSIONS)
    return {
        # `answered_by` is 1-based into the answers, exactly like a real verdict's. The
        # mapping is not a guess: the rule IS "the first `answers` dimensions, in order",
        # so dimension i was settled by answer i. Emitting it keeps one citation path for
        # both graders instead of leaving the stub with a pointer to nowhere.
        name: {"outcome": "resolved", "answered_by": i + 1,
               "note": "stub: answer recorded, substance not assessed"}
        for i, name in enumerate(names[:answers])
    }


def _cited_turn_seq(answer_turns: list[GrillTurn], cited: int | None) -> int | None:
    """The global `GrillTurn.seq` of the answer a verdict cites, or None.

    `answered_by` is 1-based into the *answers of the current window*; `GrillTurn.seq` is
    absolute across the whole transcript. Storing the former in a field named for the
    latter is what made every dimension appear to cite the same turn (GRPH-323).

    None on anything unmappable — an unanswered verdict, or an index outside the list.
    A pointer that resolves to the wrong turn is worse than no pointer, because it reads
    as provenance and a reader has no way to tell it apart from a real one.
    """
    if not cited or not (1 <= cited <= len(answer_turns)):
        return None
    return answer_turns[cited - 1].seq


def classify_grill(db: Session, prd: Prd) -> dict:
    """Grade the grill so far and record the outcomes. Returns the completion payload.

    Never downgrades an explicit `deferred` — an author's decision to leave something
    open is theirs, and a later round should not quietly convert it back into an open
    question just because the model didn't see the deferral restated."""
    # Only this interrogation's turns are admissible evidence (GRPH-322). The transcript
    # before the window is still history and still readable; it just cannot grade a spec
    # it was never shown.
    history = grill_history(db, prd.id, since=grill_window(db, prd.id))
    answers = sum(1 for t in history if t["role"] == "user")
    verdicts = _classify_dimensions(db, prd, history)
    if verdicts is None:
        # No usable verdict. The stub's mechanical rule applies ONLY when the stub is
        # genuinely what this instance has — falling back to it after a REAL model failed
        # would quietly grade with a weaker bar than the one that just refused to answer,
        # and stamp `graded_by=stub` on a project that pays for a model. Nothing is
        # recorded; the next round tries again.
        if _grader_id(db, prd) != "stub":
            logger.warning("grill classify: unusable verdict for %s; leaving dimensions "
                           "unchanged rather than applying the offline bar", prd.id)
            return completion(
                db, prd.id, graded=False,
                ungraded_reason=(
                    f"the {_grader_id(db, prd)} grader could not be asked, or returned "
                    "something unusable. The outcomes below are the previous round's — "
                    "this answer has NOT been judged. Check the project's chat model "
                    "before answering again."
                ),
            )
        graded, grader = _stub_classification(answers), "stub"
    else:
        graded, grader = verdicts, _grader_id(db, prd)

    existing = {
        d.dimension: d.outcome
        for d in db.scalars(select(GrillDimension).where(GrillDimension.prd_id == prd.id)).all()
    }
    # The answers a citation indexes into, as TURNS — so `answered_by` can be resolved to
    # a real `GrillTurn.seq` rather than left as prose in a note (GRPH-323). Filtered the
    # same way `_numbered_answers` filters the prompt's list; if the two ever disagree the
    # mapping below refuses rather than pointing at the wrong turn.
    answer_turns = [
        t for t in grill_turns(db, prd.id, since=grill_window(db, prd.id))
        if t.role == "user" and (t.text or "").strip()
    ]
    for name, verdict in graded.items():
        if existing.get(name) == "deferred" and verdict["outcome"] != "deferred":
            continue
        note = verdict.get("note", "")
        cited = verdict.get("answered_by")
        if cited:
            note = f"{note} — from answer {cited}"
        set_dimension(db, prd.id, name, verdict["outcome"],
                      note=note, turn_seq=_cited_turn_seq(answer_turns, cited),
                      graded_by=grader)
    # Approval is a consequence of the grill, so it lands here rather than waiting for
    # someone to notice the standard is met (AL-300).
    sync_status(db, prd)
    return completion(db, prd.id)


def section_digest(body: str) -> dict[str, str]:
    """`{section title: sha256 of its body}` — the unit invalidation is scoped to.

    Per section, not per body, because whole-body hashing would invalidate every
    classification beneath a typo fix. That makes the correct behaviour painful and the
    wrong one convenient, which is how a feature gets routed around.

    Whitespace is normalised so reflowing a paragraph is not a content change. Nothing
    else is: wording IS the intent, and a spec that changed its words changed.
    """
    return {
        title: hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
        for title, text in section_bodies(body).items()
    }


def diff_sections(old_body: str, new_body: str) -> dict:
    """What changed between two specs, at section granularity.

    Returns `{unchanged, modified, renamed, added, removed}`. `renamed` carries
    `(old_title, new_title)` pairs.

    Rename detection is the whole point. If identity were the title, renaming a section
    would invalidate every classification beneath it AND read as **dropped + added** in
    the close report — handing a PM a false "this was dropped" entry in the one artifact
    they are meant to act on. So a body whose hash survives under a different title is a
    rename, and its classifications survive with it.

    The ambiguity this cannot escape: a section that was BOTH renamed and edited is
    indistinguishable from a drop plus an add, because nothing anchors it. It is reported
    as removed + added, which is the honest reading — pretending to match it would be
    guessing, and a wrong guess hides a real dropped section. AL-240's fallback of stable
    per-section IDs assigned at baseline time is what closes that, and it is only worth
    building if this proves insufficient in practice.
    """
    old, new = section_digest(old_body), section_digest(new_body)
    old_by_hash = {h: t for t, h in old.items()}

    unchanged = sorted(t for t in new if t in old and old[t] == new[t])
    modified = sorted(t for t in new if t in old and old[t] != new[t])

    renamed, added = [], []
    for title in sorted(set(new) - set(old)):
        prior = old_by_hash.get(new[title])
        if prior is not None and prior not in new:
            renamed.append((prior, title))   # same body, new title
        else:
            added.append(title)

    renamed_from = {a for a, _ in renamed}
    removed = sorted(set(old) - set(new) - renamed_from)
    return {"unchanged": unchanged, "modified": modified,
            "renamed": renamed, "added": added, "removed": removed}


def intent_diff(db: Session, prd: Prd) -> dict:
    """What a rebaseline would actually change, section by section (GRPH-317).

    PRD-12 is blunt about why this exists: without it "the human ratifies a decision
    already made in chat without seeing its effect on the spec, and it is rubber-stamping
    with an audit trail." The approver has to see the change, not be told there is one.

    Per SECTION, not whole-body, for the same reason baselines hash that way: a
    whole-body diff drowns a real scope change in the noise of a typo fix, and a reader
    who has to hunt for the important line will stop reading. Only sections that actually
    changed carry line detail; unchanged ones are named and left alone.

    `governed: False` when there is no baseline — nothing to diff against, which is a
    different statement from "nothing changed".
    """
    base = baseline_of(db, prd.id)
    if base is None:
        return {"governed": False, "baseline_version": None, "sections": [], "pending": None}

    d = diff_sections(base.body, prd.body)
    old_bodies, new_bodies = section_bodies(base.body), section_bodies(prd.body)
    renamed_from = {new: old for old, new in d["renamed"]}

    sections = []
    for title in parse_sections(prd.body):
        if title in d["modified"]:
            state = "modified"
        elif title in renamed_from:
            state = "renamed"
        elif title in d["added"]:
            state = "added"
        else:
            state = "unchanged"
        entry = {"title": title, "state": state}
        if state == "renamed":
            entry["was"] = renamed_from[title]
        if state in ("modified", "added"):
            before = old_bodies.get(renamed_from.get(title, title), "") if state == "modified" else ""
            entry["lines"] = _line_diff(before, new_bodies.get(title, ""))
        sections.append(entry)

    # A removed section has no place in the new body's order, so it is appended — but it
    # carries its old text, because "this was deleted" is the single most consequential
    # thing an approver can miss.
    for title in d["removed"]:
        sections.append({"title": title, "state": "removed",
                         "lines": _line_diff(old_bodies.get(title, ""), "")})

    return {
        "governed": True,
        "baseline_version": base.version,
        "pending": prd.pending_rebaseline,
        "sections": sections,
        "changed": len(d["modified"]) + len(d["added"]) + len(d["removed"]) + len(d["renamed"]),
    }


def _line_diff(before: str, after: str) -> list[dict]:
    """Line-level changes, as `{op, text}` with op in `+ - =`.

    Context lines are kept so a change reads in place rather than as a pile of orphaned
    additions. Nothing is truncated: a diff that hides part of the change is worse than
    a long one, because the reader believes they have seen everything."""
    import difflib

    out = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        op, text = line[:2], line[2:]
        if op == "+ ":
            out.append({"op": "+", "text": text})
        elif op == "- ":
            out.append({"op": "-", "text": text})
        elif op == "  ":
            out.append({"op": "=", "text": text})
        # "? " hint lines are difflib's intra-line markers — noise for a human reader.
    return out


def baseline_drift(db: Session, prd: Prd) -> dict:
    """How the living body has diverged from the GOVERNING baseline.

    This is the mechanical half of drift and the half that works with no chat provider
    at all: it counts structural change against agreed intent and expresses no opinion
    about whether that change was good.

    A PRD with no baseline returns `governed: False` rather than a zero. Zero drift and
    "never had agreed intent to drift from" are different facts, and reporting the second
    as the first is exactly the misleading green PRD-12 exists to stop.
    """
    base = baseline_of(db, prd.id)
    if base is None:
        return {"governed": False, "baseline_version": None}
    d = diff_sections(base.body, prd.body)
    return {
        "governed": True,
        "baseline_version": base.version,
        **d,
        # Renames are deliberately NOT counted: the intent did not move, only its label.
        "drifted_sections": len(d["modified"]) + len(d["added"]) + len(d["removed"]),
    }


# What a section's linked work adds up to, mechanically. Ordered worst-first so a caller
# sorting by this reads the problems at the top.
_ABSENT, _UNDELIVERED, _PARTIAL, _DELIVERED = "absent", "undelivered", "partial", "delivered"


def completeness(db: Session, prd: Prd) -> dict:
    """What the governing baseline demands that has nothing delivered against it (GRPH-251).

    The direction is the point. Classifying work that exists can only ever find drift and
    stowaway scope; it can never surface work that was never done. "Is this PRD complete"
    is entirely a question about what is missing, so only this pass can close one.

    **The unit of intent is the section** (GRPH-313). That choice buys rename detection
    for free — a retitled section keeps its identity through `diff_sections`, so work
    linked under either title still counts, and a rename never manufactures a false
    absence. Anything finer would need its own identity scheme, which AL-240 deliberately
    left unbuilt.

    Four things this deliberately does NOT do:

    - **It does not read `prd.body`.** `coverage` does, correctly, because it answers
      "is the spec decomposed". This answers "was the agreed thing delivered", and
      measuring delivery against a spec that moves is how drift becomes definitionally
      zero. Sections the body has since added are not intent; sections the body has since
      dropped still are.
    - **It does not judge sufficiency.** A linked, `done` item counts as delivered. Whether
      the work actually satisfies what the section asked is the agent auditor's call
      (GRPH-252) — it has the repo. Conflating the two would put an opinion behind a
      mechanical count, which the baseline forbids in as many words.
    - **It does not emit a percentage.** A single green number is precisely what the PRD
      says must never be rendered as "PRD complete", and a ratio over sections would
      weight a one-line section equally with a ten-bullet one.
    - **It does not drop framing sections.** `_PROSE_SECTIONS` stays as-is for
      decomposition (a stated non-goal), but PRD-12's third named problem is that the
      section defining "done" is *structurally exempt from every check*. So framing
      sections are reported, flagged `framing`, and excluded only from the absence
      rollups — visible to a reader, never demanded of.

    Absence and non-delivery are kept apart. "Nothing was ever planned here" and "work was
    planned and has not shipped" are different failures with different owners, and merging
    them into one red count would tell a PM nothing about which they have.
    """
    base = baseline_of(db, prd.id)
    if base is None:
        # Same contract as `baseline_drift`: never a zero. "Complete" and "never had
        # agreed intent to be complete against" are different facts.
        return {"governed": False, "baseline_version": None, "sections": [],
                "absent": [], "undelivered": [], "outside_baseline": []}

    # A section renamed since the baseline is the SAME intent, so work filed under either
    # title belongs to it. Without this every rename would invent an absence.
    renames = {old: new for old, new in diff_sections(base.body, prd.body)["renamed"]}

    items = [it for it in items_svc.list_items(db, project_id=prd.project_id)
             if it.prd_id == prd.id]
    by_section: dict[str, list] = {}
    for it in items:
        by_section.setdefault(it.prd_section or "", []).append(it)

    per, claimed = [], set()
    for title in parse_sections(base.body):
        aliases = [title] + ([renames[title]] if title in renames else [])
        its = [it for a in aliases for it in by_section.get(a, [])]
        claimed.update(aliases)
        delivered = sum(1 for it in its if it.status == "done")
        if not its:
            state = _ABSENT
        elif delivered == len(its):
            state = _DELIVERED
        elif delivered:
            state = _PARTIAL
        else:
            state = _UNDELIVERED
        per.append({
            "section": title,
            "renamed_to": renames.get(title),
            "framing": not is_implementable_section(title),
            "state": state,
            "planned": len(its),
            "delivered": delivered,
            "items": [{"id": it.key, "status": it.status} for it in its],
        })

    demanded = [p for p in per if not p["framing"]]
    return {
        "governed": True,
        "baseline_version": base.version,
        "sections": per,
        # The success criterion, in the shape it was written: "absence is a first-class
        # finding, not an empty list."
        "absent": [p["section"] for p in demanded if p["state"] == _ABSENT],
        "undelivered": [p["section"] for p in demanded if p["state"] == _UNDELIVERED],
        # Work filed against a section the baseline does not contain. Not scope-drift
        # analysis (GRPH-243) — just a refusal to silently discard items, which is exactly
        # how GRPH-319 hid a third of this PRD's own work.
        "outside_baseline": sorted(s for s in by_section if s and s not in claimed),
        "demanding_sections": len(demanded),
    }


# What a verdict is allowed to point at (GRPH-314). `code` was the only form PRD-12 v1.0
# defined, which made the output of the component it names authoritative on COMPLETENESS
# definitionally malformed under its own validator: missing work has no path and no
# symbol, so an absence finding could never cite anything.
CITATION_KINDS = ("code", "intent", "evidence")


def validate_citation(db: Session, prd: Prd, citation: dict) -> tuple[bool, str]:
    """Whether a verdict's citation resolves to something real. `(ok, reason)`.

    PRD-12 requires that a verdict citing nothing is rejected as malformed, and that every
    citation resolve. That is what makes a verdict falsifiable rather than trustworthy —
    the server cannot check whether code is CORRECT, but it can check that what was cited
    exists, and a claim that can be checked at all is the achievable upgrade.

    Three forms, because one was never enough:

    - `code` — a path or symbol in the project's code graph. The original form.
    - `intent` — a section of the governing baseline. This is what an ABSENCE finding
      cites: "nothing was delivered against § Judging" points at the intent, not at code
      that by definition does not exist. Still falsifiable, since the section must resolve
      in the baseline; just not against the graph.
    - `evidence` — a receipt already carried by a completed item. `Item.evidence` accepts
      `test`, `url`, `screenshot`, `health` and `note`, so a code-graph-only rule rejected
      valid proof and skewed verdicts toward code-shaped work — a documentation or
      infrastructure item could never be signed off.
    """
    kind = (citation or {}).get("kind")
    ref = str((citation or {}).get("ref") or "").strip()
    if kind not in CITATION_KINDS:
        return False, f"unknown citation kind: {kind!r}"
    if not ref:
        return False, "citation names nothing"

    if kind == "code":
        found = db.scalar(select(CodeNode).where(CodeNode.project_id == prd.project_id,
                                                 CodeNode.path == ref))
        return (True, "") if found else (False, f"no such code node: {ref}")

    if kind == "intent":
        base = baseline_of(db, prd.id)
        if base is None:
            return False, "the PRD has no baseline to cite intent from"
        if ref in parse_sections(base.body):
            return True, ""
        # A section renamed since the baseline is the same intent; refusing its baseline
        # title would make a rename invalidate every absence finding beneath it.
        renamed = {new: old for old, new in diff_sections(base.body, prd.body)["renamed"]}
        if ref in renamed:
            return True, ""
        return False, f"no such section in baseline {base.version}: {ref}"

    item = items_svc.get_item(db, ref)
    if item is None:
        return False, f"no such item: {ref}"
    if not (item.evidence or []):
        return False, f"{item.key} carries no evidence to cite"
    return True, ""


def validate_verdict(db: Session, prd: Prd, citations: list[dict]) -> dict:
    """A verdict is well-formed only if it cites, and every citation resolves.

    Rejecting an EMPTY citation list is the load-bearing half. A verdict that points at
    nothing cannot be argued with, and one that cannot be argued with is not evidence —
    it is an assertion wearing evidence's clothes.
    """
    if not citations:
        return {"ok": False, "problems": ["a verdict must cite something"], "checked": 0}
    problems = []
    for c in citations:
        ok, why = validate_citation(db, prd, c)
        if not ok:
            problems.append(why)
    return {"ok": not problems, "problems": problems, "checked": len(citations)}


# Which receipt kinds can be checked by someone other than their author (GRPH-250).
# PRD-12 names `test`, `health` and `url` as falsifiable and `note` as not. `screenshot`
# it does not name, and it lands here as unfalsifiable: nothing can re-run or re-fetch it,
# so it is exactly as easy to produce as a note claiming the same thing. That is a call
# made here rather than in the baseline, which is why it is written down.
FALSIFIABLE_EVIDENCE = ("test", "health", "url")
UNFALSIFIABLE_EVIDENCE = ("note", "screenshot")


def _touchpoint_hits(paths: set[str], touchpoint: str) -> int:
    """Code-graph nodes that actually sit at `touchpoint`.

    Deliberately stricter than `clustering._match`, which also relates two touchpoints
    that merely share a directory. That looseness is right for "are these items related"
    and wrong here: a sibling file appearing in the same folder is not evidence that the
    code an item promised was written. Exact path, a symbol beneath it, or an explicit
    glob — nothing weaker.
    """
    tp = (touchpoint or "").strip()
    if not tp:
        return 0
    if "*" in tp:
        return sum(1 for p in paths if fnmatch.fnmatch(p, tp))
    return sum(1 for p in paths if p == tp or p.startswith(f"{tp}::"))


def evidence_rollup(db: Session, prd: Prd) -> dict:
    """What the delivered work actually offers as proof, bound to the intent it supports
    (GRPH-250).

    Two independent signals, kept apart because they fail differently.

    **Receipts**, aggregated per baselined section. Split by whether anyone but their
    author could check them: a `test` or a `health` result or a `url` can be re-run or
    re-fetched; a free-text `note` is as easy to fabricate as the description it sits
    next to. Both are reported — the split is the finding, not a filter.

    **Structural corroboration**, which needs no model and no author cooperation: did code
    actually appear where the item said it would? Comparing `touchpoints` against the code
    graph is not proof — an agent can write a file and still not have done the work — but
    it is not self-attestation either, and both halves already exist.

    Three things it refuses to do:

    - **No score.** A weighted number here would be an opinion wearing a measurement's
      clothes, which PRD-12 forbids in as many words.
    - **No silent pass for missing touchpoints.** An item that claimed nothing is
      `unknown`, never `uncorroborated`. Treating "made no claim" as "claim unmet" would
      punish honesty, and treating it as met would reward saying nothing.
    - **No collapsing of `unsupported`.** A section whose delivered work carries only
      unfalsifiable receipts is named, because that is the case a reader most needs and
      is exactly what a total would hide.
    """
    base = baseline_of(db, prd.id)
    if base is None:
        return {"governed": False, "baseline_version": None, "sections": [],
                "unsupported": [], "uncorroborated": []}

    renames = {old: new for old, new in diff_sections(base.body, prd.body)["renamed"]}
    items = [it for it in items_svc.list_items(db, project_id=prd.project_id)
             if it.prd_id == prd.id]
    by_section: dict[str, list] = {}
    for it in items:
        by_section.setdefault(it.prd_section or "", []).append(it)

    paths = {
        p for (p,) in db.execute(
            select(CodeNode.path).where(CodeNode.project_id == prd.project_id)
        ).all()
    }

    sections, unsupported, uncorroborated = [], [], []
    for title in parse_sections(base.body):
        aliases = [title] + ([renames[title]] if title in renames else [])
        delivered = [it for a in aliases for it in by_section.get(a, [])
                     if it.status == "done"]
        kinds = Counter(e.get("kind", "note") for it in delivered for e in (it.evidence or []))
        falsifiable = sum(kinds[k] for k in FALSIFIABLE_EVIDENCE)
        unfalsifiable = sum(kinds[k] for k in UNFALSIFIABLE_EVIDENCE)

        claims = []
        for it in delivered:
            for tp in (it.touchpoints or []):
                claims.append({"item": it.key, "touchpoint": tp,
                               "nodes": _touchpoint_hits(paths, tp)})
        checkable = [c for c in claims if c["nodes"] == 0]

        sections.append({
            "section": title,
            "delivered_items": [it.key for it in delivered],
            "receipts": dict(kinds),
            "falsifiable": falsifiable,
            "unfalsifiable": unfalsifiable,
            "claims": claims,
            # `unknown` when nothing was claimed — distinct from a claim that failed.
            "corroboration": ("unknown" if not claims
                              else "partial" if checkable else "corroborated"),
        })
        if delivered and not falsifiable:
            unsupported.append(title)
        if checkable:
            uncorroborated.extend(c["item"] + " → " + c["touchpoint"] for c in checkable)

    return {
        "governed": True,
        "baseline_version": base.version,
        "sections": sections,
        # Delivered work whose only proof is something its author could have typed.
        "unsupported": unsupported,
        # Touchpoints an item claimed where the code graph shows nothing.
        "uncorroborated": sorted(set(uncorroborated)),
    }


SERVES, ENABLES, UNRELATED, UNDECIDABLE = "serves", "enables", "unrelated", "undecidable"
# Only a HIGH-confidence `unrelated` self-flags; the ambiguous middle defers to sign-off,
# reusing AL-227's memory auto-triage shape. Set where a wrong flag is cheap to dismiss and
# a missed one is not — an item wrongly called unrelated is argued away in one reply, while
# stowaway scope that nobody questioned is the failure this exists to catch.
_UNRELATED_FLAG_MIN = 0.8
# Above this many stale rows, recompute lazily on first read instead of inline. Named
# constant, deliberately conservative, and NOT a project setting: we do not know the right
# value yet, and a slider is how you avoid finding out.
STALE_INLINE_MAX = 3

JUDGE_SYSTEM = (
    "You classify one piece of COMPLETED work against a PRD's goal. Answer only the "
    "question asked.\n\n"
    "Your ceiling is bounded and you must respect it: you see the item's text, its "
    "evidence receipts, and the paths it touched. You do NOT see the diff, the tests, or "
    "the running system. So you can judge whether this work is about the RIGHT THING. You "
    "cannot judge whether it WORKS, and you must never imply that you can.\n\n"
    "`serves` — this work advances the goal the PRD states.\n"
    "`unrelated` — it does not. Not a criticism: plenty of legitimate work has nothing to "
    "do with a given goal.\n\n"
    "Do NOT answer `enables`. Whether this unblocks other work is read from the link "
    "graph, not from you.\n\n"
    "`confidence` is 0.0-1.0 and should be LOW when the item text is thin, the goal is "
    "broad, or you are inferring rather than reading. A confident wrong answer is worse "
    "than an admitted uncertain one.\n\n"
    'Respond with ONLY a compact JSON object: {"outcome": "serves"|"unrelated", '
    '"confidence": <float>, "reasoning": "<one sentence>"}.'
)


def _judge_context(prd: Prd, base: PrdVersion, item: Item) -> str:
    """What the judge is shown. The GOAL leads, then the work — the opposite order to the
    grill's classifier, and for the opposite reason: there the risk was grading the
    document instead of the interrogation, here the document IS the standard."""
    goals = section_bodies(base.body).get("Goals") or section_bodies(base.body).get(
        "Problem") or base.body[:1200]
    return "\n\n".join([
        f"PRD GOAL — {prd.title} (baseline {base.version}). This is the standard:",
        goals.strip(),
        "COMPLETED WORK under classification:",
        f"title: {item.title}",
        f"section it was filed under: {item.prd_section or '(none)'}",
        f"description: {(item.description or '(none)')[:1500]}",
        f"paths touched: {', '.join(item.touchpoints or []) or '(none declared)'}",
        "evidence receipts: " + (
            "; ".join(f"{e.get('kind')}: {e.get('detail','')[:120]}"
                      for e in (item.evidence or [])) or "(none)"),
    ])


def _enabled_by_graph(db: Session, item: Item) -> bool:
    """Whether this item unblocks other work on the same PRD (GRPH-249).

    Derived, never judged. Typed links and `blocked_by`/`unblocks` already encode it, and
    asking a model to re-derive a fact the graph holds is both slower and less reliable —
    the LLM is spent only on the semantic call.
    """
    from app.services import links as links_svc

    siblings = {it.id for it in items_svc.list_items(db, project_id=item.project_id)
                if it.prd_id == item.prd_id and it.id != item.id}
    for edge in links_svc.list_links(db, project_id=item.project_id):
        if edge.type != "dependency" or item.id not in (edge.a, edge.b):
            continue
        if (edge.b if edge.a == item.id else edge.a) in siblings:
            return True
    return False


def classify_work(db: Session, item: Item, *, force: bool = False) -> "WorkClassification | None":
    """Classify one completed item against its PRD's goal (GRPH-249).

    Fires on completion rather than on link: at link time an item is an intention with
    nothing delivered to judge, and a judgement of an intention is a judgement of a
    sentence someone typed.

    Three answers come from three different places, deliberately:

    - **`unrelated` / `serves`** is the semantic call, and the only part a model is asked.
    - **`enables`** is DERIVED from the link graph. Typed links already encode it, so
      asking a model to re-derive it would be slower, less reliable, and would spend the
      one expensive call on a question already answered.
    - **`undecidable`** is what an unconfigured instance returns. `chat_provider` defaults
      to `stub`, and guessing there would put a clean bill of health on an instance that
      judged nothing.

    Confidence gates the consequence, not the answer: only a HIGH-confidence `unrelated`
    self-flags. The ambiguous middle is recorded with `needs_review` and deferred to
    sign-off, which is AL-227's memory triage shape — the same problem, one domain over.
    """
    if not item.prd_id or item.status != "done":
        return None
    prd = db.get(Prd, item.prd_id)
    base = baseline_of(db, item.prd_id) if prd is not None else None
    if prd is None or base is None:
        return None  # no agreed intent to judge against

    row = db.scalar(select(WorkClassification).where(WorkClassification.item_id == item.id))
    if row is not None and not force and not row.stale \
            and row.baseline_version == base.version:
        return row

    _r = platform_svc.resolve_chat(db, prd.project_id)
    provider, chat = _r.provider_id, _r.chat
    if provider == "stub":
        outcome, confidence, reasoning, grader = (
            UNDECIDABLE, 0.0,
            "No chat provider configured — alignment was not assessed.", "stub")
    else:
        try:
            raw = chat.chat(system=JUDGE_SYSTEM,
                            context=_judge_context(prd, base, item),
                            question="Classify this work against the goal.",
                            temperature=0)
            match = re.search(r"\{.*\}", raw or "", re.DOTALL)
            parsed = json.loads(match.group(0)) if match else {}
            outcome = parsed.get("outcome")
            if outcome not in (SERVES, UNRELATED):
                raise ValueError(f"unusable outcome {outcome!r}")
            confidence = float(parsed.get("confidence") or 0.0)
            reasoning = str(parsed.get("reasoning") or "").strip()
            grader = _grader_id(db, prd)
        except Exception:  # noqa: BLE001 — an unusable judge is undecidable, never a guess
            logger.warning("platform judge: unusable reply for %s; recording undecidable",
                           item.id)
            outcome, confidence, reasoning, grader = (
                UNDECIDABLE, 0.0, "The judge did not return a usable answer.",
                _grader_id(db, prd))

    # Derived, and it OVERRIDES an `unrelated`: work that unblocks work serving the goal
    # is not unrelated to it, whatever it looks like read on its own.
    if outcome == UNRELATED and _enabled_by_graph(db, item):
        outcome, reasoning = ENABLES, (
            f"{reasoning} Reclassified from unrelated: the link graph shows this unblocks "
            f"other work on this PRD.").strip()

    if row is None:
        row = WorkClassification(item_id=item.id, prd_id=item.prd_id)
        db.add(row)
    row.prd_id = item.prd_id
    row.outcome = outcome
    row.reasoning = reasoning
    row.confidence = round(confidence, 3)
    row.graded_by = grader
    row.baseline_version = base.version
    row.stale = False
    # Only a confident `unrelated` self-flags. Everything uncertain, and everything the
    # judge could not assess, waits for a human at sign-off rather than acting on itself.
    row.needs_review = (outcome == UNRELATED and confidence < _UNRELATED_FLAG_MIN) or \
                       outcome == UNDECIDABLE
    db.commit()
    db.refresh(row)
    return row


def mark_classifications_stale(db: Session, prd: Prd) -> int:
    """Invalidate every classification made against a superseded baseline (GRPH-249).

    Called when a new baseline is frozen. Marking is unconditional and recomputation is
    not: the marker is the single source of truth, and the eager path below is a warm-up
    on the lazy one rather than a second design that could disagree with it.
    """
    rows = list(db.scalars(select(WorkClassification).where(
        WorkClassification.prd_id == prd.id)).all())
    base = baseline_of(db, prd.id)
    stale = [r for r in rows if base is None or r.baseline_version != base.version]
    for r in stale:
        r.stale = True
    if stale:
        db.commit()
    # Small sets recompute now so the next reader sees fresh answers; large ones wait, so a
    # rebaseline on a big PRD never blocks on a hundred model calls.
    if 0 < len(stale) <= STALE_INLINE_MAX:
        for r in stale:
            it = db.get(Item, r.item_id)
            if it is not None:
                classify_work(db, it, force=True)
    return len(stale)


def classifications(db: Session, prd: Prd, *, refresh: bool = True) -> list[dict]:
    """Every classification for this PRD, recomputing stale rows on the way out.

    `refresh` is the lazy half of the staleness design: reading the report is what pays
    for a large rebaseline's recompute, so the write path stays fast and the numbers a
    reader sees are never quietly out of date.
    """
    rows = list(db.scalars(select(WorkClassification).where(
        WorkClassification.prd_id == prd.id).order_by(WorkClassification.id)).all())
    if refresh:
        for r in [r for r in rows if r.stale]:
            it = db.get(Item, r.item_id)
            if it is not None:
                classify_work(db, it, force=True)
        db.expire_all()
        rows = list(db.scalars(select(WorkClassification).where(
            WorkClassification.prd_id == prd.id).order_by(WorkClassification.id)).all())
    out, seen = [], set()
    for r in rows:
        it = db.get(Item, r.item_id)
        seen.add(r.item_id)
        out.append({
            "item": it.key if it is not None else r.item_id,
            "outcome": r.outcome, "reasoning": r.reasoning, "confidence": r.confidence,
            "graded_by": r.graded_by, "baseline_version": r.baseline_version,
            "needs_review": r.needs_review, "stale": r.stale,
        })
    # Completed work with no row at all (GRPH-325). The judge fires on completion, so
    # anything finished before it shipped — the entire population on any instance that
    # adopts this after doing work — has none. Returning only the rows that exist renders
    # that as an empty list, which reads as "nothing to report" when it means "nobody
    # looked". Those are opposite claims and the quiet one is the reassuring one.
    #
    # Named rather than classified on the spot: a first read of a large PRD must not
    # silently cost N model calls. The reader sees the gap and decides whether to spend
    # them, which is the same choice the stale/lazy split already makes.
    for it in items_svc.list_items(db, project_id=prd.project_id):
        if it.prd_id != prd.id or it.status != "done" or it.id in seen:
            continue
        out.append({
            "item": it.key, "outcome": "unclassified",
            "reasoning": "Completed before this was judged, or the judge never ran.",
            "confidence": 0.0, "graded_by": "", "baseline_version": "",
            "needs_review": True, "stale": False,
        })
    return out


def _section_histories(chain: list[PrdVersion]) -> dict[str, dict]:
    """Trace every section through the baseline chain: where it came from, what happened
    to it, and at which version (GRPH-245).

    Keyed by the section's ORIGINAL title, because that is the name the reader agreed to.
    Following renames forward is what lets a section that was retitled twice still be
    reported as the same intent rather than as one drop and two arrivals.
    """
    histories: dict[str, dict] = {}
    current: dict[str, str] = {}  # original title -> title in the latest baseline

    for title in parse_sections(chain[0].body):
        histories[title] = {"origin": chain[0].version, "events": [], "current_title": title,
                            "dropped_at": None}
        current[title] = title

    for older, newer in zip(chain, chain[1:]):
        d = diff_sections(older.body, newer.body)
        renamed = dict(d["renamed"])
        live = {v: k for k, v in current.items() if histories[k]["dropped_at"] is None}
        for was, now in renamed.items():
            origin = live.get(was)
            if origin is not None:
                histories[origin]["events"].append(
                    {"version": newer.version, "change": "renamed", "from": was, "to": now})
                current[origin] = now
        for title in d["modified"]:
            origin = live.get(title)
            if origin is not None:
                histories[origin]["events"].append(
                    {"version": newer.version, "change": "modified"})
        for title in d["removed"]:
            origin = live.get(title)
            if origin is not None:
                histories[origin]["events"].append(
                    {"version": newer.version, "change": "removed"})
                histories[origin]["dropped_at"] = newer.version
        # GRPH-318 forbids a rebaseline adding sections, so this should stay empty. It is
        # still traced rather than ignored: a PRD baselined before that rule, or a guard
        # bypassed, is exactly the retroactive legitimisation this report exists to show.
        for title in d["added"]:
            histories[title] = {"origin": newer.version, "current_title": title,
                                "dropped_at": None,
                                "events": [{"version": newer.version, "change": "added"}]}
            current[title] = title

    for origin, h in histories.items():
        h["current_title"] = current.get(origin, origin)
    return histories


def close_report(db: Session, prd: Prd) -> dict:
    """Delivered work against ORIGINAL intent — the payoff of the whole feature (GRPH-245).

    Sign-off is judged against the *current* baseline. Closing reads against the **first**
    one: the work is done, *and* here is the drift that accumulated on the way — what was
    added, what was dropped, and at which baseline each change happened. Reading against
    the governing baseline instead would make the report agree with itself by construction,
    since the governing baseline is where the spec ended up.

    The audience is a product manager deciding whether dropped scope should be picked back
    up, so the shape follows that decision: every piece of original intent, what became of
    it, and whether anything was delivered against it.

    **Retroactive legitimisation stays visible.** A section that entered at a later
    baseline is reported with the version that introduced it, never folded in as though it
    had been agreed at the start. GRPH-318 now forbids a rebaseline adding sections, so
    that list should be empty — it is still computed, because a PRD baselined before that
    rule, or a guard bypassed, is precisely the case worth surfacing.

    **It never says "complete".** PRD-12 is explicit: the platform assesses whether
    *claimed* work covers *stated* intent, and must never render that as a finished PRD.
    So there is no verdict field here, no score, and no percentage — the counts describe
    what happened, and the judgement belongs to the reader.
    """
    chain = baseline_chain(db, prd.id)
    if not chain:
        return {"governed": False, "original_version": None, "sections": [],
                "dropped": [], "expanded_scope": [], "added_after_approval": []}

    histories = _section_histories(chain)
    items = [it for it in items_svc.list_items(db, project_id=prd.project_id)
             if it.prd_id == prd.id]
    by_section: dict[str, list] = {}
    for it in items:
        by_section.setdefault(it.prd_section or "", []).append(it)

    drift = scope_drift(db, prd)
    dispositions = {d["section"]: d for d in (prd.close_record or {}).get("dispositions", [])}

    sections, dropped, expanded = [], [], []
    for origin_title, h in histories.items():
        # Work filed under ANY name this section has held — a rename must not orphan the
        # work done under the old title.
        titles = {origin_title, h["current_title"]}
        titles.update(e["from"] for e in h["events"] if e["change"] == "renamed")
        titles.update(e["to"] for e in h["events"] if e["change"] == "renamed")
        linked = [it for t in titles for it in by_section.get(t, [])]
        delivered = [it for it in linked if it.status == "done"]

        row = {
            "section": origin_title,
            "current_title": h["current_title"],
            "introduced_at": h["origin"],
            "dropped_at": h["dropped_at"],
            "history": h["events"],
            "delivered_items": sorted(it.key for it in delivered),
            "planned_items": sorted(it.key for it in linked),
            # `dropped` means dropped FROM THE SPEC by a rebaseline. Work that was simply
            # never done is `undelivered`, and conflating them would tell a PM that
            # somebody decided something when nobody did.
            # Framing sections are reported (PRD-12's third problem is that the section
            # defining "done" is structurally exempt from every check) but never counted
            # as work. Found live: without this the close report told a PM that "Problem",
            # "Goals", "Non-goals" and "Success criteria" were never delivered — six of
            # fourteen entries in the headline finding were sections that can never have
            # work, which is the AL-96 failure in the one artifact a PM acts on.
            "framing": not is_implementable_section(origin_title),
            "fate": ("dropped" if h["dropped_at"]
                     else "framing" if not is_implementable_section(origin_title)
                     else "delivered" if delivered
                     else "undelivered"),
            "disposition": dispositions.get(origin_title),
        }
        sections.append(row)
        if h["dropped_at"]:
            dropped.append(row)
        if h["origin"] != chain[0].version:
            expanded.append(row)

    return {
        "governed": True,
        "original_version": chain[0].version,
        "governing_version": chain[-1].version,
        "chain": [{"version": v.version, "reason_type": v.rebaseline_reason_type,
                   "reason": v.rebaseline_reason} for v in chain],
        "sections": sections,
        # The two lists a PM actually acts on.
        "dropped": [r["section"] for r in dropped],
        "never_delivered": [r["section"] for r in sections if r["fate"] == "undelivered"],
        # Intent that was NOT in the original spec. Should be empty since GRPH-318.
        "expanded_scope": [r["section"] for r in expanded],
        # Work attached after intent was first agreed, carried through from scope drift.
        "added_after_approval": drift["scope_added"],
        "drift": {"accumulated": drift["accumulated"], "current": drift["current"],
                  "total": drift["total"]},
        "closed": prd.close_record,
    }


def audit_brief(db: Session, prd: Prd) -> dict:
    """Everything a repo-holding agent needs to audit this PRD, in one read (GRPH-252).

    The agent auditor is the only component that can reach actual code, which is why
    **completeness authority lives there** — and why it needs no provider key on the
    instance: it brings its own model. This function is the handover.

    **The authority split is carried in the payload, not left to convention.** Drift
    history is the platform's finding — it watched the timeline, the auditor sees only the
    end state — so it arrives as a stated result rather than as raw material to re-derive.
    Completeness is the auditor's call, so what arrives is the *question*: here are the
    baselined sections, here is what is linked to each, now go and look at the repo. An
    auditor handed a conclusion where it should have been handed a question would rubber-
    stamp the platform's own guess.

    One call rather than five, because an audit assembled differently by every agent is
    not comparable between them, and comparability is most of what a corpus is for.

    Note what is deliberately NOT here: any instruction on how to decide. The brief is
    evidence and open questions; the judgement belongs to the agent, and shipping a house
    opinion inside it would make every auditor agree with us by construction.
    """
    base = baseline_of(db, prd.id)
    if base is None:
        return {"governed": False, "prd_id": prd.key, "sections": []}

    chain = baseline_chain(db, prd.id)
    done = completeness(db, prd)
    rollup = evidence_rollup(db, prd)
    graded = {c["item"]: c for c in classifications(db, prd)}
    receipts = {s["section"]: s for s in rollup["sections"]} if rollup["governed"] else {}
    bodies = section_bodies(base.body)
    verdicts_by_section: dict[str, list] = {}
    for v in verdicts(db, prd):
        verdicts_by_section.setdefault(v.section or "", []).append(
            {"outcome": v.outcome, "signed_by": v.signed_by,
             "self_signed": v.self_signed, "separation": v.separation})

    sections = []
    for s in done["sections"]:
        title = s["section"]
        ev = receipts.get(title, {})
        sections.append({
            "section": title,
            "framing": s["framing"],
            # The intent itself. An auditor asked "was this delivered" without the text of
            # what was promised is being asked to guess.
            "intent": bodies.get(title, "").strip(),
            "state": s["state"],
            "items": [
                {**i, "classification": graded.get(i["id"], {}).get("outcome", "unclassified"),
                 "classification_reasoning": graded.get(i["id"], {}).get("reasoning", "")}
                for i in s["items"]
            ],
            "falsifiable_receipts": ev.get("falsifiable", 0),
            "unfalsifiable_receipts": ev.get("unfalsifiable", 0),
            "corroboration": ev.get("corroboration", "unknown"),
            "verdicts": verdicts_by_section.get(title, []),
        })

    return {
        "governed": True,
        "prd_id": prd.key,
        "title": prd.title,
        "baseline_version": base.version,
        "original_version": chain[0].version,
        "sections": sections,
        # THE auditor's question, restated plainly so it cannot be missed in the detail.
        "outstanding": sorted(set(done["absent"]) | set(done["undelivered"])),
        # The platform's findings, labelled as such. Authoritative on drift history.
        "platform_findings": {
            "drift": {k: rollup.get(k) for k in ("unsupported", "uncorroborated")}
            | {"scope": scope_drift(db, prd)["total"]},
            "authority": "The platform is authoritative on drift history; you are "
                         "authoritative on completeness. Check the repo, not this list.",
        },
        # What has already been claimed, so a re-audit sees prior verdicts rather than
        # silently duplicating or contradicting them without noticing.
        "existing_verdicts": sum(len(v) for v in verdicts_by_section.values()),
        "unjudged_items": sorted(k for k, c in graded.items()
                                 if c["outcome"] in ("unclassified", "undecidable")),
    }


def audit_coverage(db: Session, prd: Prd) -> dict:
    """Which intent elements the auditor has actually rendered a verdict on (GRPH-252).

    An audit that verdicts three sections of fourteen is not an audit, and without this it
    is indistinguishable from one that covered everything — the submission succeeded either
    way. Framing sections are excluded: they describe the work rather than being it, and
    demanding a verdict on "Problem" would train an auditor to emit filler.
    """
    base = baseline_of(db, prd.id)
    if base is None:
        return {"governed": False, "covered": [], "uncovered": []}
    demanded = [t for t in parse_sections(base.body) if is_implementable_section(t)]
    covered = {v.section for v in verdicts(db, prd) if v.section}
    return {
        "governed": True,
        "covered": sorted(covered & set(demanded)),
        "uncovered": sorted(set(demanded) - covered),
        "complete": not (set(demanded) - covered),
    }


class CloseRefused(ValueError):
    """The close cannot proceed: no baseline, a judge that went down, or intent that
    nobody has accounted for."""


def close_prd(
    db: Session, prd: Prd, *, dispositions: list[dict], closed_by: str,
    verdict: str = "", judge_reachable: bool = True,
) -> dict:
    """Close a PRD — the terminal state (GRPH-244).

    **Close gates on disposition, not on delivery.** A PRD may always be closed; what it
    may not do is close while pretending nothing was missed. Every baselined section the
    completeness pass reports as having nothing delivered must first be accounted for as
    `promoted` (into a backlog item or a successor PRD) or `deferred` (knowingly dropped,
    with a stated reason).

    That is the grill's completion standard one level up. There: four dimensions by three
    outcomes, complete at zero *unanswered*, with `deferred` completing rather than
    blocking because the failure being caught is an implicit non-answer rather than a
    conscious decision to leave something open. Here: every undelivered section by two
    dispositions, closed at zero *undispositioned*. The shape is reused deliberately —
    it has already survived contact with real use.

    It is also what dissolves the negative-verdict question the PRD carried as open from
    v1.0. The dilemma assumed the PRD has to leave the terminal state; it does not, the
    *work* does. So a negative verdict is productive rather than punitive: nobody has to
    declare failure in order to close, they have to say where the missing work went, which
    is a question people will actually answer. A verdict that merely blocked closing would
    produce a tracker full of PRDs nobody will touch and an audit everyone routes around.

    The precondition is **set equality**, not a count: a count would let one section be
    dispositioned twice while another was missed.

    Ordering is deliberate — everything is validated before anything is created, so the
    overwhelming majority of failures happen with nothing written. A promotion that fails
    after earlier ones succeeded leaves those items in place and the PRD open; that is
    stated rather than hidden, because the alternative is a transaction spanning services
    that each commit, and a close recorded against a disposition that does not exist would
    be far worse than a promoted item somebody has to look at.
    """
    ready = close_readiness(db, prd, judge_reachable=judge_reachable)
    if not ready["can_close"]:
        raise CloseRefused(ready["blocked_on"])
    if prd.close_record is not None:
        raise CloseRefused(f"{prd.key} is already closed; post-close work is a new PRD")

    outstanding = set(dropped_intent(db, prd))
    named = [str(d.get("section") or "").strip() for d in dispositions]
    if len(named) != len(set(named)):
        raise CloseRefused("a section is dispositioned more than once")
    missing = sorted(outstanding - set(named))
    if missing:
        raise CloseRefused(
            "these have nothing delivered and nothing decided about them: "
            + ", ".join(missing) + ". Promote them, or defer them with a reason.")
    stray = sorted(set(named) - outstanding)
    if stray:
        raise CloseRefused(
            "these were delivered, so there is nothing to disposition: " + ", ".join(stray))

    # Validate every disposition BEFORE creating anything.
    for d in dispositions:
        kind = d.get("disposition")
        if kind not in DISPOSITIONS:
            raise CloseRefused(f"unknown disposition {kind!r} for {d.get('section')!r}")
        if kind == "deferred" and not str(d.get("reason") or "").strip():
            # A deferral with no reason is indistinguishable from an oversight, which is
            # the precise failure the grill's deferred/unanswered split exists to catch.
            raise CloseRefused(f"deferring {d.get('section')!r} needs a stated reason")
        if kind == "promoted" and d.get("promote_to", "item") not in ("item", "prd"):
            raise CloseRefused(f"promote_to must be 'item' or 'prd' for {d.get('section')!r}")

    recorded = []
    for d in dispositions:
        section = str(d["section"]).strip()
        if d["disposition"] == "deferred":
            recorded.append({"section": section, "disposition": "deferred",
                             "target": None, "reason": d["reason"].strip()})
            continue
        if d.get("promote_to", "item") == "prd":
            target = promote_to_prd(db, prd, [section], title=d.get("title", "")).key
        else:
            target = promote_to_item(db, prd, section, title=d.get("title", "")).key
        recorded.append({"section": section, "disposition": "promoted",
                         "target": target, "reason": str(d.get("reason") or "").strip()})

    base = baseline_of(db, prd.id)
    prd.close_record = {
        "closed_at": utcnow().isoformat(),
        "closed_by": closed_by,
        "mode": ready["mode"],
        "baseline_version": base.version if base is not None else "",
        "verdict": verdict,
        # Carried verbatim so a `mechanical` close can never be read as a judged one.
        "disclosure": ready["disclosure"],
        "dispositions": recorded,
    }
    prd.status = "closed"
    prd.updated = "just now"
    db.commit()
    db.refresh(prd)
    events_svc.record(
        db, actor_type="agent" if closed_by.startswith("agent:") else "user",
        actor_label=closed_by, surface="mcp", action="close_prd", target_type="prd",
        target_id=prd.id, project_id=prd.project_id,
        meta={"mode": ready["mode"], "dispositions": len(recorded)},
    )
    return prd.close_record


class MalformedVerdict(ValueError):
    """A verdict that cites nothing, or cites something that does not resolve."""


SELF_SIGNED, INDEPENDENT, UNVERIFIABLE = "self-signed", "independent", "unverifiable"


def separation_of_duties(db: Session, prd: Prd, *, signer: str,
                         api_key_id: str | None = None) -> dict:
    """Whether the signer also built the work under audit (GRPH-253 / GRPH-327).

    PRD-12: *"the signer must not be the implementer"* — otherwise sign-off is the worker
    grading their own exam through a second door.

    **Three answers, not two.** The original returned a bare "did the signer claim any of
    this", and on a PRD where nobody claimed anything that reported False — which reads as
    "someone else built it" when it means "nobody recorded who did". Those are opposite
    claims and the quiet one is the reassuring one. It failed exactly that way on PRD-12's
    own audit: 0 of 27 items carried a claimant, so 14 verdicts signed by the author of the
    work came back clean.

    Two signals, strongest first:

    - **`claimed_by`**, which the PRD names. Precise while a lease is held, but optional,
      and it keeps only the LAST holder.
    - **The event log**, which records an actor on every accepted mutation and cannot be
      skipped by working without a lease. Matched on `actor_id` against the signing key,
      because two agents can share a display name and a key id cannot be borrowed by
      accident.

    `unverifiable` only when NEITHER knows anything — and it must never render as a pass.
    """
    signer = (signer or "").strip()
    mine = [it for it in items_svc.list_items(db, project_id=prd.project_id)
            if it.prd_id == prd.id]
    ids = {it.id for it in mine} | {it.key for it in mine}
    by_id = {it.id: it.key for it in mine}
    by_id.update({it.key: it.key for it in mine})

    claimed = {it.key for it in mine if it.claimed_by}
    overlap = {it.key for it in mine if signer and it.claimed_by == signer}
    basis = "claim" if overlap else ""

    # The event log catches an implementer who never took a lease, which is the ordinary
    # path for a single agent and was the entire population on PRD-12.
    rows = db.scalars(select(Event).where(
        Event.target_type == "item", Event.target_id.in_(ids))).all() if ids else []
    touched_by_someone = {e.target_id for e in rows}
    if not overlap and (signer or api_key_id):
        label = signer.removeprefix("agent:")
        from_events = {
            by_id.get(e.target_id, e.target_id) for e in rows
            if (api_key_id and e.actor_id == api_key_id)
            or (label and e.actor_label in (label, signer))
        }
        if from_events:
            overlap, basis = from_events, "event-log"

    if overlap:
        status = SELF_SIGNED
    elif claimed or touched_by_someone:
        # Somebody's fingerprints are on this work and they are not the signer's.
        status, basis = INDEPENDENT, basis or ("claim" if claimed else "event-log")
    else:
        status, basis = UNVERIFIABLE, "none"
    return {"status": status, "items": sorted(overlap), "basis": basis}


def self_signed_against(db: Session, prd: Prd, signer: str) -> list[str]:
    """Back-compat shim: the items a signer also worked on, or empty."""
    out = separation_of_duties(db, prd, signer=signer)
    return out["items"] if out["status"] == SELF_SIGNED else []


def record_verdict(
    db: Session, prd: Prd, *, outcome: str, citations: list[dict],
    signed_by: str, reasoning: str = "", api_key_id: str | None = None,
    section: str | None = None,
) -> Verdict:
    """Store a sign-off verdict, or refuse it (GRPH-253).

    Three things happen here and the order matters.

    **Validity first.** A verdict that cites nothing, or cites something that does not
    resolve, is rejected as *malformed* rather than recorded as a failed pass. The
    distinction is the whole point: the server cannot check whether the code is correct,
    but it can check that the thing pointed at exists, and a claim that can be checked at
    all is the achievable upgrade over one that cannot.

    **Then provenance.** The signing identity and the key behind it are recorded. Two
    agents can share a display name; a credential cannot be borrowed by accident, so the
    key is the identity that survives a dispute.

    **Then separation of duties.** Overlap with `claimed_by` on the work under audit sets
    `self_signed`, along with the items that triggered it. Flagged, never refused: on a
    solo project the signer and the implementer are the same person, and refusing there
    would mean nobody could sign off at all — a rule that stops the ordinary case gets
    routed around within a day. What it must never be is invisible.

    The baseline version is stamped because a verdict outlives the intent it was made
    about, and without it a judgement of v1.0 silently reads as a judgement of today.
    """
    check = validate_verdict(db, prd, citations)
    if not check["ok"]:
        raise MalformedVerdict("; ".join(check["problems"]))
    if section is not None:
        base = baseline_of(db, prd.id)
        known = set(parse_sections(base.body)) if base is not None else set()
        known |= {new for _old, new in diff_sections(base.body, prd.body)["renamed"]} \
            if base is not None else set()
        if section not in known:
            # A verdict about a section the baseline does not contain is unfalsifiable in
            # the same way a citation to nothing is: there is no intent to check it against.
            raise MalformedVerdict(f"no such section in the baseline: {section}")

    sep = separation_of_duties(db, prd, signer=signed_by, api_key_id=api_key_id)
    overlap = sep["items"]
    base = baseline_of(db, prd.id)
    row = Verdict(
        prd_id=prd.id,
        baseline_version=base.version if base is not None else "",
        section=section,
        outcome=outcome,
        reasoning=reasoning,
        citations=list(citations),
        signed_by=signed_by,
        api_key_id=api_key_id,
        self_signed=sep["status"] == SELF_SIGNED,
        self_signed_items=overlap,
        separation=sep["status"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def verdicts(db: Session, prd: Prd) -> list[Verdict]:
    """Every verdict recorded against this PRD, oldest first. Append-only: a later verdict
    supersedes an earlier one by being later, and nothing overwrites what was claimed
    before — the same rule the baseline chain follows, for the same reason."""
    return list(db.scalars(
        select(Verdict).where(Verdict.prd_id == prd.id).order_by(Verdict.id)
    ).all())


# How a judged close can fail, and why the two are not the same failure (GRPH-311).
JUDGE_ABSENT, JUDGE_DOWN, JUDGE_READY = "unconfigured", "unavailable", "ready"


def judge_status(db: Session, project_id: str, *, reachable: bool = True) -> str:
    """Whether this project has a judge, and whether it answered.

    PRD-12 v1.0 answered its own question 7 with *"if the judge becomes unavailable during
    a closing, refuse to close the ticket."* Right for a transient outage, wrong as the
    shipped default: `CHAT_PROVIDER` defaults to `stub`, so on a default install the judge
    is **permanently** unavailable and closing becomes permanently impossible. The rule
    blocks the instance that most needs to ship.

    So the two cases are told apart. `unconfigured` is a standing property of the
    deployment — nobody chose a judge, and waiting for one is waiting forever.
    `unavailable` is a judge that WAS chosen and did not answer, which is the transient
    outage the original rule was written for, and it still blocks.

    Liveness is an INPUT, not something probed here. A caller learns a judge is down by
    calling it and catching `errors.Unavailable`; a separate health ping would add a
    network round trip to a read and still prove nothing, since it can succeed a second
    before the call that matters fails.
    """
    _r = platform_svc.resolve_chat(db, project_id)
    provider, _chat = _r.provider_id, _r.chat
    if provider == "stub":
        return JUDGE_ABSENT
    return JUDGE_READY if reachable else JUDGE_DOWN


def close_readiness(db: Session, prd: Prd, *, judge_reachable: bool = True) -> dict:
    """Whether this PRD can be closed, and in what mode (GRPH-311).

    The mechanical half of PRD-12 — structural drift, completeness, evidence, citation
    validity — needs no chat model at all. So a close with no judge configured is possible
    and is labelled `mechanical`, the same *degrade and disclose* pattern the grill already
    uses for its stub bar: `graded_by="stub"` with the limitation stated rather than
    hidden.

    What it must never do is let `mechanical` read as `judged`. PRD-12 is explicit that a
    verdict which looks like a measurement when it is an opinion is the failure — and the
    inverse, an unjudged close wearing a judged label, is the same dishonesty pointing the
    other way.
    """
    status = judge_status(db, prd.project_id, reachable=judge_reachable)
    governed = baseline_of(db, prd.id) is not None
    if not governed:
        return {"can_close": False, "mode": None, "judge": status,
                "blocked_on": "no baseline — there is no agreed intent to close against"}
    if status == JUDGE_DOWN:
        # The case the original rule was written for, and it still blocks. A judge that was
        # configured and is not answering means the judged close is merely LATE, and
        # closing mechanically now would silently downgrade a verdict someone is expecting.
        return {"can_close": False, "mode": None, "judge": status,
                "blocked_on": "a judge is configured but not answering; retry when it is back"}
    return {
        "can_close": True,
        "mode": "mechanical" if status == JUDGE_ABSENT else "judged",
        "judge": status,
        "blocked_on": None,
        # Carried so a caller cannot render a mechanical close as a judged one by accident.
        "disclosure": (
            "No judge is configured. Structural drift, completeness and citation validity "
            "were checked; whether the work SATISFIES the intent was not assessed."
        ) if status == JUDGE_ABSENT else None,
    }


def intent_hold(db: Session, item: Item) -> dict | None:
    """Whether this item is being built against intent that has since moved (GRPH-242).

    **Derived, never stored.** PRD-12 asks for the notice to be pull-based "so no push
    channel can fail and the agent cannot miss it" — and computing it goes further than a
    delivered message would: there is nothing to acknowledge away. The item WAS claimed
    under v1.0 and the governing baseline IS v1.1; that stays true no matter who read
    what, and a stored flag could be marked seen while the mismatch persisted.

    Returned from `_item_dict`, so it rides on every item any agent reads — claim,
    heartbeat, update, search. GRPH-312's hole was that reassessment hung off the claim
    path while agents can complete without ever touching it; hanging it off the item
    itself is what closes that, because there is no way to work on an item without
    reading one.

    None once the item is `done`: the hold is about work in flight. The completion path
    stamps the mismatch onto the item's evidence instead, so the record outlives it.
    """
    if item is None or item.status == "done" or not item.prd_id:
        return None
    if not item.baseline_at_claim:
        # Work that started before this was recorded. Saying nothing is the honest answer;
        # assuming it targeted the current baseline would invent the very fact in doubt.
        return None
    base = baseline_of(db, item.prd_id)
    if base is None or base.version == item.baseline_at_claim:
        return None

    prior = next((v for v in baseline_chain(db, item.prd_id)
                  if v.version == item.baseline_at_claim), None)
    changed = diff_sections(prior.body, base.body) if prior is not None else None
    return {
        "started_against": item.baseline_at_claim,
        "baseline_version": base.version,
        "reason_type": base.rebaseline_reason_type,
        "reason": base.rebaseline_reason,
        # Which sections actually moved, so the holder can tell "my section changed" from
        # "something else in the PRD changed" without re-reading the whole spec.
        "sections_changed": sorted(changed["modified"] + changed["added"] + changed["removed"])
        if changed else [],
        "section_affected": bool(
            changed and item.prd_section in
            set(changed["modified"] + changed["added"] + changed["removed"])
        ),
    }


def held_claims(db: Session, prd: Prd) -> list[dict]:
    """In-flight work on this PRD that started against superseded intent.

    The other direction of `intent_hold`: who needs telling. `claimed_by` and the lease
    give the list, which is why no separate subscription table is needed."""
    out = []
    for it in items_svc.list_items(db, project_id=prd.project_id):
        if it.prd_id != prd.id:
            continue
        hold = intent_hold(db, it)
        if hold:
            out.append({"id": it.key, "status": it.status, "claimed_by": it.claimed_by,
                        "section": it.prd_section, **hold})
    return out


class NothingDropped(ValueError):
    """The section named has delivered work, so there is no dropped intent to promote."""


def dropped_intent(db: Session, prd: Prd) -> list[str]:
    """Baselined sections with nothing delivered against them — what can be promoted.

    Reads the governing baseline, so a section deleted from the body after approval is
    still here. That is the case promotion exists for: the intent was agreed, the work
    never happened, and the heading quietly disappeared."""
    done = completeness(db, prd)
    return sorted(done["absent"] + done["undelivered"]) if done["governed"] else []


def _dropped_or_raise(db: Session, prd: Prd, sections: list[str]) -> list[str]:
    """Guard for both promotion paths.

    Promoting intent that WAS delivered would manufacture duplicate work and, worse, write
    a lineage record asserting something was dropped when it shipped — corrupting the one
    artifact this feature exists to make trustworthy. So the check is on the data, not on
    the caller's word for it.
    """
    if not sections:
        raise ValueError("name at least one section to promote")
    droppable = set(dropped_intent(db, prd))
    if not droppable and not baseline_of(db, prd.id):
        raise ValueError(f"{prd.key} has no baseline — there is no agreed intent to drop")
    baselined = set(parse_sections(baseline_of(db, prd.id).body))
    unknown = [s for s in sections if s not in baselined]
    if unknown:
        raise ValueError(f"not in the governing baseline: {', '.join(sorted(unknown))}")
    delivered = [s for s in sections if s not in droppable]
    if delivered:
        raise NothingDropped(
            f"has delivered work, nothing was dropped: {', '.join(sorted(delivered))}")
    return list(sections)


def promote_to_item(db: Session, prd: Prd, section: str, *, title: str = "") -> Item:
    """Turn one piece of dropped intent into a backlog item on the same PRD.

    The lighter of the two paths: the intent stays inside this PRD and simply acquires
    work. After this the section reads as `undelivered` rather than `absent` — planned but
    not shipped, which is the honest new state.
    """
    _dropped_or_raise(db, prd, [section])
    body = section_bodies(baseline_of(db, prd.id).body).get(section, "")
    return items_svc.create_item(
        db,
        title=title or section,
        # Seeded from the BASELINE's text, not the living body — the point is to carry
        # forward what was agreed, and the body may no longer contain the section at all.
        description=f"Promoted from dropped intent in {prd.key} § {section}.\n\n{body}".strip(),
        project_id=prd.project_id,
        prd_id=prd.id,
        prd_section=section,
        status="backlog",
    )


def promote_to_prd(db: Session, prd: Prd, sections: list[str], *, title: str = "") -> Prd:
    """Create a successor PRD carrying dropped intent, linked back to this one.

    PRD-12 requires post-close changes to become a new PRD rather than reopening a closed
    one, so this is the path that keeps a terminal state terminal while letting the work
    continue somewhere honest.

    The successor's body is seeded from the predecessor's **baseline**, so what it inherits
    is what was agreed rather than whatever the body drifted to. It starts at `draft`: it
    is new intent and has to earn its own approval through its own grill, exactly like any
    other PRD. Inheriting approval would let a rebaseline that could not add sections
    launder them in through a successor instead.
    """
    promoted = _dropped_or_raise(db, prd, sections)
    bodies = section_bodies(baseline_of(db, prd.id).body)
    heading = title or f"{prd.title} — dropped scope"
    body = f"# {heading}\n\nPromoted from {prd.key}, which did not deliver these.\n\n" + "\n\n".join(
        f"## {s}\n\n{bodies.get(s, '').strip()}" for s in promoted
    ) + "\n"

    successor = create_prd(db, title=heading, project_id=prd.project_id, body=body)
    successor.supersedes_prd_id = prd.id
    successor.promoted_sections = promoted
    db.commit()
    db.refresh(successor)
    return successor


def lineage(db: Session, prd: Prd) -> dict:
    """This PRD's place in the promotion chain: what it came from, what came out of it.

    Ancestors walk backwards through `supersedes_prd_id`; successors are found by the
    reverse lookup, so nothing has to be written twice and the two views can never
    disagree. The walk is depth-capped rather than trusting the data to be acyclic — a
    cycle can only arrive through an import or a hand-edited row, and a server that hangs
    on one is a worse failure than a truncated chain.
    """
    seen, ancestors, cur = {prd.id}, [], prd
    while cur.supersedes_prd_id and len(ancestors) < 50:
        parent = db.get(Prd, cur.supersedes_prd_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        ancestors.append({"id": parent.key, "title": parent.title,
                          "promoted_sections": cur.promoted_sections or []})
        cur = parent

    successors = db.scalars(
        select(Prd).where(Prd.supersedes_prd_id == prd.id).order_by(Prd.number)
    ).all()
    return {
        # Nearest first, so `ancestors[0]` is the PRD this was promoted out of.
        "ancestors": ancestors,
        "successors": [{"id": s.key, "title": s.title, "status": s.status,
                        "promoted_sections": s.promoted_sections or []} for s in successors],
        "dropped_intent": dropped_intent(db, prd),
    }


def _structural(d: dict) -> int:
    """Structural change in a section diff, excluding renames.

    A rename moved a label, not intent. Counting it would make cosmetic churn register as
    scope change — "noise wearing a serious face", which is the AL-96 failure this whole
    feature exists to avoid repeating."""
    return len(d["modified"]) + len(d["added"]) + len(d["removed"])


def scope_drift(db: Session, prd: Prd) -> dict:
    """Mechanical scope drift — no LLM, no opinion (GRPH-243 / GRPH-315).

    Works on a stub instance with no chat provider at all, which is why it anchors the
    slice: it is the half of drift that is *countable*.

    PRD-12 holds two success criteria that pull against each other — *"drift totals never
    decrease as a result of rebaselining"* and *"never emit a number that looks like a
    measurement when it is an opinion."* A monotonic total is precisely such a number, so
    the halves are kept apart and only this one carries a figure. The judged half stays
    qualitative and stays labelled as judgement.

    Three readings, per the spec:

    - **Scope added** — items attached to the PRD after its first baseline was frozen.
    - **Intent undelivered** — baselined sections that ended with no delivered work.
    - **Spec drift** — body sections diverging from the *governing* baseline.

    The count splits into two figures that mean different things:

    - `accumulated` sums structural change across every baseline transition. Frozen
      history: nothing can lower it.
    - `current` is the live divergence of the body from the governing baseline.

    A rebaseline freezes the body as the new baseline, so the divergence it had been
    reporting as `current` becomes a chain segment of exactly the same size. `total` is
    preserved across the act rather than reset — which is what "never decreases as a
    result of rebaselining" actually demands, and it falls out of the chain rather than
    being enforced by a rule that could be forgotten.

    `total` CAN fall if an author edits the body back toward the baseline. That is correct
    and is not what the criterion forbids: they undid the drift. Only rebaselining is
    barred from lowering it, because only rebaselining could launder it.
    """
    chain = baseline_chain(db, prd.id)
    if not chain:
        # Same contract as `baseline_drift` and `completeness`: never a zero. A PRD with
        # no agreed intent has not "not drifted".
        return {"governed": False, "baseline_version": None, "accumulated": 0,
                "current": 0, "total": 0, "segments": [], "scope_added": [],
                "intent_undelivered": [], "inferred_link_times": 0}

    segments = []
    for older, newer in zip(chain, chain[1:]):
        d = diff_sections(older.body, newer.body)
        segments.append({
            "from": older.version, "to": newer.version,
            "reason_type": newer.rebaseline_reason_type,
            "structural": _structural(d),
            "renamed": len(d["renamed"]),
        })
    accumulated = sum(s["structural"] for s in segments)

    governing = chain[-1]
    live = diff_sections(governing.body, prd.body)

    # Everything attached after intent was first agreed. Measured from the FIRST baseline,
    # not the governing one: a rebaseline must not wipe the record of scope that arrived
    # before it, which is the same laundering the accumulated count exists to prevent.
    since = chain[0].created_at
    items = [it for it in items_svc.list_items(db, project_id=prd.project_id)
             if it.prd_id == prd.id]
    inferred = sum(1 for it in items if it.prd_linked_at is None)
    scope_added = [
        {"id": it.key, "status": it.status, "section": it.prd_section,
         "linked_at": (it.prd_linked_at or it.created_at).isoformat(),
         # Disclosed per item, not just in a total, so a reader can tell which rows are
         # measured and which are a fallback reading of `created_at`.
         "inferred": it.prd_linked_at is None}
        for it in items if (it.prd_linked_at or it.created_at) > since
    ]

    done = completeness(db, prd)
    return {
        "governed": True,
        "baseline_version": governing.version,
        "accumulated": accumulated,
        "current": _structural(live),
        "total": accumulated + _structural(live),
        "segments": segments,
        "scope_added": sorted(scope_added, key=lambda r: r["linked_at"]),
        # Absent and undelivered both mean "this intent has nothing delivered against it",
        # which is the one question drift is asking here. `completeness` keeps them apart
        # because the owners differ; this does not, because the reading does not.
        "intent_undelivered": sorted(done["absent"] + done["undelivered"]),
        "inferred_link_times": inferred,
        # Reported so a reader can see they were considered and deliberately not counted.
        "renamed": live["renamed"],
    }


def baseline_of(db: Session, prd_id: str) -> PrdVersion | None:
    """The agreed spec for this PRD, or None if it has never been approved. The latest
    baseline wins — a re-approval supersedes, and the earlier ones stay as history."""
    return db.scalars(
        select(PrdVersion)
        .where(PrdVersion.prd_id == prd_id, PrdVersion.is_baseline.is_(True))
        .order_by(PrdVersion.created_at.desc(), PrdVersion.id.desc())
    ).first()


def freeze_baseline(db: Session, prd: Prd) -> PrdVersion:
    """Snapshot the spec as agreed, and promote the version (AL-239).

    Called at approval — since PRD-15, when the grill concludes. Records the
    per-dimension outcomes alongside the body so a deferral is visible on the baseline
    itself.

    Idempotent per body: re-approving an unchanged spec returns the existing baseline
    rather than stacking duplicates, so a status recomputation can never quietly mint a
    second "original intent".
    """
    existing = baseline_of(db, prd.id)
    pending = prd.pending_rebaseline or {}
    # An unchanged body re-approving is a no-op — EXCEPT when a rebaseline was requested,
    # where the point may be that the intent was reaffirmed after being questioned. Even
    # then an identical body earns no new baseline; there is nothing new to freeze.
    if existing is not None and existing.body == prd.body:
        if pending:
            prd.pending_rebaseline = None
            db.commit()
        return existing

    done = completion(db, prd.id)
    prd.version = _promote(prd.version)
    row = PrdVersion(
        prd_id=prd.id,
        version=prd.version,
        date="just now",
        note=("Rebaseline — new intent, superseding the previous baseline."
              if existing is not None else "Intent baseline — the spec as approved."),
        body=prd.body,
        is_baseline=True,
        grill_outcomes={n: {"outcome": d["outcome"], "note": d["note"]}
                        for n, d in done["dimensions"].items()},
        # The chain. N+1 points BACK at N; N is never touched.
        supersedes_id=existing.id if existing is not None else None,
        rebaseline_reason_type=pending.get("reason_type", ""),
        rebaseline_reason=pending.get("reason", ""),
        requested_by=pending.get("requested_by", ""),
    )
    db.add(row)
    prd.pending_rebaseline = None
    prd.updated = "just now"
    db.commit()
    db.refresh(row)
    # Every classification was made against the intent this just superseded (GRPH-249).
    # Marking is unconditional; recomputing is not, so a rebaseline on a large PRD never
    # blocks on a hundred model calls.
    mark_classifications_stale(db, prd)
    return row


def invalidate_approval(db: Session, prd: Prd, *, reason: str) -> Prd:
    """Un-approve a PRD whose approval rested on assumptions now known to be wrong.

    Normally approval is one-way: `sync_status` never demotes, because a spec that was
    genuinely agreed stays agreed. This is the exception, and it exists because the
    alternative is worse — if the STANDARD that granted an approval turns out to be
    broken, leaving the approval standing means the baseline everything is measured
    against was never really earned, and no later work can tell.

    So: an approval is only as good as the process that granted it. When the process is
    found wanting, the approval goes with it.

    Clears the dimension verdicts (they were reached under the old standard and would
    otherwise survive as unearned passes), drops the PRD back to `review`, and removes the
    baseline that approval froze. The grill TURNS are deliberately kept — the author
    really did answer those questions, and re-grilling should not make them retype it.

    Audited as a system event, because silently un-approving something is exactly the
    kind of act that has to leave a trace.
    """
    for row in db.scalars(select(GrillDimension).where(GrillDimension.prd_id == prd.id)).all():
        db.delete(row)
    for row in db.scalars(
        select(PrdVersion).where(PrdVersion.prd_id == prd.id, PrdVersion.is_baseline.is_(True))
    ).all():
        db.delete(row)
    prd.status = "review" if grill_state(db, prd.id)["answers"] else "draft"
    prd.version = "v0.1"
    prd.updated = "just now"
    db.commit()
    db.refresh(prd)
    events_svc.record(
        db, actor_type="system", actor_label="grill", surface="system",
        action="invalidate_prd_approval", target_type="prd", target_id=prd.id,
        project_id=prd.project_id, meta={"reason": reason},
    )
    return prd


REBASELINE_REASONS = ("learning", "scope-change", "correction")


def baseline_chain(db: Session, prd_id: str) -> list[PrdVersion]:
    """Every baseline this PRD has had, oldest first. Never one row — the chain IS the
    record, and reading it is how you tell a spec that was corrected from one that kept
    moving."""
    return list(db.scalars(
        select(PrdVersion)
        .where(PrdVersion.prd_id == prd_id, PrdVersion.is_baseline.is_(True))
        .order_by(PrdVersion.created_at, PrdVersion.id)
    ).all())


class RebaselineExpandsScope(ValueError):
    """A pending rebaseline would add sections. That is a new PRD, not a rebaseline."""


def rebaseline_added_sections(db: Session, prd: Prd, body: str | None = None) -> list[str]:
    """Sections in `body` that the governing baseline has no counterpart for.

    Empty unless a rebaseline is pending — an ordinary post-approval edit may add
    whatever it likes, because it is drift and drift is the thing being measured, not
    forbidden.

    Uses `diff_sections`, so a RENAME does not read as an addition (AL-240). Without that
    this rule would block the most ordinary correction there is: retitling a section
    while fixing it.
    """
    if not prd.pending_rebaseline:
        return []
    base = baseline_of(db, prd.id)
    if base is None:
        return []
    return diff_sections(base.body, body if body is not None else prd.body)["added"]


def request_rebaseline(
    db: Session, prd: Prd, *, reason_type: str, reason: str, requested_by: str,
) -> Prd:
    """Ask for new frozen intent. Does NOT create a baseline — it re-opens the grill.

    Approval is the grill, not an authority check (PRD-12 v1.0, answer 1): a rebaseline
    is a new statement of intent, so it earns approval the way the original did, by being
    interrogated until the completion standard is met. That is also the better answer to
    laundering than a click would be — "we edited the spec to match what we built" has to
    survive being questioned, and the questions and answers are recorded.

    So this clears the dimension verdicts and drops the PRD to `review`. The EXISTING
    baseline is left completely alone: it is still the governing intent until a new one
    is earned, and anything built in the meantime is still measured against it.

    The reason is stored on the PRD's pending state and carried onto the new baseline
    when the grill completes. It is the requester's own words on purpose — an agent
    mid-work is often where new intent surfaces, and that reasoning dies with the context
    window unless something writes it down.
    """
    if reason_type not in REBASELINE_REASONS:
        raise ValueError(
            f"reason_type must be one of {list(REBASELINE_REASONS)}, got {reason_type!r}"
        )
    if not (reason or "").strip():
        raise ValueError("a rebaseline needs a stated reason in the requester's own words")
    if prd.status == "closed" or prd.close_record is not None:
        raise PrdClosed("a closed PRD can never be rebaselined; open a successor PRD instead")
    if baseline_of(db, prd.id) is None:
        raise ValueError("this PRD has no baseline to supersede; it has never been approved")

    prd.pending_rebaseline = {
        "reason_type": reason_type,
        "reason": reason.strip(),
        "requested_by": requested_by,
    }
    for row in db.scalars(select(GrillDimension).where(GrillDimension.prd_id == prd.id)).all():
        db.delete(row)
    # Open a fresh evidence window (GRPH-322). Clearing the verdicts alone was not enough:
    # classification re-read the whole transcript and re-resolved every dimension from the
    # PREVIOUS grill, so a rebaseline could reach `approved` without a single new answer.
    # The transcript is untouched — it stays as history — but nothing before this point may
    # grade the spec that replaces it.
    prd.grill_from_seq = db.scalar(
        select(func.count()).select_from(GrillTurn).where(GrillTurn.prd_id == prd.id)
    ) or 0
    prd.status = "review"
    prd.updated = "just now"
    db.commit()
    db.refresh(prd)
    events_svc.record(
        db, actor_type="agent" if requested_by.startswith("agent:") else "user",
        actor_label=requested_by, surface="mcp",
        action="request_rebaseline", target_type="prd", target_id=prd.id,
        project_id=prd.project_id, meta={"reason_type": reason_type, "reason": reason.strip()},
    )
    return prd


def sync_status(db: Session, prd: Prd) -> Prd:
    """Move the PRD's status to match its grill (AL-300 / PRD-15 D5).

        draft    — never grilled, or no answers recorded
        review   — grilled, answers recorded, dimensions still unanswered
        approved — the completion standard is met

    Called after every classification, so approval happens as a consequence of the work
    rather than as a separate act somebody has to remember.

    Two things it deliberately does NOT do:

    - **Never demote an `approved` PRD.** The ones approved under the old manual model
      (PRD-13 among them) were genuinely agreed, and recomputing history would silently
      retract that. Derivation governs transitions from here forward.
    - **Never move a PRD nobody has grilled.** A `draft` with no answers stays `draft`;
      there is nothing to derive from.
    - **Never call a once-approved PRD a `draft`.** A pending rebaseline legitimately has
      no answers yet, but a governing baseline exists and saying otherwise would report a
      spec that was agreed as one that never was.
    """
    # A closed PRD is terminal and its status is not derived from anything — recomputing
    # would silently reopen it the moment a linked item changed (GRPH-244).
    if prd.status in ("approved", "closed"):
        return prd
    done = completion(db, prd.id)
    target = "approved" if done["complete"] else ("review" if done["answers"] else "draft")
    # A PRD that has ever been approved can never read as `draft` again. Once the evidence
    # window moved (GRPH-322) a freshly requested rebaseline has zero answers *in this
    # round*, which is correct, but that is not the same as "never grilled" — there is a
    # governing baseline sitting right there, and calling it a draft would tell a reader
    # this spec was never agreed. `review` is the honest floor: agreed once, being
    # re-interrogated now.
    if target == "draft" and baseline_of(db, prd.id) is not None:
        target = "review"
    # A rebaseline that expands scope cannot earn approval, however well it was grilled
    # (GRPH-318). Checked BEFORE the status moves, so the PRD is never left approved with
    # no baseline — `freeze_baseline` refusing after the fact would produce exactly that.
    if target == "approved":
        added = rebaseline_added_sections(db, prd)
        if added:
            logger.warning("rebaseline for %s adds sections %s; refusing approval", prd.id, added)
            target = "review"
    if target != prd.status:
        prd.status = target
        prd.updated = "just now"
        db.commit()
        db.refresh(prd)
        # Approval is the moment the spec was agreed, so the baseline freezes HERE
        # rather than waiting for anyone to remember (AL-239, and what AL-302 described).
        if target == "approved":
            freeze_baseline(db, prd)
    return prd


def grill_state(db: Session, prd_id: str) -> dict:
    """What the server knows about this PRD's grill, with no client involved. The shape
    AL-297 hangs per-dimension outcomes off, and what proves this item works: a fresh
    session can answer it."""
    turns = grill_turns(db, prd_id)
    window = grill_window(db, prd_id)
    done = completion(db, prd_id)
    return {
        "prd_id": prd_id,
        "turns": [{"seq": t.seq, "role": t.role, "text": t.text,
                   "via": t.via, "actor": t.actor} for t in turns],
        "questions": sum(1 for t in turns if t.role == "agent"),
        # Counted inside the evidence window, matching what `completion` grades. `turns`
        # above stays the FULL transcript on purpose: history is never hidden, and a
        # reader needs to see that earlier rounds happened. `grill_from_seq` is where the
        # current interrogation begins, so a UI can draw the line (GRPH-322).
        "grill_from_seq": window,
        "answers": sum(1 for t in turns if t.role == "user" and t.seq >= window),
        "grilled": any(t.role == "user" and t.seq >= window for t in turns),
        # The completion standard, so one call answers both "what was said" and
        # "is it finished" — AL-300 derives status from exactly this.
        "dimensions": done["dimensions"],
        "outstanding": done["outstanding"],
        "deferred": done["deferred"],
        "complete": done["complete"],
        # Whether the BODY has caught up with what the grill settled (GRPH-430). A finished
        # grill and a stale document look identical from every downstream surface, so the
        # one place that knows both says so.
        "absorption": grill_absorption(db, prd_id),
    }


def grill_absorption(db: Session, prd_id: str) -> dict:
    """Has the BODY been edited since the grill last said something?

    GRPH-424 closed *repo copy vs ledger*. This is the same absence one level in — *ledger
    body vs its own grill*. A PRD can be interrogated across five rounds, settle real
    decisions, and keep a body that still describes the older ones; to `decompose_prd`,
    `prd_coverage`, the completeness pass and any human reading it, that document is
    indistinguishable from one that absorbed everything.

    **Staleness, never correctness.** Whether a section genuinely *reflects* an answer is a
    judgement, and a check that claimed to make it would either nag constantly or pass on
    anything — both of which end with somebody switching it off. What is exact is the
    ordering: answers newer than the last body edit have demonstrably not been written down.
    That is the failure that actually happens.

    Counted inside the evidence window (`grill_from_seq`), matching what `completion` grades:
    after a rebaseline the previous round's answers are not what this body owes.

    A PRD nobody has grilled is absorbed by definition — there is nothing outstanding to
    absorb, and reporting it as stale would make the signal meaningless on the majority of
    rows.
    """
    prd = db.get(Prd, keys.resolve_prd(db, prd_id) or prd_id)
    if prd is None:
        return {}
    window = grill_window(db, prd.id)
    answers = [t for t in grill_turns(db, prd.id) if t.role == "user" and t.seq >= window]
    # `body_updated_at` is nullable for rows that predate 0082. Falling back to `updated_at`
    # keeps those optimistic rather than flagging the whole backlog on the day this ships.
    edited = prd.body_updated_at or prd.updated_at

    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt

    edited = _aware(edited)
    behind = [t for t in answers if edited is None or _aware(t.created_at) > edited]
    latest = max((_aware(t.created_at) for t in answers), default=None)
    return {
        "prd_id": prd.key,
        "absorbed": not behind,
        "answers_behind": len(behind),
        "behind_seqs": [t.seq for t in behind],
        "last_answer_at": latest.isoformat() if latest else None,
        "body_updated_at": edited.isoformat() if edited else None,
    }


def capture_grill_decisions(db: Session, prd: Prd, history: list[dict]) -> list:
    """Preserve the author's decisions from a grill as candidate memory shards
    (AL-69). Every answer becomes a `candidate` shard (origin `agent:grill`) that
    flows through Memory Review (AL-49) and clustering (AL-50) — so a decision
    can't evaporate when context is cleared (the preservation principle). Deduped
    by (source, text) so re-applying doesn't pile up copies."""
    from app.services import memory as mem_svc

    source = f"grill: {prd.id}"
    existing = {
        s.text
        for s in mem_svc.list_shards(db, project_id=prd.project_id, status="candidate")
        if s.source == source
    }
    created = []
    for m in history or []:
        text = (m.get("text") or "").strip()
        if m.get("role") != "user" or len(text) < 8 or text in existing:
            continue
        shard = mem_svc.add_memory(
            db, text_body=text, scope="global", source=source,
            project_id=prd.project_id, status="candidate", origin="agent:grill",
        )
        existing.add(text)
        created.append(shard)
    return created


def grill_apply(db: Session, prd_id: str, history: list[dict]) -> str:
    """Synthesize an updated PRD body that folds in the decisions from a grill
    transcript (the handoff). Returns the proposed body; the caller saves it."""
    prd = get_prd(db, prd_id)
    if prd is None:
        raise ValueError(f"prd not found: {prd_id}")
    # Snapshot BEFORE proposing a wholesale rewrite (AL-239). `GRILL_APPLY_SYSTEM` asks
    # the model to return the FULL body, so whatever comes back replaces everything —
    # and until now nothing preserved what was there. An ordinary snapshot, not a
    # baseline: this records what the author had, not what anyone agreed to.
    if (prd.body or "").strip():
        db.add(PrdVersion(prd_id=prd.id, version=prd.version, date="just now",
                          note="Before folding in grill decisions.", body=prd.body))
        db.commit()
    _r = platform_svc.resolve_chat(db, prd.project_id)
    provider, chat = _r.provider_id, _r.chat
    if provider == "stub":
        answers = [m.get("text", "").strip() for m in history if m.get("role") == "user"]
        answers = [a for a in answers if a]
        if not answers:
            return prd.body
        block = "## Decisions from grilling\n" + "\n".join(f"- {a}" for a in answers) + "\n"
        return prd.body.rstrip() + "\n\n" + block
    return chat.chat(
        system=GRILL_APPLY_SYSTEM,
        context=grill_context(prd, history),
        question="Return the updated PRD markdown body incorporating the decisions above.",
    )


def _stub_command(command: str, prd: Prd) -> str:
    """Deterministic, offline output so the editor is useful without a provider."""
    if command == "grill":
        secs = parse_sections(prd.body)
        thin = [s for s in secs if len(section_bodies(prd.body).get(s, "").strip()) < 40]
        # One question per dimension, in DIMENSIONS order, so the offline grill asks
        # exactly what the completion standard grades (AL-298). Previously a fixed list
        # that overlapped the dimensions by coincidence.
        lines = [f"- {q}" for q in DIMENSIONS.values()]
        for s in thin[:3]:
            lines.append(f"- Section **{s}** is thin — what belongs there?")
        return "\n".join(lines) + (
            "\n\n_(Local stub questions. Set CHAT_PROVIDER=ollama|anthropic for a real grill.)_\n"
        )
    if command == "risks":
        return (
            "## Risks & Open Questions\n"
            "- Scope creep beyond the stated non-goals.\n"
            "- Dependencies on linked items may slip the timeline.\n"
            "- Success metrics need a measurement plan.\n"
            "\n_(Generated by the local stub. Set CHAT_PROVIDER=ollama or anthropic for real drafting.)_\n"
        )
    if command == "summarize":
        first = next((ln for ln in prd.body.splitlines() if ln and not ln.startswith("#")), "")
        return f"**Summary:** {prd.title} — {first or 'no overview yet'}. _(stub summary)_\n"
    return (
        "\n_Expanded draft placeholder. Configure a chat provider (CHAT_PROVIDER=ollama|anthropic) "
        "to generate real prose here._\n"
    )


# ---- Spec-to-task traceability & coverage (feature D) ----

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# High-fidelity signal: work whose answer needs a prototype to see/feel, not words
# (AL-68). Heuristic over the section title + body; a human can always override.
_HIGH_FIDELITY_RE = re.compile(
    r"\b(ui|ux|visual|design|layout|interaction|animation|feel|look|screen|"
    r"mockup|wireframe|prototype|gesture|responsive|styling|aesthetic|onboarding flow)\b",
    re.IGNORECASE,
)


def classify_fidelity(text: str) -> str:
    """`high` when the text is about how something looks/feels/behaves (needs a
    prototype), else `low` (specifiable in words now)."""
    return "high" if _HIGH_FIDELITY_RE.search(text or "") else "low"


#: Opens or closes a fenced code block. Three or more backticks/tildes at line start; an
#: opener may carry a language tag, a closer may carry trailing space.
_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})", re.MULTILINE)


def body_hash(body: str) -> str:
    """What "you have read this PRD" means, exactly (GRPH-357).

    A hash, not `version`: `version` only moves on an explicit `create_version` snapshot, so
    two entirely different bodies routinely share one and it cannot answer the question that
    matters — is the document still what you read? The same choice `gbagent` makes for files
    (GRPH-515), for the same reason.

    Truncated to 16 hex characters. It rides on every `get_prd` and back on every full-body
    `update_prd`, and the MCP manifest is measured in tokens; 64 bits is far beyond what an
    accident could collide, and this defends against forgetting to read, not against an
    attacker who already holds a write credential.
    """
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]


def section_fingerprint(body: str, title: str) -> str:
    """What "this item was decomposed from that text" means, exactly (GRPH-360).

    Hashes the section's markdown as `section_bodies` renders it, which is the same text
    `decompose_prd` copies into the item's description — so the thing fingerprinted and the
    thing copied cannot disagree. Returns "" for a section that is not there, because an
    item whose section has been RENAMED OR DELETED is a different report from one whose
    section has been edited, and flattening them into a hash mismatch would say the wrong
    thing loudly.
    """
    bodies = section_bodies(body)
    return body_hash(bodies[title]) if title in bodies else ""


#: What an item's relationship to its section can be. `unknown` is a real answer, not a
#: fallback — see `section_drift`.
DRIFT_STATES = ("agrees", "drifted", "acknowledged", "section_gone", "unknown")


def section_drift(item, body: str) -> dict:
    """Has the PRD section this item was decomposed from moved since? (GRPH-360)

    Read-only, and deliberately never rewrites anything. An item is legitimately edited away
    from its section — retitled, split, narrowed after a spike, annotated with what the build
    actually found. Auto-syncing would destroy that, which is the same class of mistake as
    re-asking a question somebody already answered. So this reports; a human decides.

    Five answers, and the two that are not about drift matter most:

    * `unknown` — no fingerprint. Every item created before this column, and every item
      linked to a PRD by hand. It is NOT `agrees`. On PRD-17 nine of eleven items contradicted
      the spec while `prd_coverage` read 100%, because a check that cannot tell reported
      nothing; a silent pass is how that happened, and `unknown` is the fix for it.
    * `section_gone` — the section was renamed or deleted out from under the item. A hash
      comparison would call that "drifted", which is true but useless: nobody can diff against
      text that is not there, and the remedy is relinking, not re-reading.
    """
    title = item.prd_section or ""
    if not title:
        return {"state": "unknown", "reason": "not linked to a section"}
    bodies = section_bodies(body or "")
    if title not in bodies:
        return {"state": "section_gone", "reason": f"no section {title!r} in the PRD any more"}
    current = body_hash(bodies[title])
    stamped = getattr(item, "prd_section_hash", "") or ""
    if not stamped:
        return {"state": "unknown",
                "reason": "no fingerprint — created before this was recorded, or linked by hand"}
    if current == stamped:
        return {"state": "agrees", "reason": "the section is unchanged since this was created"}
    if (getattr(item, "prd_section_ack", "") or "") == current:
        # Acknowledged AGAINST THIS TEXT, not forever. A later edit produces a new hash and
        # flags again, which is the difference between "I have read the change" and "stop
        # telling me about this section".
        return {"state": "acknowledged", "reason": "this divergence was reviewed and kept"}
    return {"state": "drifted",
            "reason": "the section has been edited since this item was created; the item's "
                      "description is the OLD text and the PRD is the source"}


class StaleBody(ValueError):
    """Tried to replace a body that has moved since it was read."""


class SectionNotFound(ValueError):
    """Named a `## ` section the PRD does not have."""


class AmbiguousSection(ValueError):
    """Named a section title the PRD carries more than once."""


def section_spans(body: str) -> list[tuple[str, int, int]]:
    """`(title, content_start, content_end)` for every `## ` section, in order.

    FENCE-AWARE, which `_SECTION_RE` on its own is not. A line reading `## Example` inside
    a ```` ``` ```` block is sample text, not a heading, and treating it as one puts a
    section boundary in the middle of a code block. No PRD in this repo does that today —
    checked — so this is a latent hazard rather than a live bug, and it stays latent only
    because nothing had yet written bytes back based on these offsets. `replace_section`
    does, and getting a boundary wrong there destroys a document silently, which is the
    exact failure GRPH-357 exists to remove.

    `parse_sections` derives from this so the two cannot disagree about what a section is —
    a splitter and a lister that drift apart would corrupt precisely the documents that are
    hardest to notice corruption in.

    `content_start` is the first byte after the heading line; `content_end` is the first
    byte of the next heading, or the end of the body. The heading line itself is owned by
    neither, so a caller can never rename a section by editing its contents.
    """
    text = body or ""
    heads: list[tuple[str, int, int]] = []   # title, heading_start, content_start
    fenced = False
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if _FENCE_RE.match(stripped):
            fenced = not fenced
        elif not fenced:
            m = _SECTION_RE.match(stripped)
            if m:
                heads.append((m.group(1).strip(), pos, pos + len(line)))
        pos += len(line)
    return [(title, cstart, heads[i + 1][1] if i + 1 < len(heads) else len(text))
            for i, (title, _hstart, cstart) in enumerate(heads)]


def parse_sections(body: str) -> list[str]:
    """Level-2 headings (`## …`) — the PRD's sections, in order."""
    return [title for title, _s, _e in section_spans(body)]


def section_content(body: str, title: str) -> str:
    """One section's prose, without the heading line or its trailing blank lines."""
    return body[_span_for(body, title)[0]:_span_for(body, title)[1]].strip("\n")


def _span_for(body: str, title: str) -> tuple[int, int]:
    """Resolve a section title to its content span, comparing the way the rest of this
    module does — `_section_key` — so an agent is not made to reproduce punctuation and
    casing exactly to edit a section it just read."""
    want = _section_key(title)
    hits = [(s, e) for t, s, e in section_spans(body) if _section_key(t) == want]
    if not hits:
        have = parse_sections(body)
        raise SectionNotFound(
            f"no section {title!r}. This PRD has: {', '.join(repr(h) for h in have) or 'none'}")
    if len(hits) > 1:
        # Refused rather than picking the first. Two sections with the same title is a
        # malformed document, and quietly editing one of them is how the other silently
        # becomes the stale copy nobody is looking at.
        raise AmbiguousSection(
            f"{title!r} appears {len(hits)} times in this PRD — rename one before editing")
    return hits[0]


def replace_section(body: str, title: str, content: str) -> str:
    """Return `body` with ONE section's contents replaced. Every other byte is untouched.

    That is the whole point (GRPH-357). `update_prd` replaces the body wholesale, so an
    agent asked to record one decision had to reproduce the entire document from memory —
    and on an approved PRD, anything it failed to reproduce was simply gone, with no
    snapshot written on the MCP path to recover from.

    Whitespace around the section is normalised to the form every PRD in this repo already
    uses — a blank line after the heading, a blank line before the next one — so a caller
    that sends `"Decided: X"` with no trailing newline cannot weld its text onto the
    following `## `, and one that sends a dozen trailing blank lines cannot drift the
    document. Normalisation touches only the edited section; everything outside the span is
    spliced back byte for byte.
    """
    start, end = _span_for(body, title)
    tail = body[end:]
    text = (content or "").strip("\n")
    return body[:start] + "\n" + text + ("\n\n" if tail else "\n") + tail


# Conventional PRD sections that FRAME the work rather than being work themselves.
# Treating every `## ` heading as implementable made decompose propose non-tasks
# ("Implement: Problem") and made coverage report false gaps, which trains you to
# ignore the metric (AL-96). Compared on an alphanumeric-only key so punctuation,
# casing, and a trailing "(v1)" don't matter.
_PROSE_SECTIONS = {
    "problem", "background", "context", "overview", "motivation", "summary",
    "goal", "goals", "nongoal", "nongoals", "outofscope",
    "successcriteria", "successmetrics", "openquestions",
    "appendix", "glossary", "references", "priorart",
    # planning / risk framing — describe the work or its rollout, not buildable work (AL-198)
    "risks", "risksandopenquestions", "risksopenquestions",
    "risksandmitigations", "risksmitigations",
    "phasing", "phases", "rollout", "rolloutplan", "milestones", "timeline", "faq", "faqs",
    # Written BY `grill_apply`, and since PRD-15 made grilling the approval path it lands
    # on essentially every approved PRD. It records why decisions were settled, not work
    # to build — so leaving it implementable reported a false gap on almost every PRD in
    # the instance, and would have made "nothing delivered here" the headline finding of
    # the completeness pass (GRPH-251) every single time. Noise wearing a serious face is
    # the AL-96 failure, and it is at its most expensive in the one output whose entire
    # value is being trusted about absence. Matched narrowly: a section called plain
    # "Decisions" may well be design decisions that do need building.
    "decisionsfromgrilling",
}


# A leading section NUMBER, which carries no meaning for classification. Requires a real
# separator (`1. ` / `2.1) `) so a heading that genuinely starts with digits — "2xx responses" —
# keeps them.
_SECTION_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*[.)]\s+")


def _section_key(title: str) -> str:
    """Normalize a heading for classification: drop a leading section number and any
    parentheticals, then keep only alphanumerics — so "Non-goals (v1)", "Non Goals",
    "7. Non-goals" and "nongoals" all agree.

    **The number used to survive**, and it silently defeated the whole classification: every
    PRD in this repo numbers its headings, so "1. Overview" keyed as `1overview`, missed
    `_PROSE_SECTIONS`, and was treated as buildable. That put framing prose into
    `decompose_prd`'s proposals AND — worse, because it is a number somebody reads — counted
    Overview, Goals and Non-goals as sections owing delivery in the PRD-12 completeness
    rollups. An unnumbered PRD classified correctly, so nothing looked wrong.
    """
    cleaned = _SECTION_NUMBER.sub("", title or "")
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\(.*?\)", " ", cleaned).lower())


def is_implementable_section(title: str) -> bool:
    """Whether a section describes work to build (vs. framing prose)."""
    return _section_key(title) not in _PROSE_SECTIONS


def section_bodies(body: str) -> dict[str, str]:
    """Map each `## section` to the markdown beneath it (until the next `## `).

    Derived from `section_spans` rather than walking the lines itself (GRPH-360). It used to
    be a third independent parser — after `parse_sections` was folded into the spans in
    GRPH-357, this one was still matching `^##` line by line with no idea what a code fence
    is, so a PRD containing a markdown example would have had `decompose_prd` and
    `replace_section` disagreeing about where a section ends.

    That was already worth fixing. It became load-bearing here: the drift fingerprint hashes
    what decompose COPIED, and if the copier and the checker disagree about the boundary,
    every item on such a PRD reads as drifted the moment it is created.

    `.strip()` rather than `.strip("\n")`, matching the behaviour this replaced — callers
    compare and display these bodies, and leading indentation on the first line was never
    part of them.
    """
    return {title: body[start:end].strip() for title, start, end in section_spans(body)}


def coverage(db: Session, prd: Prd) -> dict:
    """Per-section task rollup + gaps for a PRD."""
    sections = parse_sections(prd.body)
    items = [it for it in items_svc.list_items(db, project_id=prd.project_id) if it.prd_id == prd.id]
    by_section: dict[str, list] = {}
    for it in items:
        by_section.setdefault(it.prd_section or "", []).append(it)
    per = []
    for s in sections:
        its = by_section.get(s, [])
        counts = Counter(it.status for it in its)
        # High-fidelity work still open in this section = prototype-first questions
        # a spec can't close in words yet (AL-68).
        open_high = sum(1 for it in its if it.fidelity == "high" and it.status != "done")
        implementable = is_implementable_section(s)
        per.append({
            "section": s,
            "implementable": implementable,
            "item_count": len(its),
            "done": counts.get("done", 0),
            "by_status": dict(counts),
            # Framing prose is never a gap — only buildable sections can lack work (AL-96).
            "gap": implementable and len(its) == 0,
            "high_fidelity": sum(1 for it in its if it.fidelity == "high"),
            "open_high_fidelity": open_high,
            # The rendered key, not the frozen id — otherwise a retagged project reports
            # its work under the tag it no longer holds, and the ids coverage hands back
            # are ones the UI and the agent surface no longer use (PRD-13).
            "item_ids": [it.key for it in its],
            # Per item, because "this section has drift" is not actionable — which item
            # holds the stale copy is (GRPH-360). Reported HERE, on the surface that
            # previously said 100% covered while nine of eleven items contradicted the
            # spec: a drift report nobody looks at would repeat that failure exactly.
            "drift": [{"id": it.key, **section_drift(it, prd.body or "")} for it in its],
        })
    total = len(items)
    done = sum(1 for it in items if it.status == "done")
    buildable = [p for p in per if p["implementable"]]
    return {
        # The RENDERED key, like every sibling return in this module. `prd.id` is frozen at
        # issue time, so a retagged project reports itself under a tag it no longer holds:
        # `AL-P14` and `AL-P15` still surface that way, and `PRD-1`..`PRD-13` carry no project
        # marker at all. The identical fix was already applied to `item_ids` six lines below,
        # with the reason spelled out, and these two fields in the same dict were missed.
        # Safe to round-trip: `keys.resolve_prd` accepts a rendered key and a frozen id.
        "prd_id": prd.key, "title": prd.title, "status": prd.status,
        "sections": per,
        # Items pointing at a section this PRD no longer has (GRPH-360). They are NOT in
        # `sections` above, which iterates the PRD's own headings — so a rename silently
        # dropped them out of coverage entirely, and the item went on holding rules from a
        # section nobody could find. Found by the test for `section_gone`, which could not
        # fire until this existed: the state was reachable in `section_drift` and
        # unreachable through the surface that reports it.
        "orphaned": [{"id": it.key, "section": it.prd_section,
                      **section_drift(it, prd.body or "")}
                     for it in items
                     if it.prd_section and it.prd_section not in set(sections)],
        # The rollup, so a caller that reads nothing else still cannot miss it. Counted per
        # STATE rather than as one boolean: "3 drifted, 2 unknown" and "3 drifted" call for
        # different next moves, and a single `has_drift` flag would hide the unknowns behind
        # the knowns.
        "drift_counts": {
            st: (sum(1 for p in per for d in p["drift"] if d["state"] == st)
                 + sum(1 for it in items
                       if it.prd_section and it.prd_section not in set(sections)
                       and section_drift(it, prd.body or "")["state"] == st))
            for st in DRIFT_STATES
        },
        # `section_count` stays the total for continuity; coverage is measured against
        # the buildable subset so prose can't drag the ratio down.
        "section_count": len(sections),
        "implementable_sections": len(buildable),
        "sections_with_tasks": sum(1 for p in buildable if not p["gap"]),
        "gaps": [p["section"] for p in buildable if p["gap"]],
        "total_items": total, "done_items": done,
        "percent_done": round(100 * done / total) if total else 0,
        # Prototype-first work outstanding across the whole PRD.
        "open_high_fidelity": sum(1 for it in items if it.fidelity == "high" and it.status != "done"),
    }


# Relative pointers that are true inside a document and false inside a task. "Above" and
# "below" are the common ones; the rest showed up in the PRD-13 items that had to be
# hand-rewritten. Matched on word boundaries so "aboveboard" and "belowdecks" survive.
_DANGLING_REF = re.compile(
    r"\b(?:above|below|earlier in this|later in this|the (?:first|second|last|previous|next|preceding|following) section)\b",
    re.IGNORECASE,
)


#: How much framing prose one task may carry. PRD-21's block is 13,345 characters — about
#: 3,336 tokens, on EVERY item decomposed from it. For scale, the whole MCP manifest is
#: ~13,150 tokens, a ceiling this repo has raised five times and argued each time, and a
#: session-scoped trimmer exists to save 2,100–2,400 tokens per turn. An item quietly
#: carrying a quarter of that deserved a number rather than none (GRPH-428).
#:
#: 8,000 characters is roughly 2,000 tokens, and it is measured rather than picked. Framing
#: across the five PRDs in this repo runs 7,461-15,819 chars. At this budget PRD-21 drops
#: only its Problem section — the largest and most narrative — while keeping the invariant,
#: the goals and the risks; PRD-22 fits whole. 6,000 cut PRD-20 to its Overview alone;
#: 10,000 bought almost nothing more for 25% more on every item.
#:
#: Sections go in DOCUMENT ORDER, because every PRD here states its rules first, so the cap
#: drops narrative rather than the thing an implementer cannot build without. The known cost
#: of that rule: a small early section can displace a more useful later one — at 6,000,
#: PRD-21's Phasing (1,275 chars) pushed out its Risks (1,995). Ordering by usefulness would
#: need judgement the tool does not have, so the budget is set where that trade stops biting
#: rather than pretending to solve it.
#:
#: What does not fit is NAMED, because a block that is silently short reads exactly like a
#: PRD with nothing more to say.
FRAMING_BUDGET_CHARS = 8_000


def framing_context(prd: Prd) -> str:
    """The framing sections, assembled so a task can carry its own spec.

    `decompose` lifts one section body verbatim, which reads correctly inside a document
    and incompletely outside one. Every rule a PRD states — an invariant, a charset, a set
    of assigned values, the reason a decision went the way it did — lives in framing prose
    that the implementable sections assume the reader has already seen. Extracted alone,
    the section is a paragraph about *what* with no *how* and no *why*.

    That is not hypothetical: on PRD-13 the ID grammar, the tag charset `^[A-Z][A-Z0-9]{1,3}$`
    and the five tag assignments all lived in `## Context`, and none of them reached the six
    items that had to implement them. All six were hand-rewritten to be self-contained,
    which is the exact cost `decompose_prd` exists to remove.

    So the framing goes WITH the work rather than being left behind for someone to go and
    find. It is duplicated onto every item on purpose: an item that has to fetch its parent
    to be actionable is the failure this repairs, and bloat is the cheaper of the two.
    """
    bodies = section_bodies(prd.body)
    kept: list[str] = []
    dropped: list[str] = []
    spent = 0
    for title, body in bodies.items():
        if is_implementable_section(title):
            continue
        text = (body or "").strip()
        if not text:
            continue
        block = f"### {title}\n\n{text}"
        # Whole sections, never a cut sentence. A section that half-arrives is worse than
        # one that is named as absent: the reader cannot tell where the rule stopped.
        if spent + len(block) > FRAMING_BUDGET_CHARS and kept:
            dropped.append(title)
            continue
        kept.append(block)
        spent += len(block)
    if not kept:
        return ""
    if dropped:
        kept.append(
            "### Not carried\n\n"
            f"Over the {FRAMING_BUDGET_CHARS}-character budget, so these are in the PRD and "
            f"not here: {', '.join(dropped)}. Read them there before assuming they say "
            "nothing you need."
        )
    return "\n\n".join(kept)


def dangling_refs(text: str) -> list[str]:
    """Relative references in a section body that stop resolving once it is a task.

    Reported rather than rewritten. "The five tags above" could be repaired by guessing
    which five, and a wrong guess is worse than a visible dangle — the reader who sees
    "above" knows to go looking, while a confidently wrong substitution reads as fact.
    """
    seen: list[str] = []
    for line in (text or "").splitlines():
        if _DANGLING_REF.search(line):
            trimmed = line.strip()
            if trimmed not in seen:
                seen.append(trimmed)
    return seen


def decompose(db: Session, prd: Prd, create: bool = False, include_prose: bool = False) -> dict:
    """Propose one tracked task per un-covered section (gap). With create=True, creates them
    as backlog items linked to the PRD + section, so the spec drives the tracker.

    Framing sections (Problem, Goals, Non-goals, Success criteria, …) are skipped — they
    describe the work, they aren't work (AL-96). Pass ``include_prose=True`` when a PRD
    genuinely uses one of those headings for buildable scope."""
    cov = coverage(db, prd)
    bodies = section_bodies(prd.body)
    context = framing_context(prd)
    proposals = []
    for p in cov["sections"]:
        if not include_prose and not p["implementable"]:
            continue
        if p["item_count"]:  # already covered by tracked work
            continue
        body = bodies.get(p["section"], "").strip()
        # A section about how something looks/feels needs a prototype first (AL-68).
        fidelity = classify_fidelity(f"{p['section']} {body}")
        described = body
        if context:
            # The section first, because that is the work. The spec follows it as context
            # rather than preceding it, so an implementer reads their task before the
            # material that frames it.
            described = (
                f"{body}\n\n---\n\n"
                f"## Context from {prd.key} — {prd.title}\n\n"
                f"Carried with the task on purpose: an item that has to fetch its parent "
                f"to be actionable is the gap this fills. Framing sections only — the "
                f"other implementable sections are their own items.\n\n"
                # A SNAPSHOT, and it now says so. Re-decompose skips a section that
                # already has an item, so this copy is never refreshed — a PRD edited or
                # rebaselined afterwards leaves every task holding the rules as they
                # were. `intent_hold` warns that intent MOVED, which an agent can
                # reasonably read as "scope changed" rather than "the spec in your
                # description is wrong"; the stamp makes the second reading available.
                f"*Copied from {prd.key} at {prd.version}. A snapshot: if this item shows "
                f"an intent hold, or the PRD has moved since, these are the old rules and "
                f"the PRD is the source.*\n\n{context}"
            )
        proposals.append({
            "section": p["section"],
            "title": f"Implement: {p['section']}",
            "description": described,
            "fidelity": fidelity,
            # Surfaced on the proposal so a dry run shows them before anything is created.
            "dangling_refs": dangling_refs(body),
        })
    created = []
    if create:
        for pr in proposals:
            item = items_svc.create_item(
                db, title=pr["title"], description=pr["description"],
                project_id=prd.project_id, status="backlog",
                tags=["prd", "prototype"] if pr["fidelity"] == "high" else ["prd"],
                fidelity=pr["fidelity"],
                prd_id=prd.id, prd_section=pr["section"],
                reporter={"name": "Spec", "handle": "prd", "avatar": "#c9b8ff"},
            )
            # Stamped at creation, so the description's snapshot has something to be
            # compared against later. Set here rather than passed through `create_item`:
            # this is a fact about how the item was DERIVED, not a field a caller supplies,
            # and an item created by hand must not be able to claim a provenance it has not
            # got — an unfingerprinted item reports UNKNOWN, which is the truth.
            item.prd_section_hash = section_fingerprint(prd.body or "", pr["section"])
            db.commit()
            created.append(item.id)
    # Rendered, as in `coverage` above. NOT the same as the `prd_id=prd.id` written onto each
    # created item further up: that column holds the frozen id deliberately (GRPH-319), because
    # a stored rendering would rot the moment the project is retagged. Displayed keys render;
    # stored ids freeze — and this dict is read, not stored.
    return {"prd_id": prd.key, "proposals": proposals, "created": created}
