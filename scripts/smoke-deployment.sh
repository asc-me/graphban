#!/bin/sh
# Smoke-test a deployed Graphban stack from the outside (GRPH-36).
#
#   scripts/smoke-deployment.sh https://app.example.com
#   scripts/smoke-deployment.sh                       # defaults to the compose stack
#
# WHY A SCRIPT AND NOT A MANUAL CHECKLIST. The item this implements listed the checks as
# things to do once by hand. A checklist performed once tells you about one afternoon; the
# interesting question is whether the stack is still whole after the NEXT deploy, and nobody
# re-performs a prose checklist. So each item is a command with an expected result, and the
# script exits non-zero when one fails.
#
# WHAT IT DELIBERATELY DOES NOT DO: sign up, create an account, or set a password. Those need
# a human, and an automated account-creator pointed at production is a liability rather than a
# test. The write path is covered instead by exercising the MCP tools with an API key the
# operator already has — see WRITE CHECKS below, which are opt-in for the same reason.
#
# Exit codes: 0 all checks passed · 1 a check failed · 2 the target was unreachable.
set -eu

BASE="${1:-http://localhost:8000}"
WEB="${SMOKE_WEB_URL:-${BASE}}"
PASS=0
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; return 0; }
# Counted as NEITHER pass nor fail. A check that cannot run is not a check that passed, and
# folding it into either total is how a smoke report starts overstating what it saw.
skip() { printf '  \033[33m--\033[0m   %s\n' "$1"; }

# `curl -s -o body -w code` rather than `curl -f`, because a check that only knows "not 2xx"
# cannot tell 404 from 500, and those mean opposite things on most of these routes.
req() { # method url [extra curl args...]  -> echoes status; body in $BODY_FILE
    _m=$1; _u=$2; shift 2
    curl -s -X "$_m" -o "$BODY_FILE" -w '%{http_code}' --max-time 20 "$@" "$_u" 2>/dev/null || echo 000
}

BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT

say "smoke: ${BASE}  (web: ${WEB})"
say ""

# ---- 1. is anything there at all -------------------------------------------------------
say "reachability"
code=$(req GET "${BASE}/health")
[ "$code" = "000" ] && { bad "GET /health" "no response — wrong URL, or the service is down"; say ""; say "unreachable; stopping"; exit 2; }
[ "$code" = "200" ] && ok "GET /health -> 200" || bad "GET /health -> ${code}" "expected 200"

# Release identity, so a "successful" deploy that served the OLD image is visible. Railway
# reports SUCCESS for a deploy whose container is serving stale code; this is how you tell.
sha=$(sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p' "$BODY_FILE")
db=$(sed -n 's/.*"db":"\([^"]*\)".*/\1/p' "$BODY_FILE")
# `unknown` is a SENTINEL, not a revision (GRPH-426). The API returns it deliberately, so
# that an instance which could not find out says so instead of answering blank — and a
# non-empty test then reads that admission as a pass. This check exists to make a stale or
# unidentifiable build visible, so the one value that means "I cannot tell you" must fail it.
case "${sha}" in
    "")        bad "no git_sha in /health" "cannot tell which build is serving" ;;
    unknown)   bad "git_sha=unknown" "the instance cannot state its revision: no GIT_SHA was baked in at build time and the platform supplied none. A deploy cannot be verified against origin/main, so 'is the fix live?' has no answer" ;;
    *)         ok "release identity: git_sha=${sha}" ;;
esac
[ "$db" = "ok" ] && ok "database reachable: db=ok" || bad "db=${db:-missing}" "the API is up but its database is not"

# ---- 2. pgvector, which this script CANNOT confirm from outside ---------------------------
say ""
say "pgvector"
# NOT COUNTED AS A PASS, deliberately.
#
# The tempting check is "db=ok, therefore migration 0001 ran, therefore the vector extension
# exists". That is sound on Postgres and vacuous on SQLite, which uses `create_all` and never
# runs a migration at all — so the first version of this printed a green pgvector line for a
# SQLite box, which is the exact false pass this whole script exists to avoid. From outside
# there is no signal that distinguishes them: `/health` reports `db` but not the engine, and
# it is unauthenticated, so it should not start advertising the stack either.
#
# So it says what it knows and points at the check that settles it.
skip "not verifiable from outside this process"
say "       On Postgres, a 200 from /health does imply migration 0001 ran and the extension"
say "       exists — a missing one kills startup. But this script cannot tell Postgres from"
say "       SQLite, and on SQLite no migration runs at all, so a green line here would be"
say "       meaningless. Settle it directly (see docs/deploy-railway.md):"
say "         railway run --service postgres psql \\"
say "           -c \"SELECT 1 FROM pg_available_extensions WHERE name = 'vector'\""

# ---- 3. the embed path, cross-origin ------------------------------------------------------
say ""
say "roadmap embed (iframe from another origin)"
# These headers come from nginx (web service), not the API. On a real deployment the public
# origin IS the web service, which proxies /api and /health through — so the default is right.
# Pointed straight at the API, though, every one of them would "fail" for the wrong reason:
# there is no nginx in that path to set them. Detect it and skip rather than report red.
hdrs=$(curl -s -D - -o /dev/null --max-time 20 "${WEB}/" 2>/dev/null || true)
root_type=$(printf '%s' "$hdrs" | tr -d '\r' | sed -n 's/^[Cc]ontent-[Tt]ype: *//p' | head -1)
case "$root_type" in
    text/html*) ;;  # the SPA — nginx is in front, headers are meaningful
    *)
        skip "skipped: ${WEB}/ served '${root_type:-nothing}', not the SPA"
        say "       These are nginx headers. Point at the public web origin, or set"
        say "       SMOKE_WEB_URL=https://<web-domain> when the API is addressed directly."
        hdrs="" ;;
esac
if [ -z "$hdrs" ]; then
    :
elif printf '%s' "$hdrs" | grep -qi "content-security-policy.*frame-ancestors"; then
    if printf '%s' "$hdrs" | grep -i "content-security-policy" | grep -q "frame-ancestors 'self' https:"; then
        ok "CSP allows the embed: frame-ancestors 'self' https:"
    else
        bad "CSP frame-ancestors is not the embed policy" "$(printf '%s' "$hdrs" | grep -i content-security-policy | head -1)"
    fi
else
    bad "no CSP frame-ancestors header" "the embed page's framing is unspecified"
fi
if [ -n "$hdrs" ]; then
    printf '%s' "$hdrs" | grep -qi "x-frame-options" \
        && bad "X-Frame-Options is set" "it overrides frame-ancestors in older browsers and blocks the embed" \
        || ok "no X-Frame-Options (it would override frame-ancestors)"
    printf '%s' "$hdrs" | grep -qi "strict-transport-security" \
        && ok "HSTS present" \
        || bad "no HSTS header" "expected on a TLS deployment (absent over plain http locally)"
fi

# ---- 4. the public surface is bounded ------------------------------------------------------
say ""
say "public surface"
code=$(req GET "${BASE}/api/public/roadmap")
# 429 is a PASS here, not a failure. Found by running this script twice in a row: the first
# run exhausts the very limit the next check exists to prove, so the second run's opening
# probe came back 429 and reported the endpoint broken. A smoke test that fails when run
# twice teaches operators to distrust it, which costs more than the check is worth.
case "$code" in
    404) ok "public roadmap is 404 without a share token (unprobeable)" ;;
    200) ok "public roadmap served (a project has opted in)" ;;
    429) ok "public roadmap is already rate limited (a recent run, or real traffic)" ;;
    *)   bad "public roadmap -> ${code}" "expected 404 (no token), 200 (opted in), or 429" ;;
esac

# The limit that GRPH-32 added. Unbounded, this is a full roadmap query per request for
# anyone holding a share link.
i=0; limited=no
while [ $i -lt 70 ]; do
    c=$(req GET "${BASE}/api/public/roadmap")
    [ "$c" = "429" ] && { limited=yes; break; }
    i=$((i+1))
done
[ "$limited" = yes ] && ok "public roadmap is rate limited (429 after ${i} requests)" \
    || bad "70 requests, never a 429" "the public endpoint is unbounded, or the limit is far above 70"

# ---- 5. MCP is reachable and refuses anonymous callers ------------------------------------
say ""
say "MCP endpoint"
code=$(req POST "${BASE}/api/mcp" -H 'Content-Type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
case "$code" in
    401|403) ok "POST /api/mcp -> ${code} without a key (refuses anonymous)" ;;
    404)     bad "POST /api/mcp -> 404" "the MCP route is not mounted on this deployment" ;;
    200)     bad "POST /api/mcp -> 200 without a key" "the MCP surface is answering unauthenticated callers" ;;
    *)       bad "POST /api/mcp -> ${code}" "expected 401/403" ;;
esac

# ---- WRITE CHECKS (opt-in) ----------------------------------------------------------------
# Off by default: they create real rows. Pointing them at production writes to production.
if [ -n "${GRAPHBAN_API_KEY:-}" ]; then
    say ""
    say "write path (API key supplied)"
    title="smoke $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    code=$(req POST "${BASE}/api/mcp" -H 'Content-Type: application/json' \
        -H "Authorization: Bearer ${GRAPHBAN_API_KEY}" \
        --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"create_item\",\"arguments\":{\"title\":\"${title}\"}}}")
    if [ "$code" = "200" ] && ! grep -q '"error"' "$BODY_FILE"; then
        ok "create_item accepted"
        code=$(req POST "${BASE}/api/mcp" -H 'Content-Type: application/json' \
            -H "Authorization: Bearer ${GRAPHBAN_API_KEY}" \
            --data "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"search_items\",\"arguments\":{\"query\":\"smoke\"}}}")
        [ "$code" = "200" ] && grep -q "smoke" "$BODY_FILE" \
            && ok "search_items found it (the embedding/index path works end to end)" \
            || bad "search_items did not return the item just created" "status ${code}"
    else
        bad "create_item -> ${code}" "$(head -c 200 "$BODY_FILE")"
    fi
else
    say ""
    say "write path: SKIPPED (set GRAPHBAN_API_KEY to exercise create_item/search_items)"
    say "  these create real rows, so they are opt-in rather than default."
fi

say ""
say "----------------------------------------"
say "passed ${PASS}, failed ${FAIL}"
[ "$FAIL" -eq 0 ] || exit 1
