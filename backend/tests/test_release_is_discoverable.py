"""The refusal names the verb that fixes it (GRPH-809).

Reported as "the release path is undiscoverable — it works, but both refusals point away from
it, and `update_item` silently leaves `claimed_by` set".

The trap is specific and worth naming in the message rather than in a doc nobody reads at the
moment of refusal: `update_item` LOOKS like the way out. It moves the status and leaves the
claim exactly where it was, so an agent that moved its item on believes it let go — and is
refused again, with the same sentence, having done the thing the sentence seemed to ask for.
"""
import pytest

from app.services import items as items_svc


@pytest.fixture()
def session(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _claim(session, agent_id="CORE-A1"):
    return items_svc.claim_next(session, agent_id=agent_id, project_id="core")


def test_the_refusal_names_release_item(client, auth, session):
    _claim(session)

    with pytest.raises(items_svc.AlreadyHolding) as exc:
        _claim(session)

    assert "release_item" in str(exc.value), "told the reader to release without naming the verb"


def test_the_refusal_says_update_item_is_not_it(client, auth, session):
    """The specific wrong turn. Without this the reader tries the tool that looks right,
    watches the status change, and is refused again by the same sentence."""
    _claim(session)

    with pytest.raises(items_svc.AlreadyHolding) as exc:
        _claim(session)

    said = str(exc.value)
    assert "update_item" in said and "does NOT release" in said


def test_update_item_really_does_leave_the_claim(client, auth, session):
    """The behaviour the message warns about, pinned. If this ever changes, the warning
    becomes a lie and this test says so."""
    from app.models import Item

    held = _claim(session)
    assert held is not None
    items_svc.update_item(session, held.id, {"status": "review"})
    session.commit()

    assert session.get(Item, held.id).claimed_by is not None
