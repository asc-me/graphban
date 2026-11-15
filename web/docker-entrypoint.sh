#!/bin/sh
# Front-end entrypoint (GRPH-1635): choose the snippets, then run Caddy with a watcher that
# reloads it, through the admin API and without a restart, when the box certificate changes.
set -eu

CONF=/etc/caddy
export HTTPS_SNIPPET="${CONF}/https.on.caddy"
case "$(printf %s "${HOSTED_MODE:-}" | tr 'A-Z' 'a-z')" in
  1|true|yes|on) export HTTPS_SNIPPET="${CONF}/https.off.caddy" ;;
esac
if [ -n "${REDIRECT_CHECK_URI:-}" ] && [ "${HTTPS_SNIPPET}" = "${CONF}/https.on.caddy" ]; then
  export REDIRECT_SNIPPET="${CONF}/redirect.on.caddy"
else
  export REDIRECT_SNIPPET="${CONF}/redirect.off.caddy"
fi

TLS_DIR=/etc/graphban/tls
mkdir -p "${TLS_DIR}"
# Caddy refuses to start an https site whose certificate files are absent, and a fresh box has
# none until the API's CA writes them. Serve HTTP only until they exist.
if [ "${HTTPS_SNIPPET}" = "${CONF}/https.on.caddy" ] && { [ ! -s "${TLS_DIR}/box.crt" ] || [ ! -s "${TLS_DIR}/box.key" ]; }; then
  export HTTPS_SNIPPET="${CONF}/https.off.caddy"
  WAIT_FOR_CERT=1
fi

caddy run --config "${CONF}/Caddyfile" --adapter caddyfile &
CADDY=$!
trap 'kill $CADDY 2>/dev/null' TERM INT

stamp() { cat "${TLS_DIR}/box.crt" "${TLS_DIR}/box.key" 2>/dev/null | cksum; }

if [ "${HOSTED_MODE:-}" != "" ] && [ "${HTTPS_SNIPPET}" = "${CONF}/https.off.caddy" ] && [ -z "${WAIT_FOR_CERT:-}" ]; then
  wait $CADDY
  exit $?
fi

(
  last=$(stamp)
  while kill -0 $CADDY 2>/dev/null; do
    sleep 5
    now=$(stamp)
    if [ "$now" != "$last" ] && [ -s "${TLS_DIR}/box.crt" ] && [ -s "${TLS_DIR}/box.key" ]; then
      # A reload re-reads the Caddyfile: with the files present the https site is now in it.
      export HTTPS_SNIPPET="${CONF}/https.on.caddy"
      case "$(printf %s "${HOSTED_MODE:-}" | tr 'A-Z' 'a-z')" in 1|true|yes|on) export HTTPS_SNIPPET="${CONF}/https.off.caddy" ;; esac
      if caddy reload --config "${CONF}/Caddyfile" --adapter caddyfile --address localhost:2019 --force; then
        last=$now
      fi
    fi
  done
) &

wait $CADDY
