# LLM call observability (GRPH-225)

> The item is high-fidelity: the store decision below came before the build, and the
> build records it in `backend/app/providers/llm_meter.py`'s module docstring. This is
> the longer form — why this shape and not the obvious others.

## The problem

Every feature that spends money on tokens — the assistant, the grill, MCP tools,
memory judging, lesson extraction, artifact drafting — resolves its own provider via
`platform_svc`. Before this, nothing recorded what those calls cost. `Event` counts
mutations, `OrgUsage` counts hosted calls, `McpToolStat` counts tool traffic; none of
them know a model name. "Which project is burning the budget, on which feature, with
which provider, and did it work" was not answerable at all, and the absence was
invisible: a provider returning 429s all week looked identical to nobody using it.

## The store: a SQL table, not OpenTelemetry

**`llm_call_spans`, one row per provider call, plus a structured log line per span.**
The OTel alternative was considered and rejected on three grounds:

1. **The consumers are SQL.** The analytics work charts from tables, and a cost panel
   is a `GROUP BY feature/project` over `llm_call_spans`. A trace explorer is a
   different product for a different question.
2. **OTel's export half is already here without the dependency.** Every span also logs
   `logger.info(..., extra={"llm": {...}})`, which `LOG_JSON=true` ships verbatim to
   the log platform (the GRPH-33 pattern). Neither deployment has a collector; adding
   one to make a claim about "observability standards" would add the only thing the
   system has ever done without needing.
3. **`Event` is the wrong table.** It is the audit ledger — one row per accepted
   *mutation*, written at the boundary with actor identity. A span is per-*provider-call*
   telemetry: two orders of magnitude more volume, error rows included (a failed call
   spent the money), and its own retention policy. Mixing them corrupts both semantics.

## Capture: the construction chokepoint, not the call sites

The protocols return bare types — `ChatModel.chat() -> str`, `Embedder.embed() ->
list[float]`, `Extractor.extract() -> list[str]`. There is no usage field to read at
the call site, and changing every protocol to return a richer result to carry it would
touch every adapter and every consumer for telemetry's convenience.

So the shape is:

- **`metered(obj, provider=, model=, base_url=, project_id=)`** is applied inside
  `build_chat` / `build_extractor` / `build_embedder` (and the env-path `get_embedder`).
  Construction is the only point that knows BOTH the provider id and the resolved model,
  and every public path to an adapter passes through it. A tenth provider entry is not a
  tenth instrumentation site.
- **Token counts arrive by contextvar handshake.** The adapters already parse the usage
  block from the HTTP response, so each calls `llm_meter.record_usage(input=…, output=…)`
  at that point. The wrapper opens a sink around the call and drains it on completion;
  providers that report nothing simply never fill it, and the wrapper falls back to a
  chars/4 estimate *flagged as an estimate*.
- **The `_inside` guard keeps one span per provider call.** Extractors construct raw
  inner chats, and compat `chat()` is assembled from `stream()` — without the guard,
  nested wrapped calls would double-count. Nested calls pass through silent; the outer
  span owns them.
- **Generators keep their return contract.** `stream_turn` returns its `ToolTurn` via
  generator `return`; `_drive_gen` preserves `StopIteration.value` on the way out,
  because `iter_reply` and the assistant loop read it.

## Attribution

- **`project_id` is bound at construction**, passed down from the resolvers, not read
  from a contextvar at emit time. A request that resolves project A and then writes for
  project B would misattribute every contextvar read; an instance-bound project cannot.
  Deployment-scoped embedders carry `""` — unknown project, its own chartable bucket.
- **`feature` is tagged at the call site** with `llm_context(feature=…)`: `assistant`,
  `grill`, `grill.classify`, `prd.judge`, `memory.judge`, `memory.search`,
  `lessons.extract`, `artifacts.classify`, `artifacts.draft`, `embed.write`, and
  `mcp:<tool>` for every MCP tool call. `""` means the call site never tagged — stored
  as `""` and chartable as "untagged", never silently bucketed into some default.
- **Two places need the set-never-reset shape**, both because the LLM call runs in a
  *different copied context* than the scope that knows what it is: the MCP dispatcher
  (tool exec lands in a `run_in_threadpool` context; deferred analytics writes land in
  a BackgroundTask context, each a copy of the request task's context — and each
  request task gets its own copy, so an un-reset set cannot leak between requests), and
  the tool-session factory (`_wrap_session_factory` stamps the creation-time feature
  onto the session's spans, because the session is created in the request scope but its
  turns stream from the response generator's thread).
- **`request_id` rides along** from the middleware's contextvar, joining every span of
  one request in the log platform.

## Absence conventions

This repo's recurring defect class is *absence reading as clean*, and a cost table is
its natural habitat. The encoding:

| Fact | Row | Why not the shortcut |
| --- | --- | --- |
| Model matches no price prefix | `cost_usd = NULL` | A `0` on a cost panel is a claim about money. NULL is "we cannot price this". |
| Provider reported nothing | token counts estimated, `tokens_source="estimated"` | The estimate exists so an unreporting provider still has an order of magnitude to chart — flagged, so nobody bills against it. |
| Stub / local ollama | `cost_usd = 0.0`, `tokens_source="none"` until the provider reports | This spend is *provably* zero — the compute is on hardware already paid for. `LOCAL_PROVIDERS`. |
| Call site never tagged | `feature = ""` | Its own bucket, visible and chartable, not silently attributed to a default. |
| Telemetry row never written | (span write is swallowed on failure) | See below — but a *missing* span must never be filled with a fabricated one. |

## The one invariant that outranks the rest

**Telemetry must not break the feature.** `_persist` wraps the DB write in
try/except-and-warn; the chat answer still returns when the span row cannot. The
provider call's outcome belongs to the user; the span belongs to us. The same applies
at startup: the retention sweep (`purge_expired`, driven by
`LLM_SPAN_RETENTION_DAYS`) logs loudly if it fails but never blocks boot.

## Failover semantics

Both chats handed to `FailoverChat` are built through `build_chat`, so each carries its
own wrapper with its own provider/model; the failover router itself is not wrapped. A
primary that fails and a fallback that succeeds therefore produces **two spans** —
one with `ok=false` and the error class, one clean. That is the billing truth (the
failed attempt was still a request), and `retryable` / `http_status` make the pair
readable as a failover event. AL-179's per-thread token accumulation in the assistant
continues unchanged alongside; the spans are the per-call ledger, not a replacement for
the per-conversation display.

## What this is NOT

- Not billing. `PRICES` are public list prices matched by longest model prefix; the
  point is the *shape* of spend — which feature and which tenant moves the number —
  and that survives being roughly right.
- Not an audit trail. Spans carry no actor; `Event` owns that. Attribution is
  project × feature, deliberately coarser.
- Not a trace system. There is one span per call, no parent ids. Correlate via
  `request_id`.
