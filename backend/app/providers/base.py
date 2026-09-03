"""Provider protocols for the AI layer (F1).

Three capabilities, each behind a Protocol so implementations swap by config:
  - Embedder:  text -> vector           (memory embedding + semantic search)
  - ChatModel: grounded question -> answer  (agent chat sidebar)
  - Extractor: completed item -> lessons     (auto-extraction on done)

The default implementations (see stub.py) are deterministic and dependency-free,
so the whole stack runs offline. Ollama / OpenAI / Anthropic adapters are opt-in.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Protocol, runtime_checkable

import httpx

from app import errors


#: HTTP statuses worth trying a DIFFERENT credential for (PRD-25 D-h, S3).
#:
#: The line is the one `gbfleet.client` already draws — *"a bad credential does not become
#: true by waiting"*. 429 and 5xx are about the provider's moment; 401/403/400 are about the
#: credential itself, and asking a second provider the same malformed question spends money to
#: be told the same thing twice.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _tagged(exc, *, status: int | None, retryable: bool | None = None):
    """Attach the failover verdict to a provider error.

    **Unknown and vendor-specific statuses are TERMINAL**, and that default is the decision,
    not an oversight (D-h). Failing over on an unclassified error is how one bug becomes a
    doubled bill on every call. The asymmetry is what makes the default safe: a terminal
    misclassification surfaces immediately as an error a human reads, while a retryable one
    hides inside a response that looks fine.
    """
    exc.status = status
    exc.retryable = (status in RETRYABLE_STATUSES) if retryable is None else retryable
    return exc


@contextmanager
def provider_errors(provider: str, *, model: str = "", endpoint: str = ""):
    """Turn a provider transport failure into an ACTIONABLE domain error.

    Without this every provider problem reached the agent as
    `internal error executing 'grill_prd'` with the hint "safe to retry once" —
    which is worse than useless for a misconfiguration: retrying a refused
    connection never helps, and the hint sends the agent off to file a bug instead
    of checking Settings. That message cost two separate debugging sessions (a
    wrong model name, then a wrong base URL on a different project) before anyone
    saw the actual cause, which was one layer down the whole time.

    `Unavailable` is the right class: the call was well formed and permitted, this
    instance just is not configured to serve it, and nothing the caller does
    differently will change that.
    """
    where = f"{provider}" + (f" ({endpoint})" if endpoint else "")
    try:
        yield
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        body = (e.response.text or "")[:200]
        # `status` is carried on the exception, not only formatted into the message.
        # PRD-25 S3 has to decide whether to fail over, and parsing "returned HTTP 429" back
        # out of prose would be a second, weaker copy of a fact we already have.
        if status == 404 and model and model in body:
            raise _tagged(errors.Unavailable(
                f"{where} has no model {model!r}",
                hint=f"pull it on the provider (`ollama pull {model}`) or correct the "
                     "model name in Settings -> AI providers; retrying will not help",
            ), status=404, retryable=False) from e
        raise _tagged(errors.Unavailable(
            f"{where} returned HTTP {status}" + (f": {body}" if body else ""),
            hint="check the provider's credentials and model configuration in "
                 "Settings -> AI providers",
        ), status=status) from e
    except httpx.TimeoutException as e:
        raise _tagged(errors.Unavailable(
            f"{where} timed out",
            hint="the model may be cold or the endpoint overloaded; this one IS worth "
                 "retrying, or raise LLM_TIMEOUT_SECONDS",
        ), status=None, retryable=True) from e
    except httpx.HTTPError as e:
        raise _tagged(errors.Unavailable(
            f"cannot reach {where}: {type(e).__name__}",
            hint="correct the provider base URL in Settings -> AI providers. Note that "
                 "`localhost` resolves to the API CONTAINER, not the host — use the "
                 "host's name or address; retrying will not help",
        ), status=None, retryable=True) from e


class EmptyAnswer(errors.Unavailable):
    """The provider returned a well-formed response with NOTHING in it.

    Its own class so the span table can tell it from a transport failure: ``error_class``
    is the type name, and "the model answered nothing" is a different operational fact from
    "the model was unreachable". Observed live on 2026-09-03: the xAI fallback streamed for
    ~120s, the stream ended with no content delta and no ``[DONE]``, and the empty string
    became a grill result recorded ``ok=true`` with zero output tokens — a blank grill,
    reported as a success. An absence must not read as a clean result.

    Retryable, because a second credential answering is exactly the failover's job here,
    and because the observed cause (a stream severed upstream) is about the provider's
    moment, not the credential.
    """


def require_answer(text: str, provider: str, *, model: str = "", endpoint: str = "",
                   detail: str = "") -> str:
    """Return ``text`` stripped, or raise ``EmptyAnswer`` if there is nothing in it.

    Every ``chat()`` passes its assembled answer through here, so the rule lives in one place
    and a caller holding a ``str`` can trust it is non-empty.
    """
    out = (text or "").strip()
    if out:
        return out
    where = f"{provider}" + (f" ({endpoint})" if endpoint else "")
    raise _tagged(EmptyAnswer(
        f"{where} returned an empty answer" + (f" for model {model!r}" if model else "")
        + (f": {detail}" if detail else ""),
        hint="the provider produced no text; this is worth retrying, and if it keeps "
             "happening check the model in Settings -> AI providers",
    ), status=None, retryable=True)


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


@runtime_checkable
class ChatModel(Protocol):
    # `temperature=0` asks for a deterministic answer. Judging is not writing: a
    # classifier that returns a different verdict for identical input makes approval
    # depend on WHEN it ran. Providers that cannot honour it ignore it.
    def chat(self, *, system: str, context: str, question: str,
             temperature: float | None = None) -> str: ...


@runtime_checkable
class Extractor(Protocol):
    def extract(self, *, title: str, description: str) -> list[str]:
        """Return zero or more memory-shard texts distilled from a completed item."""
        ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
