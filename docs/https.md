# HTTPS

A self-hosted box serves HTTPS on its own port beside plain HTTP. Caddy (the `web` service)
terminates it; see [deploy.md](deploy.md#https-port-and-the-caddy-front-end-grph-1635) and
[native-install.md](native-install.md#https-on-a-native-install). Plain HTTP stays on, so
nothing that already points at the `http://` URL changes.

## The three tiers

1. **The box's own certificate (tier 1, free, the default).** On first start the box makes its
   own certificate authority and signs a server certificate for the names it is reached by
   (hostname, interface addresses, and local-only names or private addresses it is first seen
   at; at most 32). It renews and re-issues that certificate itself. Each device must trust the
   box once (below). Adding a name in Settings → This box → HTTPS only adds it to this
   certificate; it does not get you a publicly trusted one.
2. **Bring your own, or Let's Encrypt (tier 2, free).** Upload a certificate chain and private
   key (PEM), for example from your company CA, or let a box with a public, internet-reachable
   domain get and renew a Let's Encrypt certificate. A certificate your devices already trust
   needs no per-device step. *Not shipped yet:* this tier lands with its own item; today the
   box serves its own certificate only.
3. **Graphban Cloud certificates (tier 3, paid plans).** A box linked to an org on a paid plan
   gets a publicly trusted certificate for `<box>.<org>.gban.cloud`. The key is generated on the
   box and never leaves it. If the link lapses, the box falls back to tier 1 and says why.
   *Not shipped yet.*

## Trusting the box on each device (tier 1)

Settings → This box → HTTPS has the same steps and a check that tells you when this browser
trusts the box. The certificate file is `/api/https/ca.crt` (`graphban-box-ca.crt`); it is the
public CA certificate only, never a private key.

- **macOS:** open the file; Keychain Access opens. Add it to the login keychain, double-click
  "Graphban box CA", open Trust, set "When using this certificate" to *Always Trust*, and enter
  your password to confirm.
- **Windows:** open the file, choose Install Certificate, pick Current User, and place it in
  "Trusted Root Certification Authorities".
- **iOS:** allow the profile to download, install it from Settings → Profile Downloaded, then turn
  on full trust in Settings → General → About → Certificate Trust Settings.
- **Android:** in Settings, search for "CA certificate", choose Install anyway if asked, and pick
  the file.
- **Linux:** copy it to `/usr/local/share/ca-certificates/` and run
  `sudo update-ca-certificates`. Firefox keeps its own store under Settings → Certificates.

A device that cannot install the authority (MDM, enterprise profiles) keeps using the plain
`http://` address. In-app alerts still work there; only browser push is missing. Tier 2 or 3
removes the per-device step.

## The redirect rule

- **With the box's own certificate (tier 1), HTTP is never redirected to HTTPS.** A device that
  has not trusted the box would hit a full-page warning. There is no redirect switch in this
  tier; the settings page says it arrives with a public certificate.
- **With a public certificate (tier 2 or 3),** HTTP redirects to HTTPS by default, and the
  operator can turn the redirect off in Settings. For an uploaded certificate the redirect goes
  to the box address the certificate covers, never to the Host header.

Under the hood Caddy asks the API before redirecting (`REDIRECT_CHECK_URI`, default
`/api/https/redirect-check`); empty means never.

## Tier 2: bring your own certificate, and Let's Encrypt

Upload a certificate chain and its key (PEM) and name the **box address**. Exactly three things
refuse it, each reported on its own: the key does not match the certificate, the certificate does
not cover the box address, the certificate has expired. An address that does not resolve to this
box yet, and a wildcard certificate, are warnings, not refusals. It is served straight away.

With 30 days or fewer left the page says *expiring soon*. At expiry the box returns to its own
certificate, the redirect turns off, and the page says *your certificate expired on …*. **Stop
using** deletes the uploaded files and the key and does the same. Let's Encrypt (a public domain)
shows pending, failed (with the reason; the current certificate keeps serving) or active; it
renews itself and has no expiring-soon state.

While HTTP stays on, agent and CLI configs that point at `http://` keep working. `gban doctor`
reports the box's HTTPS address when your server URL is `http://` and the box reports one, and
says "can't tell" when it cannot read it, never "no HTTPS".

## The HTTPS port

HTTPS listens on `8443` inside the `web` container; Compose publishes it on
`${WEB_HTTPS_PORT:-8443}`. To change it, set `WEB_HTTPS_PORT` in `.env` and run
`docker compose up -d web`. Update bookmarks to the new port; the HTTP port (`WEB_PORT`) is
unaffected.

## Why push needs HTTPS

Browsers expose service workers and push only in a secure context: HTTPS, or `localhost`. A box
reached as `http://ubuntu-srv:8080` is not one, so the bell cannot offer browser push there.

Moving from `http://` to `https://` changes the origin, and push subscriptions and browser
storage belong to an origin. **Turn push on again after moving to HTTPS**, from the bell on the
HTTPS address. The box's VAPID keys do not change.
