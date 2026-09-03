"""First-run provisioning (AL-283 / PRD-14 D3).

PRD-14 splits the product's stops into QUALITY gates, which an agent may hold, and
AUTHORITY gates, which stay human. Issuing the first credential is the purest authority
gate there is — and it was also the one thing blocking a zero-browser install, because
signup and key minting are both JWT/UI-only and an agent cannot bootstrap itself into
existing.

The resolution is not to relax the gate. It is to satisfy it OUT OF BAND: an operator
runs a script on the box they already control, and that script mints the first
credential once. Authority never moves to the agent; it just stops requiring a browser.

Everything here is deliberately narrow. It runs once, on an instance with no users, and
refuses in any configuration where "no users yet" is not proof that the person running
it owns the deployment.
"""
from __future__ import annotations

import secrets
import uuid

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.security.apikey import generate_api_key
from app.security.passwords import hash_password
from app.services import projects as projects_svc
from app.services import tool_tiers


# Must survive Pydantic's EmailStr on the LOGIN endpoint, which is a stricter check than
# the `users.email` column (a plain String). `operator@localhost` provisioned happily and
# then could not sign in — the exact "account nobody can log into" dead end this script
# set out to avoid, arriving one layer down. Found by the AL-286 acceptance walk.
DEFAULT_EMAIL = "operator@example.com"


class BootstrapRefused(Exception):
    """This instance must not be bootstrapped. The message says why and what to do."""


def is_virgin(db: Session) -> bool:
    """No users yet — the same signal `seed()` uses (seed.py), not a separate flag.

    Zero users is the only honest definition of "first run": it cannot drift from
    reality the way a marker file or an env var can, and it makes re-running the script
    a no-op rather than a second, conflicting operator account.
    """
    return db.scalar(select(User).limit(1)) is None


def check_allowed(db: Session) -> None:
    """Refuse where minting a credential without authentication would be a hole.

    Both refusals are about the same thing: "no users yet" only implies "the person at
    the terminal owns this deployment" on a single-tenant box being set up for the first
    time.
    """
    if settings.hosted_mode:
        raise BootstrapRefused(
            "refusing to bootstrap a HOSTED instance: this mints a credential without "
            "authenticating anyone, which on a multi-tenant deployment is a hole, not a "
            "convenience. Use `graphban admin bootstrap-hosted` instead — it creates the "
            "first operator and their org WITHOUT minting a key."
        )
    if settings.seed_on_start:
        # Both key off zero users and seeding runs during lifespan startup, so seeding
        # would win the race and the operator would silently get the prototype dataset
        # instead of their own project. Demo data and a real first project are different
        # intents; refuse rather than pick one.
        raise BootstrapRefused(
            "refusing to bootstrap with SEED_ON_START=true: the demo dataset creates "
            "users at startup, so this would either race it or land beside it. Unset "
            "SEED_ON_START for a real first project, or keep the demo data and use it."
        )


#: The SAME validator `LoginIn.email` uses, deliberately — not a regex that agrees with it
#: today. `init` provisioning an operator the login route will refuse is the whole of
#: GRPH-461, and any second implementation of "is this an email" is that bug waiting to
#: come back. A `TypeAdapter` over `EmailStr` runs exactly what the schema runs.
_EMAIL = TypeAdapter(EmailStr)


def check_email(email: str) -> str:
    """The address, or refuse. Called BEFORE anything is written.

    `.invalid` and `.test` are RFC 2606 reserved and email-validator rejects them, which is
    correct and is not loosened here — a walk provisioned with `walk@example.invalid` got an
    operator it could not sign in as. The fix is `init` agreeing with login, not login
    agreeing with `init`.
    """
    try:
        return _EMAIL.validate_python(email)
    except ValidationError as e:
        reason = e.errors()[0].get("msg", "not a valid email address")
        raise BootstrapRefused(
            f"{email!r} is not an address this instance can sign in with — {reason}. "
            "Nothing was provisioned; re-run with a real address."
        ) from None


KEY_SCOPES = ("project", "global")


def provision(
    db: Session,
    *,
    project_name: str,
    email: str = DEFAULT_EMAIL,
    name: str = "Operator",
    password: str | None = None,
    key_scope: str = "project",
    key_tiers: list[str] | None = None,
) -> dict:
    """Create the operator, their project, and one read+write key. Idempotent by
    refusal: on an instance that already has users it changes nothing and says so.

    `key_scope` decides what the provisioned MCP key may write to:
    "project" (the default) binds the key to the new project, so the agent's writes
    target it without naming it; "global" mints an unbound key (`ApiKey.project_id`
    NULL) that must pass `project_id` per call, or falls back to the default project.
    Either way the project is still created — a global key is about the CREDENTIAL,
    not about skipping the instance's first project.

    `key_tiers` names optional tool tiers (see `services.tool_tiers.TIERS`) that the
    key's manifest should ADVERTISE — e.g. ["prd"] to hand a first-run agent the
    spec-authoring verbs. Omitted/empty is core-only, the same default as every other
    mint. It is visibility, not authorisation: `scopes` and `roles` still decide what
    may be called.

    The generated password is returned ONCE and only here. The API key likewise — keys
    are stored as a hash and cannot be recovered, so a caller that loses this dict has
    to mint a new one, which is a deliberate property rather than an oversight.
    """
    # FIRST, before the virgin check and before any write. A half-provisioned instance with
    # an operator nobody can sign in as is worse than a refused one: the second `init` then
    # answers "this instance already has users; nothing was changed" and the human is stuck
    # with a password for an account that cannot accept it (GRPH-461). An invalid scope is
    # the same failure class, so it is checked in the same place.
    if key_scope not in KEY_SCOPES:
        raise BootstrapRefused(
            f"key_scope must be one of {KEY_SCOPES}, got {key_scope!r}; nothing was provisioned"
        )
    key_tiers = [t.strip() for t in key_tiers if t.strip()] if key_tiers else []
    unknown = [t for t in key_tiers if t not in tool_tiers.TIERS]
    if unknown:
        raise BootstrapRefused(
            f"unknown key tier(s) {', '.join(repr(t) for t in unknown)} — the tiers are "
            f"{', '.join(tool_tiers.TIERS)}; nothing was provisioned"
        )
    email = check_email(email)
    check_allowed(db)
    if not is_virgin(db):
        return {"provisioned": False,
                "reason": "this instance already has users; nothing was changed"}

    password = password or secrets.token_urlsafe(18)
    user = User(
        id="u_" + uuid.uuid4().hex[:10],
        name=name,
        handle=(email.split("@", 1)[0] or "operator")[:32],
        email=email,
        initials="".join(w[0] for w in name.split()[:2]).upper() or "OP",
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()

    project = projects_svc.create_project(db, name=project_name, owner_user_id=user.id)
    # Trusted memory, deliberately, and ONLY for a project created by this script.
    #
    # The AL-286 acceptance walk caught D1 and D3 not composing: each is right alone, but
    # a bootstrapped project defaulting to `review` cannot read back its own writes, so
    # the zero-browser install this script exists to deliver stops at the first memory.
    # Running it is an explicit request for an agent-driven instance, the project is brand
    # new so there is no existing corpus to poison, and every trusted publish is labelled
    # and undoable in Memory review. Existing projects are untouched — migration 0040
    # still defaults them to `review`.
    project.memory_write_mode = "trusted"
    db.commit()
    # Global mints with project_id NULL — the same shape the UI's key form produces,
    # so authz treats the two paths identically. Empty tiers pass through as None so a
    # CLI-minted core key is indistinguishable from any other core key in the row.
    key_project_id = None if key_scope == "global" else project.id
    _, api_key = generate_api_key(
        db, user.id, "first-run", ["read", "write"], key_project_id, None,
        tool_tiers=list(key_tiers) or None,
    )

    return {
        "provisioned": True,
        "email": email,
        "password": password,
        "project_id": project.id,
        "project_name": project.name,
        "project_tag": project.tag,
        "memory_write_mode": project.memory_write_mode,
        "key_scope": key_scope,
        "key_tiers": key_tiers,
        "api_key": api_key,
    }


def check_hosted_allowed(db: Session) -> None:
    """Refuse where creating a first operator would not be self-evidently authorised.

    The mirror of `check_allowed`, and the reasoning is the same in the other direction:
    zero users is the only honest definition of "first run", and on a hosted instance it is
    what makes "the person running this owns the deployment" true — running it at all
    requires shell access to the deployed service.
    """
    if not settings.hosted_mode:
        raise BootstrapRefused(
            "this instance is not hosted; use `graphban init`, which also creates a "
            "project and a first API key"
        )
    if settings.seed_on_start:
        # Same race as the self-host path: seeding creates users during lifespan startup,
        # so this would either lose the race or land beside the demo dataset.
        raise BootstrapRefused(
            "refusing to bootstrap with SEED_ON_START=true: the demo dataset creates "
            "users at startup, so this would either race it or land beside it"
        )


def provision_hosted(
    db: Session,
    *,
    email: str,
    org_name: str,
    name: str = "Operator",
    password: str | None = None,
) -> dict:
    """Create the first operator and their organization on a HOSTED instance (GRPH-219).

    **This exists because the hosted deployment had no way to reach its own first user.**
    `signup_mode=invite_only` refuses registration without a token (`auth.py`), issuing a
    platform invite requires a platform-admin JWT, and a JWT requires an account. The
    refusal in `check_allowed` advised "invite the first operator instead", which is not a
    thing anyone can do on a virgin instance. Verified live on 2026-08-11: the production
    instance had zero users, zero orgs and zero invites, and no supported route to a first
    login.

    **Deliberately mints NO API key and creates NO project.** That is the whole difference
    from `provision`, and it is the reason this is safe where that one is not: an
    unauthenticated key mint on a multi-tenant deployment is a hole. Everything past the
    first login — projects, credentials, invites — goes through the product, which is also
    what makes the onboarding path worth trusting once it has been walked.

    Idempotent by refusal, like `provision`: on an instance that already has users it
    changes nothing and says so, so a second run cannot create a rival operator.
    """
    from app.services import orgs as orgs_svc

    check_hosted_allowed(db)
    if not is_virgin(db):
        return {"provisioned": False,
                "reason": "this instance already has users; nothing was changed"}

    email = email.strip().lower()
    if not email:
        raise BootstrapRefused("an operator email is required")
    # Format before allowlist, and for the same reason `provision` checks it first: the
    # allowlist would catch most malformed addresses by accident, but an operator who put
    # the same typo in `PLATFORM_ADMIN_EMAILS` passes it and lands on an account the login
    # route refuses (GRPH-461). Format is the more actionable message either way.
    email = check_email(email)
    if email not in settings.platform_admin_email_set:
        # Not a security control — the allowlist is the control. This catches the mistake
        # that leaves you exactly where you started: an account that exists, can sign in,
        # and still cannot open the operator console to issue the first invite.
        raise BootstrapRefused(
            f"{email} is not in PLATFORM_ADMIN_EMAILS "
            f"({', '.join(sorted(settings.platform_admin_email_set)) or 'empty'}), so this "
            "account could sign in but never reach the operator console. Add it to that "
            "variable first — and on Railway, apply the change: staged edits are absent "
            "from the running service while appearing saved."
        )

    password = password or secrets.token_urlsafe(18)
    user = User(
        id="u_" + uuid.uuid4().hex[:10],
        name=name,
        handle=(email.split("@", 1)[0] or "operator")[:32],
        email=email,
        initials="".join(w[0] for w in name.split()[:2]).upper() or "OP",
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    org = orgs_svc.create_org(db, user, org_name)
    return {
        "provisioned": True,
        "email": email,
        "password": password,
        "org_id": org.id,
        "org_name": org.name,
        "next": "sign in, then Settings → API keys to mint a link key",
    }
