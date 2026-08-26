"""Domain error taxonomy — one owner for "what went wrong" across the app.

Raised by services and the MCP dispatcher; the dispatcher maps each to a stable
machine-readable ``code`` in the JSON-RPC tool error so an agent can branch
without parsing prose (AL-47 / review finding F6). REST routers may also catch
these, though most still use HTTPException directly.

Codes: ``not_found`` · ``validation`` · ``conflict`` · ``unavailable`` (authorization
uses a separate ``unauthorized`` code, owned by ``security/authz.Forbidden``).
"""
from __future__ import annotations


class AppError(Exception):
    """Base for expected, agent-correctable failures. ``hint`` names the fix."""

    code = "internal"

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class NotFound(AppError):
    """A referenced resource does not exist (or is not visible)."""

    code = "not_found"


class Validation(AppError):
    """The arguments are malformed: missing required field, bad enum, wrong type."""

    code = "validation"


class Conflict(AppError):
    """The request collides with current state: lost lease, reused idempotency key."""

    code = "conflict"


class Unavailable(AppError):
    """The operation is well-formed and permitted but cannot run in this instance's
    configuration — e.g. adjudicating a memory candidate with no chat model configured
    (AL-282). Distinct from ``validation`` (the call was fine) and from ``unauthorized``
    (the caller is allowed); retrying without an operator change will not help, so the
    hint names what an operator must configure."""

    code = "unavailable"


class ModelTimedOut(AppError):
    """The configured chat model did not answer inside its budget (GRPH-505).

    Its own code, because the whole point is that a caller can tell it apart from the two it
    used to be indistinguishable from. `unavailable` says an operator must change something
    before a retry can help; `internal` says nothing at all. This one says: the call was
    well-formed, the provider is configured, and **trying again may simply work** — which is
    what was measured. `grill_prd` timed out twice against a 46k-character PRD and succeeded
    twenty minutes later with nothing changed.

    Not to be widened into "any provider failure". A refused key, a wrong base_url and a model
    that does not exist are all permanent until somebody edits something, and collapsing them
    into a code that invites retrying is how a caller ends up retrying forever.
    """

    code = "model_timeout"


class QuotaExceeded(AppError):
    """A hosted plan limit was hit (projects/seats/shards/monthly calls). AL-75.
    Mapped to a ``quota_exceeded`` MCP tool error and, in REST, to HTTP 402."""

    code = "quota_exceeded"


class RateLimited(AppError):
    """Too many requests in a short window (per-org agent-call burst cap). Phase 5.
    Mapped to a ``rate_limited`` MCP tool error; the agent should back off and retry."""

    code = "rate_limited"
