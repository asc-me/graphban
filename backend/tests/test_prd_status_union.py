"""The frontend's PRD status union must contain everything the backend can send (GRPH-458).

A PRD status lives twice — `STATUSES` in `services/prds.py` and the `PrdStatus` union in
`web/src/lib/types.ts` — and nothing compared them. `closed` was added to the backend and the
union never learned it, so `PRD_STATUS_META[p.status]` returned undefined, `meta.color` threw,
and the PRD list and editor rendered a white screen from the moment the first PRD was closed
(GRPH-P18, 2026-08-20 21:43 UTC). Every other view kept working, because nothing else in the
app renders a PRD status.

TYPESCRIPT COULD NOT HAVE CAUGHT THIS, which is why the check lives here rather than in the
frontend. `Record<PrdStatus, …>` is exhaustive over `PrdStatus`. The map was complete with
respect to a union that was itself wrong, so the index type-checked and the compiler had
nothing to say. Every test in `web/` is in the same position: it can assert the frontend
agrees with itself, and cannot know what the server is able to send. Only a test that reads
both sides can, and only this side of the stack has the authoritative list.

WHAT THIS CANNOT CATCH, stated because a silent gap is the thing being fixed: it compares
the DECLARED sets. A backend that writes a status not in `STATUSES` — by hand, by migration,
or by a route that skips the service — is invisible here, and the frontend's runtime fallback
is what covers that case. The two are complements, not duplicates.
"""
import pathlib
import re

import pytest

from app.services.prds import STATUSES

TYPES_TS = (pathlib.Path(__file__).resolve().parent.parent.parent
            / "web" / "src" / "lib" / "types.ts")

UNION = re.compile(r"export type PrdStatus\s*=\s*([^;]+);", re.S)


def _declared_in_typescript() -> set[str]:
    m = UNION.search(TYPES_TS.read_text(encoding="utf-8"))
    assert m, f"no `export type PrdStatus` in {TYPES_TS} — this test is reading nothing"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_the_frontend_union_covers_every_status_the_backend_sends():
    """The one that would have prevented the outage."""
    missing = set(STATUSES) - _declared_in_typescript()

    assert not missing, (
        f"the backend can send {sorted(missing)}, which `PrdStatus` in web/src/lib/types.ts "
        "does not declare. `Record<PrdStatus, …>` will be exhaustive over the wrong union, "
        "so nothing in web/ will fail — the PRD page will just render a white screen. Add it "
        "to the union AND to PRD_STATUS_META."
    )


def test_the_union_does_not_claim_statuses_the_backend_cannot_send():
    """The reverse. A union that has drifted the other way means dead branches in the UI and
    a status somebody thinks is reachable."""
    invented = _declared_in_typescript() - set(STATUSES)

    assert not invented, (
        f"web/src/lib/types.ts declares {sorted(invented)}, which the backend never sends."
    )


@pytest.mark.parametrize("status", STATUSES)
def test_every_backend_status_is_rendered_by_the_meta_map(status):
    """The union is not enough on its own: `closed` could be added to the type while the map
    stays at three entries, and TypeScript WOULD catch that — but only if somebody compiles.
    Asserted here too so the failure arrives in the same run as everything else."""
    meta = (TYPES_TS.parent.parent / "features" / "prds" / "meta.ts").read_text(encoding="utf-8")
    body = meta.split("PRD_STATUS_META", 1)[1]

    assert re.search(rf"^\s+{re.escape(status)}:\s*{{", body, re.M), (
        f"PRD_STATUS_META has no entry for {status!r}, so the chip for it renders undefined"
    )


def test_the_status_list_is_not_empty():
    """The control. Every assertion above is a set difference, and two empty sets agree."""
    assert len(STATUSES) >= 3
    assert _declared_in_typescript(), "parsed no statuses out of types.ts"
