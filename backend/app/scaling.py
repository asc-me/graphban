"""A deferred finding that can fire (GRPH-540).

GRPH-55 records real scaling debt — `get_code_map` returns every node and edge,
`related_work` and `code_neighbors` full-scan into Python, `search_items` materialises every
project item per query, `top_k` is uncapped — and defers it against a precise, measurable
condition:

> **Trigger: first project > ~5k items.** Until then, leave it — recorded so it isn't
> rediscovered.

That is the right call and the trigger is well chosen, because it is checkable. **Nothing
checked it.** The item sat in `blocked` and the condition fired only if somebody re-measured
on a hunch — which is the same weakness as the prose it was meant to improve on. A condition
nobody evaluates is a condition that is always false.

Measured 2026-08-27 across every project: the largest held 420 items, 8.4% of the trigger. So
the deferral is sound today. What was missing is what happens on the day it stops being sound.

**One number, in one place.** The threshold below is the only copy. Two — one in the item's
text, one in code — drift, and the drift is invisible because neither side fails.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger("graphban.scaling")

#: The trigger GRPH-55 named. Quoted in the warning below so a reader sees one number rather
#: than trusting that two agree.
SCALING_TRIGGER_ITEMS = 5_000

#: The item whose analysis this reconnects the measurement to. A warning that reports a count
#: and nothing else tells an operator "project X has 5,102 items", which is not an instruction
#: — the whole point is to point back at the work that was already reasoned about and parked.
SCALING_TRIGGER_ITEM = "GRPH-55"


def check_scaling_triggers(db: Session, *, threshold: int = SCALING_TRIGGER_ITEMS) -> list[tuple[str, int]]:
    """Warn once per boot if any project has crossed GRPH-55's trigger. Returns what crossed.

    Boot-time rather than per-request, and that is proportionate: 420 → 5,000 is not a
    threshold anything crosses between deploys, and a per-request check would pay a COUNT on
    every call to answer a question whose answer changes over months.

    Returns the crossers so a caller — or a test — can assert on the decision rather than on
    the log text. `threshold` is injectable for the same reason: the test database holds a
    handful of rows, so a check exercised only against it can never fire, and a test asserting
    "no warning was emitted" would pass for entirely the wrong reason.
    """
    from app.models import Item

    rows = db.execute(
        select(Item.project_id, func.count(Item.id)).group_by(Item.project_id)
    ).all()
    crossed = sorted(((pid, n) for pid, n in rows if n > threshold), key=lambda r: -r[1])
    if not crossed:
        # Silent below the line, deliberately. A warning that always fires is noise, and noise
        # is how the real ones get scrolled past.
        return []
    worst = ", ".join(f"{pid} ({n:,} items)" for pid, n in crossed[:5])
    logger.warning(
        "%s has crossed the %s-item trigger recorded on %s: %s. That item defers real "
        "scaling work — get_code_map returns every node and edge, related_work and "
        "code_neighbors full-scan in Python, search_items materialises every project item "
        "per query, and top_k is uncapped. The deferral was sound below this line; it is "
        "not any more.",
        "A project" if len(crossed) == 1 else f"{len(crossed)} projects",
        f"{threshold:,}", SCALING_TRIGGER_ITEM, worst,
    )
    return crossed
