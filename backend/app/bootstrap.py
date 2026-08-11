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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.security.apikey import generate_api_key
from app.security.passwords import hash_password
from app.services import projects as projects_svc


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


def provision(
    db: Session,
    *,
    project_name: str,
    email: str = DEFAULT_EMAIL,
    name: str = "Operator",
    password: str | None = None,
) -> dict:
    """Create the operator, their project, and one read+write key. Idempotent by
    refusal: on an instance that already has users it changes nothing and says so.

    The generated password is returned ONCE and only here. The API key likewise — keys
    are stored as a hash and cannot be recovered, so a caller that loses this dict has
    to mint a new one, which is a deliberate property rather than an oversight.
    """
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
    _, api_key = generate_api_key(db, user.id, "first-run", ["read", "write"], project.id, None)

    return {
        "provisioned": True,
        "email": email,
        "password": password,
        "project_id": project.id,
        "project_name": project.name,
        "project_tag": project.tag,
        "memory_write_mode": project.memory_write_mode,
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
        "next": "sign in, then Settings → API Keys to mint a sync credential",
    }
