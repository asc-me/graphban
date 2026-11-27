"""Messages on a PRD link's thread (GRPH-P104 S1): the kinds, their validation and rendering."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import Item, Prd, PrdLink, PrdLinkMessage
from app.services import keys

# `suggested_item` is filed by the receiving side (S2); S1 stores it like any other kind.
KINDS = ("note", "question", "correction", "suggested_item")
MAX_BODY = 8000


LINK_DIRECTIONS = ("depends_on", "blocks")
_SUGGESTION_FIELDS = ("kind", "title", "description", "touchpoints", "tags", "status", "link")


def parse_suggestion(db: Session, body: str, *, sender_prd_id: str) -> dict:
    """A `suggested_item` body is JSON `{kind, title, description, touchpoints, tags, status,
    link}` for the other side's PRD (GRPH-P104 S2). Only the shape is checked here; the
    receiving tool's own validation runs when the other side files it. `link` is the one
    optional dependency link `{item, direction}` to an item on the SENDER's PRD; its item is
    frozen to an id. Raises ValueError."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError("a suggested_item body is a JSON object with a title") from None
    if not isinstance(data, dict):
        raise ValueError("a suggested_item body is a JSON object with a title")
    extra = sorted(set(data) - set(_SUGGESTION_FIELDS))
    if extra:
        raise ValueError(f"unknown suggested_item field(s) {extra}; fields are "
                         f"{', '.join(_SUGGESTION_FIELDS)}")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("a suggested_item needs a title")
    for f in ("touchpoints", "tags"):
        v = data.get(f, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"suggested_item {f} is a list of strings")
    for f in ("kind", "description", "status"):
        if f in data and not isinstance(data[f], str):
            raise ValueError(f"suggested_item {f} is a string")
    lk = data.get("link")
    if lk is not None:
        if not isinstance(lk, dict) or set(lk) != {"item", "direction"}:
            raise ValueError("suggested_item link is {item, direction}")
        if lk["direction"] not in LINK_DIRECTIONS:
            raise ValueError(f"link direction is one of {', '.join(LINK_DIRECTIONS)}")
        item = db.get(Item, keys.resolve_item(db, lk["item"]) or lk["item"])
        if item is None or item.prd_id != sender_prd_id:
            raise ValueError(f"cannot link {lk['item']!r}: the link is to an item on your own PRD")
        data["link"] = {"item": item.id, "direction": lk["direction"]}
    return data


def validate(db: Session, link: PrdLink, *, kind: str, body: str, cites: list[str] | None,
             reply_to: int | None, sender_prd_id: str = "") -> tuple[str, list[str], PrdLinkMessage | None]:
    """Returns (body, frozen cited item ids, the replied-to message). Raises ValueError."""
    if kind not in KINDS:
        raise ValueError(f"invalid message kind {kind!r}; kinds are {', '.join(KINDS)}")
    body = (body or "").strip()
    if not body:
        raise ValueError("a message needs a body")
    if len(body) > MAX_BODY:
        raise ValueError(f"a message body is at most {MAX_BODY} characters")
    ids: list[str] = []
    if kind == "suggested_item":
        data = parse_suggestion(db, body, sender_prd_id=sender_prd_id)
        body = json.dumps(data, sort_keys=True)
        if data.get("link"):
            cites = [*(cites or []), data["link"]["item"]]
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
            **({"suggestion": json.loads(m.body), "outcome": render_outcome(m)}
               if m.kind == "suggested_item" else {}),
            "created_at": m.created_at.isoformat() if m.created_at else None}


def render_outcome(m: PrdLinkMessage) -> dict | None:
    """The one decision on a suggestion, or None while it is undecided."""
    if not m.outcome:
        return None
    return {"outcome": m.outcome, "code": m.outcome_code, "message": m.outcome_message,
            "item": m.outcome_item_id or None, "decided_by": m.decided_by,
            "decided_at": m.decided_at.isoformat() if m.decided_at else None}
