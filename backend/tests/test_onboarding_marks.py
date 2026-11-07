"""GRPH-1361: the quick-start dialog's progress marks on `users.onboarding`.

The dialog opens for a user whose onboarding is unset (D6) and its top-bar chip clears on
Finish (D8). Both need the document readable before any project exists and writable by the
user, which is what these endpoints are. They are UI progress only: no checklist step reads
them (that rule is GRPH-1360's, asserted there)."""


def _onboarding(client, auth):
    r = client.get("/api/auth/me/onboarding", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["onboarding"]


def test_a_new_user_reads_null_with_no_project(client, auth):
    # The fixture user has no backfill: a new user, the dialog's trigger.
    assert _onboarding(client, auth) is None


def test_finish_stamps_quickstart_finished_and_reads_back(client, auth):
    r = client.patch("/api/auth/me/onboarding", json={"mark": "quickstart_finished"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["onboarding"]["quickstart_finished_at"]
    assert _onboarding(client, auth)["quickstart_finished_at"]


def test_marks_merge_rather_than_replace(client, auth):
    client.patch("/api/auth/me/onboarding", json={"mark": "quickstart_dismissed"}, headers=auth)
    client.patch("/api/auth/me/onboarding", json={"mark": "quickstart_finished"}, headers=auth)
    doc = _onboarding(client, auth)
    assert doc["quickstart_dismissed_at"] and doc["quickstart_finished_at"]


def test_unknown_marks_and_fields_are_refused(client, auth):
    for body in ({"mark": "tour_completed"}, {"mark": "quickstart_finished", "extra": 1}, {}):
        r = client.patch("/api/auth/me/onboarding", json=body, headers=auth)
        assert r.status_code == 422, (body, r.text)
    assert _onboarding(client, auth) is None


def test_the_service_refuses_an_unknown_mark():
    """The boundary lives in the service, not only in the router's Literal."""
    from types import SimpleNamespace

    import pytest

    from app.services import setup as setup_svc

    user = SimpleNamespace(onboarding=None)
    with pytest.raises(ValueError):
        setup_svc.mark_onboarding(None, user, "tour_completed")
    assert user.onboarding is None


def test_onboarding_needs_a_signed_in_user(client):
    assert client.get("/api/auth/me/onboarding").status_code == 401
    assert client.patch("/api/auth/me/onboarding", json={"mark": "quickstart_finished"}).status_code == 401
