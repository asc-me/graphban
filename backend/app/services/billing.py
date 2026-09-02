"""Self-serve Stripe billing (GRPH-82).

Unset keys keep today's manual `set_org_plan` path. The webhook is the only
writer that Stripe drives; checkout and the portal only mint URLs.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Organization
from app.services import events as events_svc
from app.services.quotas import PLANS

logger = logging.getLogger("graphban.billing")

SELF_SERVE_PLANS = ("pro", "team")


class BillingUnavailable(Exception):
    """Stripe is not configured, or this org cannot use self-serve yet."""


def configured() -> bool:
    return bool(
        settings.stripe_api_key
        and settings.stripe_webhook_secret
        and settings.stripe_price_pro
        and settings.stripe_price_team
    )


def price_id_for(plan: str) -> str:
    if plan == "pro":
        return settings.stripe_price_pro
    if plan == "team":
        return settings.stripe_price_team
    raise ValueError(f"plan {plan!r} is not self-serve")


def plan_for_price(price_id: str) -> str | None:
    if not price_id:
        return None
    if price_id == settings.stripe_price_pro:
        return "pro"
    if price_id == settings.stripe_price_team:
        return "team"
    return None


def _sdk():
    try:
        import stripe
    except ImportError as e:
        raise BillingUnavailable("stripe is not installed (pip install 'graphban-api[billing]')") from e
    stripe.api_key = settings.stripe_api_key
    return stripe


def create_checkout_url(
    org: Organization, *, plan: str, success_url: str, cancel_url: str,
    customer_email: str,
) -> str:
    if not configured():
        raise BillingUnavailable("stripe is not configured")
    if plan not in SELF_SERVE_PLANS:
        raise ValueError(f"plan {plan!r} is not self-serve; expected one of {SELF_SERVE_PLANS}")
    stripe = _sdk()
    kwargs: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id_for(plan), "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": org.id,
        "metadata": {"org_id": org.id, "plan": plan},
        "subscription_data": {"metadata": {"org_id": org.id, "plan": plan}},
    }
    if org.stripe_customer_id:
        kwargs["customer"] = org.stripe_customer_id
    elif customer_email:
        kwargs["customer_email"] = customer_email
    session = stripe.checkout.Session.create(**kwargs)
    url = getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None)
    if not url:
        raise BillingUnavailable("stripe returned no checkout url")
    return str(url)


def create_portal_url(org: Organization, *, return_url: str) -> str:
    if not configured():
        raise BillingUnavailable("stripe is not configured")
    if not org.stripe_customer_id:
        raise BillingUnavailable("this org has no stripe customer yet")
    stripe = _sdk()
    session = stripe.billing_portal.Session.create(
        customer=org.stripe_customer_id, return_url=return_url,
    )
    url = getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None)
    if not url:
        raise BillingUnavailable("stripe returned no portal url")
    return str(url)


def construct_event(payload: bytes, sig_header: str) -> dict:
    """Verify the Stripe-Signature header. Raises ValueError on a bad signature."""
    if not configured():
        raise BillingUnavailable("stripe is not configured")
    stripe = _sdk()
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret,
        )
    except Exception as e:  # noqa: BLE001 — any verify failure is a 401, not a 500
        raise ValueError("webhook signature verification failed") from e
    if isinstance(event, dict):
        return event
    return {
        "type": event["type"],
        "data": {"object": dict(event["data"]["object"])},
    }


def apply_event(db: Session, event: dict) -> None:
    """Apply a verified Stripe event to Organization.plan. THE CALL the webhook makes."""
    etype = event.get("type") or ""
    obj = ((event.get("data") or {}).get("object")) or {}
    if etype == "checkout.session.completed":
        _on_checkout(db, obj)
    elif etype == "customer.subscription.updated":
        _on_subscription(db, obj, deleted=False)
    elif etype == "customer.subscription.deleted":
        _on_subscription(db, obj, deleted=True)


def _on_checkout(db: Session, session: dict) -> None:
    org_id = (session.get("metadata") or {}).get("org_id") or session.get("client_reference_id")
    plan = (session.get("metadata") or {}).get("plan")
    customer = session.get("customer")
    org = db.get(Organization, org_id) if org_id else None
    if org is None:
        logger.warning("stripe checkout for unknown org %r", org_id)
        return
    if customer:
        org.stripe_customer_id = str(customer)
    if plan in PLANS:
        org.plan = plan
    db.commit()
    events_svc.record(
        db, actor_type="system", actor_label="stripe", surface="webhook",
        action="stripe_checkout", target_type="org", target_id=org.id,
        meta={"plan": org.plan, "type": "checkout.session.completed"},
    )


def _org_by_customer(db: Session, customer_id: str) -> Organization | None:
    if not customer_id:
        return None
    return db.scalar(
        select(Organization).where(Organization.stripe_customer_id == customer_id)
    )


def _price_from_subscription(sub: dict) -> str:
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return ""
    price = (items[0].get("price") or {})
    if isinstance(price, str):
        return price
    return str(price.get("id") or "")


def _on_subscription(db: Session, sub: dict, *, deleted: bool) -> None:
    customer = str(sub.get("customer") or "")
    org = _org_by_customer(db, customer)
    if org is None:
        org_id = (sub.get("metadata") or {}).get("org_id")
        org = db.get(Organization, org_id) if org_id else None
    if org is None:
        logger.warning("stripe subscription for unknown customer %r", customer)
        return
    if customer and not org.stripe_customer_id:
        org.stripe_customer_id = customer
    if deleted:
        org.plan = "free"
    else:
        plan = plan_for_price(_price_from_subscription(sub)) or (sub.get("metadata") or {}).get("plan")
        if plan in PLANS:
            org.plan = plan
    db.commit()
    events_svc.record(
        db, actor_type="system", actor_label="stripe", surface="webhook",
        action="stripe_subscription", target_type="org", target_id=org.id,
        meta={"plan": org.plan, "deleted": deleted},
    )
