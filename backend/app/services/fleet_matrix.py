"""Committed preference-matrix rows, served to the Fleet UI (GRPH-866).

The supervisor still loads `fleet/src/gbfleet/matrix.toml` to resolve spawns. The web app
cannot read that file (it talks to this API, not the supervisor), so the same facts are
listed here for display. Status still moves by a commit to the toml; this list is the
catalog the page draws, not a second resolver.

Unique on (harness, model, tier). Duplicate toml rows are not a second cell.
"""
from __future__ import annotations

# Facts only. Taste is fleet_profiles; constraints are fleet_policy.
ROWS: tuple[dict, ...] = (
    {
        "harness": "gbagent",
        "model": "qwen3.6:35b-a3b-coding-mtp-det",
        "vendor": "gbagent",
        "lane": "any",
        "tier": "cheap",
        "status": "verified",
        "cost_class": "local",
        "local": True,
    },
    {
        "harness": "gbagent",
        "model": "qwen3-coder:30b",
        "vendor": "gbagent",
        "lane": "any",
        "tier": "cheap",
        "status": "failed",
        "cost_class": "local",
        "local": True,
    },
    {
        "harness": "claude",
        "model": "sonnet",
        "vendor": "anthropic",
        "lane": "any",
        "tier": "cheap",
        "status": "unverified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "qwen-code",
        "model": "",
        "vendor": "alibaba",
        "lane": "any",
        "tier": "cheap",
        "status": "verified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "cursor-agent",
        "model": "composer-2.5",
        "vendor": "cursor",
        "lane": "any",
        "tier": "cheap",
        "status": "unverified",
        "cost_class": "cheap",
        "local": False,
    },
    {
        "harness": "claude",
        "model": "opus",
        "vendor": "anthropic",
        "lane": "any",
        "tier": "frontier",
        "status": "unverified",
        "cost_class": "frontier",
        "local": False,
    },
    {
        "harness": "grok",
        "model": "grok-4.5",
        "vendor": "xai",
        "lane": "any",
        "tier": "frontier",
        "status": "unverified",
        "cost_class": "frontier",
        "local": False,
    },
    {
        "harness": "codex",
        "model": "",
        "vendor": "openai",
        "lane": "any",
        "tier": "frontier",
        "status": "unregistered",
        "cost_class": "frontier",
        "local": False,
    },
)


def payload() -> dict:
    """What `GET /api/fleet` carries as `matrix`. Always present; empty would mean unlooked."""
    return {"rows": [dict(row) for row in ROWS]}


def harnesses() -> list[str]:
    """Unique harness names that can take mix share, excluding unregistered."""
    seen: list[str] = []
    for row in ROWS:
        if row["status"] == "unregistered":
            continue
        name = str(row["harness"])
        if name not in seen:
            seen.append(name)
    return seen
