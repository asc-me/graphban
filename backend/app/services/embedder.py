"""One embedding credential per deployment, and the dimension that gates it (PRD-25 S4, D-c).

**The probe IS the check.** Embed one short string with the candidate model and read the
vector's length — that length *is* the dimension. Nothing is inferred from a model name or a
lookup table, so there is no "the probe passed but the model turned out to be incompatible"
case to handle later.

**Unknown is NOT permission, and this deliberately contradicts GRPH-485.** There, a provider
that could not be reached was treated as *unchecked, not invalid*, because a brief blip
blocking a correct edit was the whole cost. Here the worst case is different in kind: an
unverified embedder writing vectors of one width into a column of another. Both rules are
correct for their own blast radius, and they are written down together so the difference reads
as a decision rather than an inconsistency.

**A dimension change cannot be made from a form.** `EmbeddingType(settings.embed_dim)` is
evaluated at MODEL-DEFINITION time (`models/__init__.py`), so the column width is fixed when the
module imports. A settings dialog that appeared to offer a different dimension would be lying —
it would need a migration and a process restart. So it is refused, naming `EMBED_DIM`.

**Changing the embedder while vectors exist is refused even at the same dimension.** Same width
does not mean same space: neighbours computed under the old model are meaningless under the new
one, and search would quietly return them. Re-indexing is a separate, deliberate action
(GRPH-536), never a side effect of saving a form.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import providers
from app.config import settings
from app.models import CodeNode, Credential, DeploymentConfig, MemoryShard
from app.security import secrets

logger = logging.getLogger("graphban.embedder")

#: The string the dimension probe embeds. Short and content-free: this asks the provider what
#: shape it returns, not what it thinks of the text.
PROBE_TEXT = "dimension probe"


class EmbedderRefused(Exception):
    """A proposed embedding credential cannot be accepted. Carries `reason` so a caller can
    tell the three cases apart without matching on prose."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason  # unreachable | wrong_dimension | vectors_exist
        super().__init__(message)


def probe_dimension(kind: str, base_url: str, api_key: str, model: str) -> int:
    """The dimension this credential actually produces, or raise.

    Raises `EmbedderRefused("unreachable", ...)` when the provider cannot be asked. That is the
    "unknown is not permission" rule: a provider that did not answer has not demonstrated
    anything, and accepting it would let vectors of an unverified width reach a fixed-width
    column.
    """
    embedder = providers.build_embedder(kind, base_url=base_url, api_key=api_key, model=model)
    try:
        vector = embedder.embed(PROBE_TEXT)
    except Exception as exc:  # noqa: BLE001 — every failure here has the same consequence
        raise EmbedderRefused(
            "unreachable",
            f"{kind} at {base_url or '(no endpoint)'} could not be asked for a vector: {exc}. "
            "An embedder that has not answered cannot be accepted — unlike a chat provider, an "
            "unverified one writes into a fixed-width column.",
        ) from exc
    if not vector:
        raise EmbedderRefused(
            "unreachable",
            f"{kind} returned an empty vector for a probe, so its dimension is unknown.",
        )
    return len(vector)


def vectors_exist(db: Session) -> bool:
    """Whether anything has already been embedded in this deployment.

    Checked across BOTH vector-bearing tables. Looking at one would answer "are there vectors"
    with "are there vectors in the table I happened to check", and a deployment with an empty
    memory but a populated code graph would sail past the guard.
    """
    for model in (MemoryShard, CodeNode):
        found = db.execute(
            select(func.count()).select_from(model).where(model.embedding.isnot(None))
        ).scalar_one()
        if found:
            return True
    return False


def set_embed_credential(db: Session, scope: str, credential_id: str, *,
                         allow_reindex: bool = False) -> DeploymentConfig:
    """Point a scope's embedding at a credential, after proving it is safe.

    Three refusals in a deliberate order — cheapest and most certain first:

    1. **wrong dimension** — refused whatever else is true. It can never be made to work from
       here, because the column width is fixed at import.
    2. **unreachable** — refused because unknown is not permission (above).
    3. **vectors_exist** — refused unless the caller has explicitly asked for a re-index, since
       the same width is not the same space.
    """
    cred = db.get(Credential, credential_id)
    if cred is None or (cred.org_id or "") != (scope or ""):
        raise LookupError(credential_id)

    dimension = probe_dimension(cred.kind, cred.base_url,
                               secrets.decrypt(cred.api_key) if cred.api_key else "",
                               cred.model)
    if dimension != settings.embed_dim:
        raise EmbedderRefused(
            "wrong_dimension",
            f"{cred.kind}/{cred.model} returns {dimension}-dimensional vectors and this "
            f"deployment's columns are {settings.embed_dim}-wide. Changing that needs "
            "EMBED_DIM and a migration, not a settings change: the column width is fixed when "
            "the models import, so nothing chosen here can resize it.",
        )

    row = db.get(DeploymentConfig, scope or "")
    changing = row is not None and row.embed_credential_id not in (None, credential_id)
    if (changing or (row is None or row.embed_credential_id is None)) and vectors_exist(db) \
            and not allow_reindex:
        raise EmbedderRefused(
            "vectors_exist",
            "vectors already exist under a different embedder. Same dimension is not the same "
            "space — neighbours computed under the old model are meaningless under the new one "
            "and search would return them without saying so. Re-index explicitly.",
        )

    if row is None:
        row = DeploymentConfig(scope=scope or "")
        db.add(row)
    row.embed_credential_id = credential_id
    db.commit()
    db.refresh(row)
    logger.info("embedding credential for scope %r set to %s (%s, dim %d)",
                scope, credential_id, cred.model, dimension)
    return row


def resolve_embedder(db: Session, scope: str = ""):
    """The embedder this deployment should use, or the configured fallback.

    **This replaces reading it from whichever project sorted first alphabetically.** `lifespan`
    used to call `apply_llm(get_config(db, first.id))` where `first` was the alphabetically
    first project — so on a multi-project deployment the embedder was decided by a project
    NAME, and renaming a project could silently change which model produced every vector
    written afterwards.

    Falling back to the env-configured embedder when no credential is set keeps an offline or
    freshly-installed deployment working exactly as it does today.
    """
    row = db.get(DeploymentConfig, scope or "")
    credential_id = row.embed_credential_id if row else None
    if not credential_id:
        return providers.get_embedder()
    cred = db.get(Credential, credential_id)
    if cred is None or (cred.org_id or "") != (scope or ""):
        logger.warning("embedding credential %s is not readable in scope %r; using the "
                       "environment's embedder", credential_id, scope)
        return providers.get_embedder()
    return providers.build_embedder(
        cred.kind, base_url=cred.base_url,
        api_key=secrets.decrypt(cred.api_key) if cred.api_key else "", model=cred.model,
    )


def apply_embedder(db: Session, scope: str = "") -> str:
    """Point the live embedding layer at the deployment's credential. Returns what it chose.

    Called once at boot. Returns a short description rather than nothing so the caller — and a
    test — can tell "used the deployment credential" from "fell back to the environment"
    without inspecting module state.
    """
    row = db.get(DeploymentConfig, scope or "")
    credential_id = row.embed_credential_id if row else None
    if not credential_id:
        return "environment"

    cred = db.get(Credential, credential_id)
    if cred is None or (cred.org_id or "") != (scope or ""):
        logger.warning("embedding credential %s is not readable in scope %r; keeping the "
                       "environment's embedder", credential_id, scope)
        return "environment"

    providers.set_active_embedder(
        cred.kind, base_url=cred.base_url,
        api_key=secrets.decrypt(cred.api_key) if cred.api_key else "", model=cred.model,
    )
    return f"credential:{cred.id}"
