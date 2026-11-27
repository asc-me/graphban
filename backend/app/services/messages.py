"""Messages on a PRD link's thread (GRPH-P104 S1): the kinds, their validation and rendering."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Item, Prd, PrdLink, PrdLinkMessage
from app.services import keys

# `suggested_item` is filed by the receiving side (S2); S1 stores it like any other kind.
KINDS = ("note", "question", "correction", "suggested_item")
MAX_BODY = 8000


def validate(db: Session, link: PrdLink, *, kind: str, body: str, cites: list[str] | None,
             reply_to: int | None) -> tuple[str, list[str], PrdLinkMessage | None]:
    """Returns (body, frozen cited item ids, the replied-to message). Raises ValueError."""
    if kind not in KINDS:
        raise ValueError(f"invalid message kind {kind!r}; kinds are {', '.join(KINDS)}")
    body = (body or "").strip()
    if not body:
        raise ValueError("a message needs a body")
    if len(body) > MAX_BODY:
        raise ValueError(f"a message body is at most {MAX_BODY} characters")
    ids: list[str] = []
    for ref in cites or []:
        item = db.get(Item, keys.resolve_item(db, ref) or ref)
        if item is None or item.prd_id not in (link.prd_a_id, link.prd_b_id):
            raise ValueError(f"cannot cite {ref!r}: a message cites items of the two linked PRDs")
        if item.id not in ids:
            ids.append(item.id)
    parent = None
    if reply_to is not None:
        parent = db.get(PrdLinkMessage, reply_to)
        if parent is None or parent.link_id != link.id:
            raise ValueError(f"message {reply_to} is not on this thread")
    return body, ids, parent


def render(db: Session, m: PrdLinkMessage) -> dict:
    """A message with its author as label + owner only, and cited items by key."""
    cited = [db.get(Item, i) for i in m.cites or []]
    return {"id": m.id, "kind": m.kind, "body": m.body,
            "author": {"kind": m.author_kind, "label": m.author_label, "owner": m.author_owner},
            "cites": [c.key if c else i for c, i in zip(cited, m.cites or [])],
            "reply_to": m.reply_to_id,
            "created_at": m.created_at.isoformat() if m.created_at else None}
