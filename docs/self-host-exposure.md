# Self-host: exposing the feedback API to the public internet

<!-- framing -->

Graphban's feedback intake (`POST /api/public/requests`) and public boards are
designed to be reachable by your app's end-users. On the hosted SaaS this is
handled for you. On a self-hosted deployment you need to expose **only the
public surfaces** to the internet while keeping the signed-in app, MCP API,
and admin routes private.

This page documents how. The anti-pattern is port-forwarding `:8000` or
`:8080` — that exposes everything, including authenticated routes.

## What to expose

Only these routes need public access:

| Route | Purpose |
| --- | --- |
| `POST /api/public/requests` | Feedback intake (bugs, features) |
| `POST /api/public/attachments` | Screenshot upload |
| `GET /api/public/attachments/{id}` | Serve uploaded screenshots |
| `GET /api/public/duplicates` | Live duplicate check for widget |
| `GET /api/public/roadmap` | Public roadmap (if enabled) |
| `GET /api/public/boards/issues` | Public issues board |
| `GET /api/public/boards/requests` | Public requests board |
| `GET /api/public/t/{token}` | Submitter tracking page |
| `POST /api/public/requests/{id}/vote` | Anonymous public voting |
| `GET /api/public/widget-config` | Widget configuration |
| `GET /api/public/slugs/validate` | Slug validation |

**Do NOT expose:**

- `/api/` (authenticated MCP/tracker API)
- `/api/auth/*` (login, signup)
- `/api/items/*`, `/api/requests/*` (triage — JWT-protected)
- `/api/platform/*` (settings)
- The web UI at `/` (signed-in app shell)

## Protection on public routes

Even when exposed, the intake API is protected by:

1. **Ingest token** — `Authorization: Bearer gbfb_…` (per-project, rotatable, stored hashed)
2. **Rate limiting** — per-project, per-IP (default 20/min)
3. **Turnstile** — optional CAPTCHA on the widget (browser-only; native apps skip it)
4. **Honeypot** — hidden form field catches bots

Set `PUBLIC_SUBMIT_ENABLED=true` and turn on the `intake_enabled` flag per project.

## Recommended: Cloudflare Tunnel

The recommended approach is a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
with a named hostname and TLS, fronting **only** the public routes.

```yaml
# cloudflared config example
tunnel: <your-tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json

ingress:
  # Public feedback surfaces — allow these.
  - hostname: feedback.yourdomain.com
    path: /api/public/requests
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/attachments/*
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/duplicates
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/roadmap
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/boards/*
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/t/*
    service: http://localhost:8000
  - hostname: feedback.yourdomain.com
    path: /api/public/widget-config
    service: http://localhost:8000
  # Everything else — reject.
  - service: http_status:404
```

This gives you TLS termination, DDoS protection, and path-level allow-listing
without opening any inbound ports on your firewall.

## Alternative: Tailscale Funnel

For private-network or team-internal use, [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
provides a publicly reachable HTTPS endpoint backed by your Tailnet. Same
principle: point it at the Graphban origin and path-allow only the public routes.

## Alternative: Reverse proxy with TLS

If you already run nginx or Caddy, configure a `server` block for the feedback
hostname that proxies only the public paths:

```nginx
server {
    listen 443 ssl;
    server_name feedback.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/feedback.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/feedback.yourdomain.com/privkey.pem;

    location /api/public/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Everything else — 404.
    location / {
        return 404;
    }
}
```

## What not to do

- **Do not** port-forward `:8000` or `:8080` to the internet. This exposes the
  entire API including authenticated routes, the web UI, and MCP.
- **Do not** disable the ingest token. It is the credential your app sends;
  without it anyone who guesses a project ID can submit.
- **Do not** set `intake_enabled` on projects that should not receive public
  feedback. The flag is per-project and off by default.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `PUBLIC_SUBMIT_ENABLED=true` | Global kill switch for public intake |
| `HOSTED_MODE=true` | Refuse raw `project_id` in POST body (hosted only) |

## Feedback Kit snippet

When your app is self-hosted, the Feedback Kit widget snippet should point at
your tunnel origin, not `cloud.graphban.dev`:

```html
<script>
  window.__GRAPHBAN_FEEDBACK__ = {
    origin: "https://feedback.yourdomain.com",
    token: "gbfb_…",  // your project's ingest token
  };
</script>
<script src="https://feedback.yourdomain.com/embed/feedback.js" defer></script>
```
