"""Resolve MCP tool annotations the way a client must: absent means the spec default.

Kept in one place because two suites need it and because the rule IS the contract — the
manifest emits only the hints that differ from the default (GRPH-48), so a test that reads
`annotations["readOnlyHint"]` directly is asserting on the wire format rather than on what
the tool actually claims.
"""
from app.mcp_server import _ANNOTATION_DEFAULTS


def effective(tool: dict) -> dict:
    """Every hint for `tool`, with defaults filled in for the ones it did not send."""
    return {**_ANNOTATION_DEFAULTS, **(tool.get("annotations") or {})}
