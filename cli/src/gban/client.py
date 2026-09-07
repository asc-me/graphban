"""The one place `gban` talks to a Graphban server (PRD-40 D3, D11).

Every verb is one endpoint, called once. The only call that is not a verb is the token
exchange, and it is transport rather than policy: it carries no rule, decides nothing, and
happens before any verb runs.

**The session model, and why it needs no machinery.** `POST /api/auth/refresh` issues a new
pair but does not consume the token presented — validity is keyed on `user.token_version`,
which moves only on logout or a password change (AL-59). So an invocation exchanges once, holds
the access token in memory, makes its call, and exits; two `gban` processes at once cannot
disturb each other; and `session.json` is written only by `gban login`, never per call. Writing
it every time would manufacture the race that does not otherwise exist.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from gban import config

#: Exit codes. Chosen so they cannot collide with a passed-through `gbfleet` code, which
#: carries meaning of its own (75 stuck, 69 unreachable endpoint, 55 budget) and is returned
#: unchanged by `gban fleet` (D5).
EXIT_REFUSED = 1
EXIT_UNREACHABLE = 2
EXIT_NO_SESSION = 3
EXIT_NO_SUPERVISOR = 4

TIMEOUT = 30.0


class Unreachable(Exception):
    """No HTTP response at all: the server, the network, or the URL."""


class NoSession(Exception):
    """No stored session, or one the server will not renew.

    Expired and revoked are deliberately the SAME state here. Both come back 401, and the
    person does the same thing either way — a distinction would be precision nobody can act on.

    "You have a credential, but not one that can do this" IS a different state, and the only
    one where the next step is not simply `gban login` (criterion 5). Somebody who exported
    `GRAPHBAN_API_KEY` and watched `gban fleet` work has every reason to read "session expired"
    as a bug in the tool rather than as a statement about what a key is for.
    """

    def __init__(self, why: str, *, act: str = "", have_key: bool = False) -> None:
        super().__init__(why)
        self.why = why
        self.act = act
        self.have_key = have_key

    def advice(self, prog: str) -> str:
        if self.have_key and self.act:
            return (f"`{prog} {self.act}` needs a session, not an API key. It acts as YOU — "
                    f"the ledger records which human did it — and ${config.API_KEY_ENV} names "
                    f"an agent.\n     Run `{prog} login`.")
        return f"session expired, run `{prog} login`"


class Refused(Exception):
    """The server said no, in its own words (D8).

    Carries the server's `detail` and `hint` unedited. Re-wording either would put a second
    copy of a rule in the client, and the server's phrasing is the one under test.
    """

    def __init__(self, status: int, detail: str, hint: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.hint = hint


class Client:
    """A server, and the credential this invocation is using."""

    def __init__(self, url: str, *, token: str = "", api_key: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.api_key = api_key

    # ---- transport ---------------------------------------------------------------------
    def _open(self, method: str, path: str, body: dict | None, headers: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode() or "{}"
                return response.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() or "{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"detail": raw[:400]}
            return exc.code, payload if isinstance(payload, dict) else {"detail": str(payload)}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # No HTTP response at all. Structurally different from a refusal, and reported as
            # such — the client never reads a message to decide which failure it is in (D8).
            raise Unreachable(f"could not reach {self.url}: {exc}") from exc

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        status, payload = self._open(method, path, body, headers)
        if status >= 400:
            detail = payload.get("detail") or payload.get("message") or f"HTTP {status}"
            if isinstance(detail, dict):
                detail, hint = (detail.get("message") or str(detail)), detail.get("hint", "")
            else:
                hint = payload.get("hint", "")
            raise Refused(status, str(detail), str(hint))
        return payload


def login(url: str, email: str, password: str) -> dict:
    """Exchange credentials for a token pair. The only call that sends a password."""
    return Client(url).call("POST", "/api/auth/login",
                            {"email": email, "password": password})


def authenticated(url: str, *, act: str = "") -> Client:
    """A client for this invocation: one refresh exchange, access token held in memory.

    Raises `NoSession` when there is nothing stored or the server will not renew it. The
    caller turns that into one instruction and exit 3 — never a traceback, and never a bare
    401 (criterion 4). `act` is the verb the person typed; it appears only in the message for
    somebody holding an API key and nothing else.
    """
    stored = config.session().get("refresh_token")
    if not stored:
        # `act` is what the person typed, so the message can name it rather than describe a
        # category they would then have to work out they are in.
        raise NoSession("no stored session", act=act,
                        have_key=bool(os.environ.get(config.API_KEY_ENV)))
    try:
        pair = Client(url).call("POST", "/api/auth/refresh", {"refresh_token": stored})
    except Refused as exc:
        raise NoSession(str(exc)) from exc
    token = pair.get("access_token") or ""
    if not token:
        raise NoSession("the server returned no access token")
    # The new refresh token is NOT written back. The presented one stays valid (the server
    # keys on `token_version`), so rewriting the file on every call would buy nothing and
    # create a race between concurrent invocations.
    return Client(url, token=token)
