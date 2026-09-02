# AI providers

Every AI capability in Graphban sits behind a small **provider abstraction** so the app
runs fully offline by default and swaps in real models without touching call sites.

## Three capabilities

`backend/app/providers/base.py` defines three protocols:

| Protocol | Used by |
| --- | --- |
| `Embedder` | Memory embedding + semantic search, duplicate detection |
| `ChatModel` | Agent chat (with a `stream()` method for SSE) |
| `Extractor` | Auto-extraction on `done`, PRD AI commands, the `extract_lessons` MCP tool |

## Implementations

| Provider | Chat / extraction | Embeddings | Extra deps |
| --- | --- | --- | --- |
| **stub** (default) | deterministic composed reply | hashed bag-of-tokens → L2-normalized vector | none |
| **ollama** | `POST {base}/api/chat` (+ streaming ndjson) | `POST {base}/api/embeddings` | httpx |
| **anthropic** | Claude Messages API (`claude-opus-4-8`) | — (no embeddings endpoint) | `anthropic` (optional `cloud` extra) |
| **openai** | — | `POST {base}/v1/embeddings` | httpx |

The **stub** is deterministic (same text → same vector/reply), which keeps the stack offline
and makes tests reproducible. It's honest: the stub chat reply grounds itself in real project
data and tells you how to enable a real model. Because Anthropic has no embeddings endpoint,
**cloud embeddings** go through any OpenAI-compatible `/v1/embeddings` API.

## Choosing providers

Two independent selectors:

- **`CHAT_PROVIDER`** — `stub | ollama | anthropic`. Drives chat, auto-extraction, and PRD AI
  commands. **Switchable live** from [Settings → AI Providers](settings.md#ai-providers-tab)
  (or env). Changing it updates the in-memory settings and resets the provider cache.
- The Settings editor is a list of **credentials** — one row per provider key. The shipped
  catalogue lives in `backend/app/providers/registry.py`; every OpenAI-compat entry carries
  an editable endpoint, so a gateway or local server (vLLM, LM Studio, an internal proxy)
  is configuration rather than a code change (GRPH-625). Defaults named in the catalogue
  are best-known-at-filing; on save, a provider that answers is asked for its live model
  list, and a wrong default comes back as a refusal naming what it actually offers (a
  provider that cannot be asked saves as `pending_validation` and is retried in the
  background).
- **`EMBED_PROVIDER`** — `stub | ollama | openai`. Drives memory embedding + search and
  duplicate detection. This is a **deploy-time** setting.

### Why embeddings are deploy-time

The pgvector column dimension is fixed when the schema is created. Different embedders have
different dimensions (stub 384, `nomic-embed-text` 768, OpenAI `text-embedding-3-small`
1536). To switch: set `EMBED_PROVIDER` **and** `EMBED_DIM` to match, reprovision the database
(so the `vector` column has the right width), then re-embed everything:

```bash
curl -s -X POST http://localhost:8000/api/memory/backfill -H "Authorization: Bearer <jwt>"
```

## Configuration

Set in `.env` / the environment (see [Configuration](configuration.md)):

```bash
CHAT_PROVIDER=stub                 # stub | ollama | anthropic
EMBED_PROVIDER=stub                # stub | ollama | openai
EMBED_DIM=384                      # must match EMBED_PROVIDER

# local (Ollama) — from Docker, the host is host.docker.internal
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_CHAT_MODEL=llama3.1:8b
OLLAMA_EMBED_MODEL=nomic-embed-text

# cloud
OPENAI_API_KEY=...                 # for OpenAI-compatible embeddings
OPENAI_EMBED_MODEL=text-embedding-3-small
ANTHROPIC_API_KEY=...              # read by the anthropic SDK
ANTHROPIC_MODEL=claude-opus-4-8
```

The `anthropic` SDK is only imported when `CHAT_PROVIDER=anthropic`; install it with the
`cloud` extra (`pip install -e ".[cloud]"`) or add it to the backend image.

## How it works

- `backend/app/providers/__init__.py` is the registry (`get_embedder`, `get_chat_model`,
  `get_extractor`, `iter_reply`, `reset`), cached per process.
- `backend/app/services/platform.py::apply_llm` maps the platform `llm_mode` to the chat
  provider and resets the cache — this is what makes the Settings switch take effect live.
- `backend/app/embeddings.py` is a thin back-compat shim over the registry.

## Eval harness (GRPH-224)

Deterministic tests cover code. They do not cover what an extractor *says*. Golden-set
fixtures live in `backend/app/evals/cases/<surface>/` and run through the real service
(`extract_lessons`, `grill_prd`, `assistant`, `prd_eval`).

```bash
# Mechanical checks only — the stub cannot judge substance, and that is ungraded, not a pass.
graphban eval --surface extract_lessons
graphban eval --surface grill_prd
graphban eval --surface assistant
graphban eval --surface prd_eval

# Ask the project's chat model (three samples, unanimity on groundedness). Stub stays ungraded.
graphban eval --surface extract_lessons --judge
```

The report status is `ok`, `failed`, or `absent`. `absent` is a missing or empty cases
directory — an empty tree must not look like a green run. `graded: false` means the judge
was not asked or could not decide; it is not a quality pass.

The judge reuses the project's chat provider. A dedicated judge model is GRPH-316, not a
new setting. The rubric is groundedness against the fixture (forbidden claims as settled
fact), not "is this a good lesson?" — that question is how a fluent false extraction
scores well.

Human labels in v1 *are* the golden JSON. Live sampling reuses Memory review; there is
no second queue. `generate_digest` is a template, not a model call, so it is not a surface.

Judge calls are tagged `evals.judge` on `llm_call_spans` (GRPH-225).

Live human-eval (GRPH-644) samples those spans into Memory review as **candidates**
(`origin: agent:eval-sample`). Unlabelled candidates are `ungraded`, not a pass.
Promote prints JSON for a human to paste into `app/evals/cases/` — it does not write
the repo. Stub spans are skipped (labelling the offline heuristic is not a live eval).

```bash
graphban eval sample --limit 20
graphban eval labels
graphban eval promote --shard <id>
```

## Related

- [Memory & chat](memory-and-chat.md) · [PRDs](prds.md) · [Settings](settings.md)
