# HTTPS

The box can serve HTTPS beside plain HTTP. Caddy (the `web` service) terminates it; see
[deploy.md](deploy.md#https-port-and-the-caddy-front-end-grph-1635) and
[native-install.md](native-install.md#https-on-a-native-install).

## The three tiers

1. **Box certificate (default).** The box makes its own certificate authority and signs a
   certificate for the names on its allowlist. Free, works offline, but each device must trust
   the box (below).
2. **Bring your own / Let's Encrypt.** Add a public name to the allowlist (`POST /api/https/names`)
   or supply your own certificate; a publicly trusted certificate needs no per-device step.
3. **Graphban Cloud certificates.** Paid plans get a publicly trusted certificate for their
   hosted name, managed for you.

## Trusting the box per device

Download the public CA from `/api/https/ca.crt` (it never includes a private key), then install it:

- **macOS:** open the file, add it to Keychain Access (System), set it to *Always Trust*.
- **Windows:** `certutil -addstore -f Root graphban-box-ca.crt`, or double-click → Install → Trusted Root.
- **Linux:** copy to `/usr/local/share/ca-certificates/` and run `update-ca-certificates`.
- **iOS/Android:** install the profile/certificate, then enable it in the trust settings.

## The redirect rule

HTTP is redirected to HTTPS only when the API says so (`REDIRECT_CHECK_URI`). Until then both
work, and agent and CLI configs on `http://` keep working while http stays on. `gban doctor`
prints the HTTPS address when your server URL is http, and says "can't tell" if it cannot read
the status.

## The HTTPS port

HTTPS listens on `WEB_HTTPS_PORT` (default `8443`). Change it in `.env` and restart the stack;
update any bookmarked URL to match.

## Why push needs HTTPS

Browsers expose service workers and push only on HTTPS (or `localhost`). Push must be turned on
again after moving to https, because the subscription belongs to the old origin.
