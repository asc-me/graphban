# PRDs

A dedicated PRD (Product Requirements Doc) tracker with a **markdown editor**, **live
preview**, **version history + diff**, **AI drafting commands**, and **item links**.

## PRD list (`/prds`)

Each PRD row shows its id, title, linked-item chips, current version, status, and updated
date. **New PRD** opens a dialog to create one from a template (`standard` / `blank`) **or
import a `.md` file** — choose *Import a .md file* and the whole markdown becomes the PRD body
verbatim (no template applied). The title defaults to the file's first `# heading`, falling
back to the file name; the initial version snapshot is noted "Imported from markdown." This is
the same path a future Google Drive sync uses for files dropped into the `PRDs/` subfolder
(see [Settings → Google Drive](settings.md#google-drive)).

| Field | Notes |
| --- | --- |
| `id` | e.g. `PRD-1` |
| `status` | `draft` · `review` · `approved` · `closed` — the first two are SETTABLE, the last two are EARNED (see below) |
| `version` | e.g. `v1.0` |
| `body` | Markdown |
| `linked` | List of tracker item ids |

### Templates

- **Standard** — a full skeleton (Overview / Goals / Non-Goals / Key Features / Success
  Metrics / Risks & Open Questions).
- **Blank** — just the title heading.

## PRD editor (`/prds/:id`)

**`approved` is reached, never set.** It is earned by finishing the grill; a PATCH that
tries to set it directly is refused (`services/prds.py` — "approved is reached by
finishing the grill, not set directly"). `closed` is reached by closing the PRD out.
That is why the status dropdown offers two states and the table above lists four.

A split view: a raw **markdown editor** on the left, a **live rendered preview** on the
right (a dependency-free renderer handling headings, lists, code, bold/italic/inline-code,
rules). The header has the editable title, a **status** dropdown, the version badge, and
**Save** / **Snapshot** buttons.

### Version history & diff

Toggle the right pane to **History**: a list of every version (number, note, date, "current"
marker). Click a version to render a **line-level diff** (LCS-based) between that version's
snapshot and the current draft — added lines in green, removed in red.

- **Snapshot** cuts a new version: it saves the current body, appends a `PrdVersion`
  snapshot with your note, and **bumps the minor version** (e.g. `v0.4 → v0.5`).
- Seeded PRDs carry their design history; only the latest snapshot stores the full body.

### AI commands

The toolbar has three provider-backed commands that **append** generated markdown to the
body:

- **Expand** — expand the section under discussion into prose.
- **Generate risks** — produce a "## Risks & Open Questions" section.
- **Summarize** — an executive summary.

The offline **stub** returns deterministic, insertable snippets per command (and tells you to
configure a provider for real drafting); with Ollama/Claude configured, they're generated.
See [AI providers](ai-providers.md).

### Readiness to approve (GRPH-80)

The grill tab shows a **Readiness to approve** panel next to grill progress. It is
advisory — finishing the grill still approves the PRD; a low score warns, it does
not block.

- **Mechanical (GET `/evaluate`, always):** are Problem / Scope / Non-goals /
  Acceptance present and substantive (the standard template's italic placeholders
  count as thin, not present), plus coverage gaps from `prd_coverage`. A PRD with
  no buildable sections says so rather than reading as fully covered.
- **LLM (POST `/evaluate`, on demand):** ambiguity and testability. Stub, split, or
  an unasked judge is **ungraded**, not a fail. A judged `ready` cannot paper over
  missing sections.

Ungraded is named on the panel (`not judged — …`). If the grill has already
approved and nobody asked the judge, the panel says so rather than looking clean.

### Linking items

The **Linked** dropdown lists tracker items with a checkbox to link/unlink; linked ids show
as chips on the list view.

## How it works

- Service: `backend/app/services/prds.py` — CRUD, templates, `create_version` (snapshot +
  minor bump), `link_item`, `ai_command`.
- Router: `backend/app/routers/prds.py`.
- Frontend: `web/src/features/prds/` plus the shared `lib/markdown.tsx` renderer and
  `lib/diff.ts` line-diff.
- Schema: the `prds` and `prd_versions` tables were added by Alembic migration `0002`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/prds` | List (summaries) |
| POST | `/api/prds` | Create (`{title, template}`) |
| GET | `/api/prds/{id}` | Fetch one (with body) |
| PATCH | `/api/prds/{id}` | Update title / body, and status **to `draft` or `review` only** |
| GET | `/api/prds/{id}/versions` | Version history (newest first) |
| POST | `/api/prds/{id}/versions` | Snapshot + bump version (`{note}`) |
| POST | `/api/prds/{id}/link` | Link/unlink an item (`{item_id, add}`) |
| POST | `/api/prds/{id}/ai` | Run an AI command (`{command}`) |
| GET | `/api/prds/{id}/evaluate` | Mechanical pre-approval quality (no chat call) |
| POST | `/api/prds/{id}/evaluate` | Mechanical + LLM; advisory, never mutates status |
