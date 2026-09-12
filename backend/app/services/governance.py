"""Governance and data boundary (PRD-P10 / GRPH-194).

Two independent boundaries:

1. **Inference (BYOK):** the customer configures their own inference endpoint —
   a cloud provider via their key (Anthropic / any OpenAI-compatible) or fully
   local Ollama. Ticket content reaches only the customer's configured endpoint;
   a strict local-only-inference posture is available. No AgentLedger-shared model.
   This boundary is enforced by the existing provider configuration (EMBED_PROVIDER,
   CHAT_PROVIDER) and is not managed here.

2. **At-rest storage:** by DEFAULT the hub stores mirrored ticket bodies (titles,
   descriptions, comments) so cloud-side features (triage board, server-side agent
   reasoning, collision clustering) are full-featured. A **metadata-only tier** is
   available for regulated buyers: the hub stores only IDs/state/assignee/labels/
   timestamps and keeps bodies on the local spoke (fetched from the tracker on demand).

   Under metadata-only, hub-side collision clustering is **unavailable** (a third
   answer, not a blank or labels-derived cluster). An empty or label-derived cluster
   would read as "no collision" when nobody saw the body — that is the absence rule.
"""
from __future__ import annotations

import logging
from typing import Any

from app.models import TrackerLink

logger = logging.getLogger("graphban.governance")

# Storage tier constants.
BODIES_IN_HUB = "bodies_in_hub"
METADATA_ONLY = "metadata_only"

VALID_STORAGE_TIERS = {BODIES_IN_HUB, METADATA_ONLY}

# Fields that are ALWAYS stored (metadata).
METADATA_FIELDS = frozenset({
    "issue_id", "link_id", "tracker_kind", "identifier",
    "canonical_status", "assignee_id", "assignee_name",
    "labels", "tracker_updated_at", "version", "url", "mirrored_at",
})

# Fields that are ONLY stored under bodies_in_hub.
BODY_FIELDS = frozenset({
    "title", "description",
})


def get_storage_tier(link: TrackerLink) -> str:
    """Return the storage tier for a link. Defaults to BODIES_IN_HUB."""
    tier = getattr(link, "storage_tier", None) or BODIES_IN_HUB
    if tier not in VALID_STORAGE_TIERS:
        logger.warning("governance: unknown storage_tier=%r on link=%s, defaulting to bodies_in_hub",
                       tier, link.id)
        return BODIES_IN_HUB
    return tier


def is_metadata_only(link: TrackerLink) -> bool:
    """Check if the link is in metadata-only mode."""
    return get_storage_tier(link) == METADATA_ONLY


def filter_mirror_data(link: TrackerLink, data: dict[str, Any]) -> dict[str, Any]:
    """Filter mirrored issue data based on the link's storage tier.

    Under bodies_in_hub: all fields are stored.
    Under metadata_only: only METADATA_FIELDS are stored; body fields (title,
    description) are dropped.

    Returns a new dict with the appropriate fields.
    """
    if not is_metadata_only(link):
        return dict(data)
    return {k: v for k, v in data.items() if k in METADATA_FIELDS}


def clustering_available(link: TrackerLink) -> bool:
    """Whether hub-side collision clustering is available for this link.

    Under metadata_only, clustering is **unavailable** — not labels-derived, not
    offloaded to spokes. The caller should return a third answer (`unavailable`),
    not a blank or a confident cluster from labels.
    """
    return not is_metadata_only(link)


def inference_boundary() -> dict:
    """Describe the inference boundary for the current deployment.

    BYOK: the customer configures their own inference endpoint. Ticket content
    reaches only the customer's configured endpoint. No AgentLedger-shared model.

    Returns a dict describing the current posture.
    """
    from app.config import settings

    embed_provider = getattr(settings, "embed_provider", "stub")
    chat_provider = getattr(settings, "chat_provider", "stub")

    is_local_only = embed_provider in ("stub", "ollama") and chat_provider in ("stub", "ollama")

    return {
        "embed_provider": embed_provider,
        "chat_provider": chat_provider,
        "local_only_posture_available": True,
        "is_local_only": is_local_only,
        "no_shared_model": True,
    }
