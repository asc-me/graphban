"""Per-user UI preference documents (PRD-60 D1 / GRPH-1133).

Sidebar collapse, condense, tracker layout, board columns and saved views are the user's, not
the deployment's and not the project's — so the document is keyed by `user_id` alone and there
is exactly one per user.

**The stored document is returned as written.** Nothing here fills in defaults, because a
default the server invents is indistinguishable from a choice the user made, and the client
already owns the defaults. What the server owns is the VOCABULARY, which is why the constants
below live here and `app/schemas/__init__.py` references them rather than restating them — the
same rule AGENTS.md states for `items.STATUSES`.

**Absent is not empty.** `read` answers `saved: False` when the user has never written, and
`saved: True` with `{}` when they wrote an empty document. The two look identical to a UI that
renders either one as "the defaults", and the difference is the whole migration contract: a
client with values in localStorage uploads them once *only* when the server has nothing, so a
second device must not read "no preferences" as "upload mine" and flatten the first device's
layout.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import UserPreference, utcnow
from app.services import items as items_svc
from app.services import prds as prds_svc

#: Tracker presentation: board first, list second (PRD-60 G1).
LAYOUTS = ("board", "list")
#: What a board column can be doing. `hidden` changes the display only — view counts still
#: include its items (PRD-60 A2), so this is a preference and never a filter on the data.
COLUMN_STATES = ("shown", "collapsed", "hidden")
#: The scope popover's WINDOW (GRPH-1146).
WINDOWS = ("30d", "90d", "180d", "all")
#: The scope popover's AUTO-HIDE DONE/CLOSED AFTER (GRPH-1146).
AUTO_HIDE = ("never", "3d", "7d", "14d", "30d")
#: Status filter chips: All, Active, then each real status.
TRACKER_STATUSES = ("all", "active", *items_svc.STATUSES)
PRD_STATUSES = ("all", *prds_svc.STATUSES)
#: The pages that have a saved-view set, i.e. the legal `views.{page}` keys.
VIEW_PAGES = ("tracker", "prds")

#: A saved-view id or a `default` pointer is client-minted ("My view N" → an id of the
#: client's choosing), so it is bounded rather than enumerated. Enumerating it would reject
#: every view the moment GRPH-1146 picks an id scheme this file had not guessed.
MAX_ID_LENGTH = 64
MAX_LABEL_LENGTH = 120


def read(db: Session, user_id: str) -> dict:
    """`{saved, preferences}` for one user. Never another user's row — the key IS the caller."""
    row = db.get(UserPreference, user_id)
    if row is None:
        return {"saved": False, "preferences": {}}
    return {"saved": True, "preferences": row.doc or {}}


def write(db: Session, user_id: str, doc: dict) -> dict:
    """Replace the caller's whole document and read it back.

    Replace, not merge: the PUT is last-writer-wins across devices, and a merge would make the
    outcome depend on what the previous device happened to leave behind — a key deleted in one
    browser would resurrect itself from another. The caller sends the document it has.

    The response is the row read back from the database rather than the argument echoed, so a
    value the JSON column cannot carry shows up here instead of on the next GET.
    """
    row = db.get(UserPreference, user_id)
    if row is None:
        row = UserPreference(user_id=user_id, doc=doc, updated_at=utcnow())
        db.add(row)
    else:
        row.doc = doc
        row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return {"saved": True, "preferences": row.doc or {}}
