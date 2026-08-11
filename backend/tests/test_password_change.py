"""Changing your own password (GRPH-219 follow-up).

There was no way to do this. `password_hash` was written at account creation — `register`,
`seed`, `bootstrap` — and never again, in the API or the UI. Found the moment the first
hosted operator was provisioned and told to change the generated password we had just
printed for them: there was no button, and no endpoint behind the missing button.

The consequences were not cosmetic. An operator could never rotate a credential that had
been printed to a terminal and pasted into a chat log, and anyone who forgot their password
was locked out permanently, because there is no reset flow either.

The property worth defending here is the revocation: a password change must kill sessions
issued under the old one. Otherwise "I changed my password" does not mean what everybody
assumes it means, which is the only reason anyone changes a password in a hurry.
"""
import pytest


def _login(client, email="alex@ascme-labs.com", password="graphban"):
    return client.post("/api/auth/login", json={"email": email, "password": password})


@pytest.fixture()
def tokens(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_user_can_change_their_own_password(client, tokens):
    r = client.post("/api/auth/me/password",
                    json={"current_password": "graphban", "new_password": "a-new-password"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert r.status_code == 200, r.text
    assert _login(client, password="a-new-password").status_code == 200
    assert _login(client, password="graphban").status_code == 401, "the old one is dead"


def test_the_wrong_current_password_is_refused(client, tokens):
    """Holding a valid token is not enough. Otherwise a stolen access token — which expires
    in 30 minutes — could be turned into a permanent takeover in one call."""
    r = client.post("/api/auth/me/password",
                    json={"current_password": "not-it", "new_password": "a-new-password"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert r.status_code == 403
    assert _login(client, password="graphban").status_code == 200, "unchanged"


def test_it_revokes_every_session_issued_under_the_old_password(client, tokens):
    """THE point of the change, not a side effect. The reason to change a password is
    usually that it is no longer private, and a token minted before the change would keep
    whoever holds it signed in."""
    other = _login(client).json()  # a second device, signed in beforehand

    client.post("/api/auth/me/password",
                json={"current_password": "graphban", "new_password": "a-new-password"},
                headers={"Authorization": f"Bearer {tokens['access_token']}"})

    stale = client.get("/api/auth/me",
                       headers={"Authorization": f"Bearer {other['access_token']}"})
    assert stale.status_code == 401
    refreshed = client.post("/api/auth/refresh",
                            json={"refresh_token": other["refresh_token"]})
    assert refreshed.status_code == 401, "the stale refresh token must not mint a new pair"


def test_it_returns_a_working_token_pair_so_the_caller_stays_signed_in(client, tokens):
    """The caller's own session is revoked too — it has to be, it was issued under the old
    password. Handing back a fresh pair is what stops that being a logout."""
    r = client.post("/api/auth/me/password",
                    json={"current_password": "graphban", "new_password": "a-new-password"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})

    fresh = r.json()
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {fresh['access_token']}"}
                      ).status_code == 200
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": fresh["refresh_token"]}).status_code == 200


def test_a_short_password_is_refused(client, tokens):
    """Same floor as registration. A change path with a weaker bar than signup is a
    downgrade anyone can take once."""
    r = client.post("/api/auth/me/password",
                    json={"current_password": "graphban", "new_password": "short"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert r.status_code == 422
    assert _login(client, password="graphban").status_code == 200, "unchanged"


def test_an_anonymous_caller_cannot_change_anything(client):
    r = client.post("/api/auth/me/password",
                    json={"current_password": "graphban", "new_password": "a-new-password"})

    assert r.status_code == 401
    assert _login(client, password="graphban").status_code == 200, "unchanged"


def test_one_user_cannot_change_another_users_password(client, tokens):
    """The route reads the user from the TOKEN and takes no id, so there is no parameter to
    point at somebody else. Asserted anyway: the property is 'no such surface exists', and
    that is worth pinning against a future refactor that adds one for convenience."""
    victim = client.post("/api/auth/register", json={
        "name": "Victim", "email": "victim@example.com", "handle": "victim",
        "password": "victim-password",
    })
    assert victim.status_code == 201, victim.text

    client.post("/api/auth/me/password",
                json={"current_password": "graphban", "new_password": "a-new-password"},
                headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert client.post("/api/auth/login", json={
        "email": "victim@example.com", "password": "victim-password"}).status_code == 200
