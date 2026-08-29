"""Prompt caching is deferred against a measurable condition (GRPH-226).

The item calls this a "cheap win": the assistant sends a stable system prompt plus stable tool
schemas, which is an ideal prompt-cache prefix. **Measured, it is not — yet.**

    assistant tool schemas (10 tools) ....  ~511 tokens
    _SYSTEM ..............................   ~73 tokens
    ------------------------------------------------
    stable prefix ........................  ~584 tokens

    configured model `claude-opus-4-8` ...  1,024 token minimum

`context` is the large part and it is NOT stable — it is rebuilt per question from that
question's search hits, so it can never be a cache prefix however big it gets.

**Why this is a deferral rather than a shipped feature.** Anthropic's documented behaviour for a
prefix under the minimum is that the request is *"processed without caching, and no error is
returned"*. So adding `cache_control` today would look exactly like adding it successfully:
no failure, no warning, and a hit rate of zero that nobody is measuring. That is the shape this
repository has a name for, and the docs even give the test — *"if both
`cache_creation_input_tokens` and `cache_read_input_tokens` are 0, the prompt was not cached"*.

Two things are shipped instead, and they are what make the deferral honest:

1. Those two fields are now captured on every turn, so any future claim about caching is
   falsifiable rather than assumed.
2. This test, which fails when the prefix crosses the threshold — the day adding the
   breakpoint stops being decoration, somebody is told. Same shape as `app/scaling.py`, which
   turned GRPH-55's "first project over ~5k items" into a condition that fires.
"""
from __future__ import annotations

import json

import pytest

#: Minimum cacheable prompt length, by model, from
#: https://platform.claude.com/docs/en/build-with-claude/prompt-caching (read 2026-08-28).
#: A prefix shorter than this is silently not cached.
CACHE_MINIMUM_TOKENS = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}
DEFAULT_MINIMUM = 1024


def _stable_prefix_tokens() -> int:
    """Everything the assistant sends that is identical between two questions.

    Tool schemas and the system prompt. NOT `context`, which is assembled per question from
    that question's search hits — the largest part of the payload and the one that can never
    be a prefix.

    Four characters per token, the same estimator `test_mcp_footprint.py` uses. Precision is
    not the point: the gap being measured is hundreds of tokens wide.
    """
    from app.routers.assistant import _SYSTEM
    from app.services import assistant_tools as at

    specs = [t.spec for t in at._TOOLS.values()]
    tools = json.dumps([{"name": t.name, "description": t.description,
                         "input_schema": t.input_schema} for t in specs])
    return (len(tools) + len(_SYSTEM)) // 4


def _minimum_for(model: str) -> int:
    for name, minimum in CACHE_MINIMUM_TOKENS.items():
        if model.startswith(name):
            return minimum
    return DEFAULT_MINIMUM


def test_the_stable_prefix_is_still_too_short_to_cache():
    """THE TRIPWIRE. Fails when the prefix grows past the configured model's minimum — at
    which point `cache_control` on the last tool block stops being decoration and this
    deferral should be revisited.

    Failing here is GOOD NEWS and the message says so. It is not a regression; it is the
    condition GRPH-226 was waiting for.
    """
    from app.config import settings

    prefix = _stable_prefix_tokens()
    minimum = _minimum_for(settings.anthropic_model)

    assert prefix < minimum, (
        f"the assistant's stable prefix is now ~{prefix} tokens, at or past the {minimum}-token "
        f"minimum for {settings.anthropic_model}. GRPH-226 deferred prompt caching precisely "
        "because it was below this line and a breakpoint would have been silently ignored. It "
        "would now bite: add `cache_control` to the last tool block and assert `cache_read` "
        "rises on a repeated call."
    )


def test_the_measurement_is_of_something_real():
    """A prefix computed as 0 would satisfy the tripwire forever. Pinned so the estimate has to
    be reading actual tool schemas."""
    assert _stable_prefix_tokens() > 300, (
        "the stable prefix measures as almost nothing — the tool specs are not being read, so "
        "the tripwire above can never fire")


def test_the_grounding_context_is_not_part_of_the_prefix():
    """The large part of the payload is per-question and therefore uncacheable, which is the
    reason the prefix is small despite the request not being. Asserted so a future reader does
    not "fix" the tripwire by counting context into it."""
    import inspect

    from app.routers import assistant

    source = inspect.getsource(assistant._context)
    assert "thread" in source, "_context no longer derives from the thread"
    assert "_SYSTEM" not in source, (
        "the grounding context now includes the system prompt, so the two are no longer "
        "separable and this file's measurement is wrong")


@pytest.mark.parametrize("model, expected", [
    ("claude-opus-5", 512),
    ("claude-opus-4-8", 1024),
    ("claude-haiku-4-5", 4096),
    ("claude-opus-4-8-20260101", 1024),   # dated variants resolve to their family
    # PINNED BY VALUE, not by the constant (GRPH-590). This row read
    # `("some-unknown-model", DEFAULT_MINIMUM)` — the expected value WAS what the function
    # returns, so both sides moved together and it asserted `DEFAULT_MINIMUM ==
    # DEFAULT_MINIMUM`. Setting the constant to 0 — the exact value the docstring below says
    # would disarm the tripwire — left all nine tests passing.
    ("some-unknown-model", 1024),
])
def test_the_threshold_table_resolves_by_model(model, expected):
    """The thresholds differ fourfold across models, so the tripwire is only meaningful if it
    reads the RIGHT one. An unknown model falls back to 1024 rather than to 0 — guessing low
    would silently disarm this."""
    assert _minimum_for(model) == expected


def test_the_fallback_is_conservative_rather_than_permissive():
    """The second axis, because pinning the literal only catches someone editing the number
    and not someone adding a cheaper model to the table below it.

    Direction is everything here. The tripwire defers when `prefix < minimum`, so a fallback
    that is too LOW makes that comparison false and reports "go ahead" — and Anthropic's
    documented behaviour for a prefix under the real minimum is that the request is processed
    WITHOUT caching and NO ERROR IS RETURNED. A too-low fallback therefore enables a feature
    that silently does nothing, which is the whole reason GRPH-226 shipped a tripwire instead
    of the feature.

    An unknown model is one nobody has measured, so the safe assumption is the strictest
    threshold seen, not the loosest.
    """
    assert DEFAULT_MINIMUM >= min(CACHE_MINIMUM_TOKENS.values()), (
        f"the fallback ({DEFAULT_MINIMUM}) is below the cheapest model in the table "
        f"({min(CACHE_MINIMUM_TOKENS.values())}), so an unknown model would be told a prefix "
        "is long enough when no measured model would accept it"
    )
    assert DEFAULT_MINIMUM > 0, "a fallback of 0 disarms the tripwire for every unknown model"


def test_cache_accounting_is_carried_on_every_turn():
    """The fields the documentation names as the only way to tell whether caching happened.
    Without them a future `cache_control` is unfalsifiable — which is why they ship now, before
    the feature they are here to check."""
    from app.providers.anthropic_provider import _usage

    class _U:
        input_tokens, output_tokens = 100, 20
        cache_read_input_tokens, cache_creation_input_tokens = 800, 0

    assert _usage(_U()) == {"input": 100, "output": 20, "cache_read": 800, "cache_write": 0}
    assert _usage(None) is None

    class _NoCache:                       # a provider that does not report cache fields
        input_tokens, output_tokens = 5, 5

    assert _usage(_NoCache())["cache_read"] == 0, "a missing field must read as 0, not crash"
