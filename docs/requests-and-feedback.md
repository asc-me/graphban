# Requests & feedback

Graphban has an internal **triage queue** plus a **public embeddable feedback form** with
**auto-duplicate detection**, and a **Feedback Kit** that generates a themeable widget.

## Requests (triage queue)

The **Requests** view is a linear queue of incoming feature/bug/enhancement/feedback items.

| Field | Notes |
| --- | --- |
| `id` | e.g. `R-31` |
| `type` | `bug` · `feature` · `enhancement` · `feedback` (each has a fixed color) |
| `title`, `by` | Submitter handle |
| `votes` | Upvote count |
| `status` | `new` · `triaging` · `linked` |
| `linked_to` | Item id when linked to a tracker item |

**Use it:** filter by type via the chips, **upvote** with the vote button (optimistic), and
**link** a request to a tracker item via the link dialog (which sets status to `linked`).

## Public embeddable feedback form

An unauthenticated, themeable widget that drops submissions straight into the triage queue.

- **Standalone page:** `/embed/feedback` — embed it in any site via an iframe.
- **Theming** via URL params: `accent` (hex, no `#`), `radius` (px), `types` (comma list),
  `email` (`1`/`0`), `project`. Example:
  `…/embed/feedback?accent=a78bfa&radius=20&types=bug,feature,feedback`
- **Backend:** `POST /api/public/requests` (ingest token or legacy share token, rate-limited)
  creates the request and returns any duplicates it found. Native apps POST the same
  contract; see the Feedback Kit. Exposing a self-host box to the public internet:
  [self-host exposure](self-host-exposure.md).

### Auto-duplicate detection

As the user types a title (debounced), the widget calls `GET /api/public/duplicates?q=…` and
shows a **"Possibly already reported"** panel with matching items/requests and a similarity
percentage. On submit, the same detection runs server-side and is returned alongside the new
request. This is the AL-21 feature.

**How it works:** `backend/app/services/duplicates.py` embeds the submission via the current
[embedder](ai-providers.md) and cosine-ranks existing items + requests on the fly (default
threshold 0.55, top 5). Provider-agnostic; runs on the offline stub by default.

### Rate limiting & safety

Public endpoints share a simple in-memory, per-IP **sliding-window rate limit** (20/60s) and
respect a `PUBLIC_SUBMIT_ENABLED` flag. (In-memory means per-process — a shared store is
needed for multi-instance deployments.)

## Feedback Kit (widget generator)

The **Feedback Kit** view is a no-code generator for the widget above:

- **Embed mode** — **Inline** (an `<iframe>` + a tiny auto-resize listener) or **Launcher**
  (a self-contained `<script>` that adds a floating button opening the widget in a popover,
  positioned bottom-left/right).
- **Theme** — **dark / light / auto** (`auto` follows the host's `prefers-color-scheme`). The
  widget paints its own surface so it blends on any site; the iframe renders transparent.
- **Accent** — six presets **or a custom hex** (native color picker). Button text auto-contrasts.
- **Corner radius**, enabled **types**, collect-email toggle.
- **Custom copy** — title, subtitle, button label, and thank-you message.
- A **live preview** in **test mode** — submissions are mocked, so configuring never writes junk
  to your triage queue (duplicate detection still runs; it's read-only).

**Auto-resize & host events.** The embedded widget `postMessage`s to the parent:
`{__graphban:true, type:'resize', height}` (both snippets size the frame to content) and
`{type:'submitted', id}` (the launcher closes the popover ~1.8s after a successful submit).

**Captured context.** Every submission records the **page URL** it came from (`source_url`, via
the launcher's `ref` param or `document.referrer`), the submitter's **detail** text, and a
**`meta`** object (the `user_agent` is added server-side; custom fields pass through). Triage
shows the origin as a link on each request row.

**Screenshots.** With attachments enabled, users can attach or **paste** images (bug
screenshots). Files upload to `POST /api/public/attachments` (images only, ≤3 MB, rate-limited),
are referenced by the request's `attachment_ids`, and served public-read by unguessable id.
Triage shows thumbnails on the request row.

**Spam protection** (configured per project in **Settings → Integrations → Spam protection**):
1. **Honeypot** — a hidden field bots fill; always on, zero-config.
2. **Rate limit** — a per-(project, IP) sliding-window cap (`rate_limit_per_min`, default 20).
3. **Cloudflare Turnstile** *(optional)* — set a site key + secret to require a captcha. The
   widget renders the challenge automatically; the server verifies the token. Leave blank
   (default) for no captcha — the app stays fully offline.

The generator and the standalone page share the same `FeedbackWidget` component and config
serialization (`web/src/features/feedback/config.ts`); snippet builders live in `snippets.ts`.

## Inbound GitHub issues → requests/items

A GitHub issues webhook can feed the tracker directly — see
[Settings & integrations](settings.md#integrations) and [API reference](api-reference.md#public-unauthenticated).

## API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/requests` | JWT | List (optional `?type=`) |
| POST | `/api/requests` | JWT | Create |
| POST | `/api/requests/{id}/vote` | JWT | Upvote (`{delta}`) |
| POST | `/api/requests/{id}/link` | JWT | Link to an item (`{item_id}`) |
| POST | `/api/public/requests` | **public** | Submit + return duplicates |
| GET | `/api/public/duplicates` | **public** | Live duplicate check (`?q=&project_id=`) |

## Related

- Duplicate detection reuses the [embedding provider](ai-providers.md).
- The Feedback Kit is one of the "UI templates" from the PRD; the roadmap has a public share
  page too — see [Roadmap](roadmap.md).
