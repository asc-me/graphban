# Dashboard

The **Dashboard** is a single-glance view of project health, powered by one aggregation
endpoint.

## What it shows

- **KPI tiles** — total items, in-progress, blocked, memory shards, PRDs, and total MCP calls.
- **Item status distribution** — a segmented bar across the six states, with a labeled legend
  (state + count). Identity is carried by the label, never color alone.
- **Requests by type** — a small horizontal bar per type (bug/feature/enhancement/feedback)
  with counts.
- **Recent activity** — the most recently updated items with their status and date.

The charts follow a small visualization method: fixed status/type palettes, labeled marks,
rounded bar ends with 2px gaps, and text in ink tokens (never the series color).

## How it works

- One call, `GET /api/dashboard`, aggregates everything: `backend/app/services/dashboard.py`
  computes status counts, effort totals, request breakdowns, shard/PRD counts, total MCP
  calls (from the metering table), and the recent-items list.
- Frontend: `web/src/features/projecthome/ProjectHome.tsx`. The nav calls this page Dashboard. `/dashboard` redirects to it (`/home` on self-host, `/p/:tag` when hosted).

## API

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/dashboard` | `items_total`, `items_by_status`, `effort_total`, `in_progress_count`, `blocked_count`, `requests_total`, `requests_by_type`, `requests_by_status`, `shard_count`, `prd_count`, `mcp_calls`, `recent_items` |

Optional `?project_id=` scopes the aggregation.
