"""Startup security checks (AL-44 / review finding F2).

The app must never silently boot internet-exposed with the well-known default
JWT secret — anyone knowing it can mint tokens for any user. We can't reliably
detect "is this production?", so the policy is:

- SQLite (tests / zero-infra local) — skip; a throwaway DB isn't a target.
- Otherwise — always emit a loud warning on a weak/default secret, and REFUSE to
  start when the operator has opted into enforcement (``REQUIRE_STRONG_SECRET``).

This keeps `docker compose up` working out of the box while giving a one-flag
hardening switch for a real deploy.
"""
from __future__ import annotations

from app.config import settings

_BANNER = "=" * 72


def check_security() -> None:
    if settings.is_sqlite:
        return

    # Hosted (multi-tenant) mode must encrypt BYOK provider keys at rest — refuse to
    # boot a shared instance that would store tenants' keys in plaintext (AL-73).
    if settings.hosted_mode and not settings.secret_encryption_key:
        raise RuntimeError(
            "refusing to start: HOSTED_MODE is on but SECRET_ENCRYPTION_KEY is unset — "
            "tenant provider keys would be stored in plaintext. Set a strong "
            "SECRET_ENCRYPTION_KEY."
        )

    # Hosted + stub embeddings is a SILENT failure: stub vectors are deterministic
    # noise, so search returns confident nonsense while every health check stays green
    # (AL-136). Warn loudly by default — refusing outright would strand an existing
    # deployment mid-migration — and refuse only when the operator opts in.
    if settings.hosted_mode and settings.embed_provider == "stub":
        message = (
            "HOSTED_MODE is on but EMBED_PROVIDER is 'stub'. Stub vectors are "
            "deterministic noise, so semantic search over memory and the code graph "
            "will return meaningless results while appearing healthy. Configure a real "
            "embedding provider (and set EMBED_DIM to match its output width)."
        )
        if settings.require_real_embeddings:
            raise RuntimeError(
                f"refusing to start: {message} (REQUIRE_REAL_EMBEDDINGS is on)"
            )
        print(f"\n{_BANNER}\n  CONFIGURATION WARNING: {message}\n{_BANNER}\n", flush=True)

    # NO CHAT GUARD HERE, deliberately. `settings.chat_provider` looks like the chat
    # equivalent of the check above, but it is a legacy MIRROR that `platform.apply_llm`
    # writes at runtime — the resolver (`platform._chat_params`) reads
    # `PlatformConfig.active_chat_provider` from the DB, per project. So at boot this
    # field is always the env default regardless of what any project has configured:
    # a guard on it warns forever on a correctly-configured instance, and refusing on it
    # would strand a healthy one. Chat is per-project BYOK, not an instance property.
    # The correct surface already exists: `PlatformConfigOut.effective_chat_provider`
    # resolves it per project and drives the UI's no-model banner (AL-248).

    # A rate limit that cannot tell callers apart is not a rate limit (GRPH-32).
    # `security.net.client_ip` honours X-Forwarded-For ONLY when TRUSTED_PROXY is set,
    # because otherwise the header is client-spoofable. The cost of leaving it off behind
    # a proxy is not "no limit" but something easier to miss: the socket peer is the
    # proxy, so EVERY caller shares one bucket. The first abuser exhausts it and the
    # limit then applies to everyone else — the endpoints look protected and the
    # protection is aimed at the wrong thing.
    #
    # Warned, not refused: hosted mode without a proxy in front is a legitimate
    # configuration, and turning this on for someone who has no proxy would let any
    # caller forge their own bucket key.
    if settings.hosted_mode and not settings.trusted_proxy:
        print(
            f"\n{_BANNER}\n  CONFIGURATION WARNING: HOSTED_MODE is on but TRUSTED_PROXY "
            "is off. If a proxy or load balancer sits in front of this instance, every "
            "caller shares ONE rate-limit bucket (the proxy's IP), so the limits on "
            "/api/public/* and login are effectively global rather than per-client. "
            f"Set TRUSTED_PROXY=true if — and only if — a trusted proxy terminates every "
            f"request.\n{_BANNER}\n",
            flush=True,
        )

    if not settings.jwt_secret_is_weak:
        return

    message = (
        "JWT_SECRET is weak or the built-in default. Anyone who knows it can forge "
        "auth tokens for any user. Set JWT_SECRET to a long random string (>=32 bytes) "
        "before exposing this instance."
    )
    if settings.require_strong_secret:
        raise RuntimeError(
            f"refusing to start: {message} "
            "(REQUIRE_STRONG_SECRET is on)"
        )
    print(f"\n{_BANNER}\n  SECURITY WARNING: {message}\n{_BANNER}\n", flush=True)
