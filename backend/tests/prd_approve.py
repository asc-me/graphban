"""Earn `approved` the way the product does — finish the grill, never set the field."""

from app.services import prds as prd_svc


def approve(db, prd):
    """Record answers on every dimension and derive status. Commits."""
    window = prd_svc.grill_window(db, prd.id)
    prior = prd_svc.grill_history(db, prd.id, since=window)
    prd_svc.record_grill_turns(db, prd.id, prior + [
        {"role": "user", "text": f"Answer, round {len(prd_svc.baseline_chain(db, prd.id))}."}])
    for name in prd_svc.DIMENSIONS:
        prd_svc.set_dimension(db, prd.id, name, "resolved")
    return prd_svc.sync_status(db, prd)


def approve_id(prd_id: str):
    """HTTP-test helper: open a session, approve, close. The next request sees `approved`."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        prd = prd_svc.get_prd(db, prd_id)
        if prd is None:
            raise AssertionError(f"prd not found: {prd_id}")
        return approve(db, prd)
    finally:
        db.close()
