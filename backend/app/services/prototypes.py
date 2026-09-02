"""Automated prototype handoff (GRPH-235).

AL-68 made a high-fidelity item *visible* — `fidelity="high"`, the `open_high_fidelity`
coverage count, the "needs a prototype" nudge. What it never built was the literal
handoff: `grill → prototype → grill again` was three manual hops with a human carrying
state between the tools. This service closes two of them:

- **emit** composes the prompt-pack — the shared design preamble plus the specific
  question the prototype exists to settle — so the author pastes ONE block into Claude
  Design instead of re-deriving the context every time. Generation itself stays manual
  by design (the grill settled this): the repo has no browser tooling, and the design
  docs' consistency rules exist precisely because output is curated by eye.
- **record_verdict** brings the result back: the screenshot lands in the existing
  attachment store, a `screenshot` receipt appears on the item, and — the part that makes
  this a loop rather than an open-a-browser button — the human's verdict re-enters the
  grill as a real turn citing the artifact URL, so the next `classify_grill` round grades
  it against the window it already uses.

The fidelity flip is deliberately NOT here. A wrong auto-flip silently removes the very
signal AL-68 exists to surface, so the verdict response only *proposes* `high → low` and
the author confirms it on the item. Likewise there is no vision path: providers are
text-only, so what re-enters the grill is the verdict on the artifact, not the artifact's
pixels.

Grill turns are text-only by model (`GrillTurn {seq, role, text}`), so the artifact
re-enters by REFERENCE — the public attachment URL inside the turn text, which
`classify_grill`'s citation rules can point a reader at.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Item, Prd
from app.services import attachments as att_svc
from app.services import items as items_svc
from app.services import prds as prds_svc

# Condensed from `docs/design/cloud-tenant-design-prompts.md` Part 1 — the shared
# preamble every screen prompt is pasted under. Kept in code because the handoff must
# emit it programmatically; the doc stays the human-facing source of the fuller version,
# and this constant carries its substance (product frame, visual system, consistency
# rules) rather than duplicating its screen roster.
PREAMBLE = """\
**Product.** Graphban is an agent-native dev tool: a linear tracker + pgvector agent
memory + request triage + a code-structure graph, all operable by coding agents through
MCP tools. It ships local-first (a self-hosted box) and cloud-hosted (a multi-tenant
SaaS holding items, claims, memory and triage across an org).

**Visual system (match exactly):**
- Dark canvas `#0d0f0e`; surfaces `#111412` (cards) and `#0b0d0c` (insets/inputs);
  borders `#20241f` (strong) / `#1b1f1a` (subtle).
- Type: IBM Plex Sans for UI, IBM Plex Mono for labels, values, IDs, codes. Micro-labels
  mono, 10px, UPPERCASE, letter-spacing ~.7px, color `#5b6355`.
- Text: primary `#e6e9e4`, secondary `#dfe4da`, muted `#868f80`, faint `#5b6355`.
- Accent lime `#c6f24e` (hover `#d8ff74`) — primary actions, active nav, selection bars.
- Status: green `#5fd07a`, amber `#e2b247`, blue `#7ca2ff`, red `#e85d5d`, purple `#a78bfa`.
- Left rail for top-level nav; status pills; content column ~640–760px; never invent a
  fifth accent color."""

# How much of the item description rides along in the prompt-pack. Enough to orient the
# design; not so much that a 5k-word item body buries the one question being settled.
_DESCRIPTION_BUDGET = 600


def emit_prompt_pack(
    db: Session, prd: Prd, item: Item,
    *, dimension: str = "open_decisions", note: str = "",
) -> dict:
    """Compose the paste-ready prompt-pack for one high-fidelity question.

    Also records the handoff as a server-authored grill turn, so "was this ever handed
    off?" is answerable from the PRD's own transcript — and a later verdict turn has a
    real `seq` nearby to read against. The turn is deliberately compact: the full pack is
    re-derivable from this function, and pasting 30 lines of palette into the transcript
    would pollute the evidence `classify_grill` grades.
    """
    if dimension not in prds_svc.DIMENSIONS:
        raise ValueError(
            f"unknown grill dimension: {dimension!r} (expected {sorted(prds_svc.DIMENSIONS)})"
        )
    question = prds_svc.DIMENSIONS[dimension]
    desc = (item.description or "").strip()
    if len(desc) > _DESCRIPTION_BUDGET:
        desc = desc[:_DESCRIPTION_BUDGET].rstrip() + " …"

    parts = [
        PREAMBLE,
        "",
        "---",
        "",
        f"**Screen prompt — {item.key}: {item.title}**",
        "",
        f"This screen exists to settle one grill question that prose cannot:",
        f"*{question}* (dimension: `{dimension}`)",
    ]
    if desc:
        parts += ["", f"**Context — the item being specced.**", "", desc]
    if (note or "").strip():
        parts += ["", f"**Focus.** {note.strip()}"]
    parts += [
        "",
        "Produce ONE `<Screen Name>.dc.html` screen. Render the states the question turns "
        "on — empty, error, and the specific ambiguous case above — not just the happy "
        "path. When you have looked at it, bring the verdict back to the grill (paste it "
        "into the PRD's prototype panel with a screenshot); the screenshot alone is not "
        "the answer, what you concluded from it is.",
    ]
    prompt_pack = "\n".join(parts)

    turn = prds_svc.append_grill_turn(
        db, prd.id, role="agent", via="prototype",
        text=(f"Prototype handoff emitted for {item.key} ({dimension}): {item.title}. "
              f"Awaiting a verdict that cites the artifact."),
    )
    return {
        "prd": prd.key, "item": item.key, "dimension": dimension,
        "prompt_pack": prompt_pack, "turn_seq": turn.seq,
    }


def record_verdict(
    db: Session, prd: Prd, item: Item,
    *, attachment_id: str, verdict: str, dimension: str = "open_decisions",
) -> dict:
    """Capture the prototype result and feed the human's verdict back into the grill.

    Three writes, one per hop of the loop the item describes:
    1. the artifact — an existing attachment (uploaded via `/api/public/attachments`),
       referenced, never copied;
    2. a `screenshot` evidence receipt on the item, appended through `update_item` so the
       record only grows (GRPH-494);
    3. a USER grill turn carrying the verdict text plus the artifact URL — the only shape
       the text-only pipeline can grade. The NEXT grill round, not this call, decides
       whether `open_decisions` resolves; that keeps grading where grading lives.

    Returns a `fidelity_proposal` when the item is still `high` — a proposal, never a
    write. The flip is the author's confirmation to make, because a wrong automatic
    downgrade deletes AL-68's signal silently.
    """
    if dimension not in prds_svc.DIMENSIONS:
        raise ValueError(
            f"unknown grill dimension: {dimension!r} (expected {sorted(prds_svc.DIMENSIONS)})"
        )
    verdict = (verdict or "").strip()
    if not verdict:
        raise ValueError("verdict text is required — the screenshot alone is not an answer")
    att = att_svc.get_attachment(db, attachment_id)
    if att is None:
        raise ValueError(f"unknown attachment: {attachment_id!r} "
                         "(upload the screenshot first via /api/public/attachments)")
    url = f"/api/public/attachments/{att.id}"

    items_svc.update_item(
        db, item.id,
        evidence=[{"kind": "screenshot", "detail": f"prototype verdict: {verdict}", "url": url}],
    )
    turn = prds_svc.append_grill_turn(
        db, prd.id, role="user", via="prototype",
        text=f"Prototype verdict on {item.key} ({dimension}): {verdict} — artifact: {url}",
    )
    db.refresh(item)
    proposal = None
    if item.fidelity == "high":
        proposal = {
            "item": item.key, "from": "high", "to": "low", "confirmed": False,
            "how": "PATCH /api/items/{item.key} {\"fidelity\": \"low\"} once the "
                   "verdict genuinely settles the question in words",
        }
    return {
        "prd": prd.key, "item": item.key, "dimension": dimension,
        "turn_seq": turn.seq, "artifact_url": url,
        "fidelity": item.fidelity, "fidelity_proposal": proposal,
    }
