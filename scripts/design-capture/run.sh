#!/usr/bin/env bash
# Capture the running web app into graphban-local Claude Design cards. See README.md.
#
#   scripts/design-capture/run.sh          # everything: servers up, capture, build, render-check, servers down
#   KEEP_SERVERS=1 scripts/design-capture/run.sh
#
# Output: $DC_WORK/bundle/<card>/index.html (default $TMPDIR/gb-design-capture). Pushing is a separate
# step (DesignSync from a Claude session after /design-login); this script never touches the project.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${GB_REPO:-$(cd "$HERE/../.." && pwd)}"  # GB_REPO: a checkout that has backend/.venv + web/node_modules
export DC_WORK="${DC_WORK:-${TMPDIR:-/tmp}/gb-design-capture}"
PY="$REPO/backend/.venv/bin/python"
SECRET="$(python3 -c 'print("design-capture-" + "x" * 32)')"
mkdir -p "$DC_WORK"; rm -rf "$DC_WORK"/out-* "$DC_WORK/bundle" "$DC_WORK"/*.db

[ -d "$HERE/node_modules/playwright" ] || (cd "$HERE" && npm install --no-audit --no-fund && npx playwright install chromium)

pids=()
cleanup() { [ -n "${KEEP_SERVERS:-}" ] || kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT

api() {  # port db extra-env…
  local port=$1 db=$2; shift 2
  # exec: the recorded pid must be the server itself, or cleanup kills only a subshell and leaks it
  (cd "$REPO/backend" && exec env DATABASE_URL="sqlite:///$DC_WORK/$db" SEED_ON_START=true JWT_SECRET="$SECRET" "$@" \
    "$PY" -m uvicorn app.main:app --port "$port" >"$DC_WORK/api-$port.log" 2>&1) & pids+=($!)
}
web() {  # port api-port
  (cd "$REPO/web" && exec env VITE_API_PROXY="http://localhost:$2" ./node_modules/.bin/vite --port "$1" --strictPort \
    >"$DC_WORK/vite-$1.log" 2>&1) & pids+=($!)
}
wait_for() { for _ in $(seq 1 60); do curl -fsS "$1" >/dev/null 2>&1 && return; sleep 1; done; echo "timeout: $1" >&2; exit 1; }

api 8766 selfhost.db
api 8767 hosted.db HOSTED_MODE=true PLATFORM_ADMIN_EMAILS=alex@ascme-labs.com
web 5199 8766
web 5198 8767
wait_for http://localhost:8766/health; wait_for http://localhost:8767/health
wait_for http://localhost:5199/; wait_for http://localhost:5198/

# Hosted first run: the owner onboards through the UI; sam stops before creating an org, rui before a project.
node "$HERE/onboard.mjs" http://localhost:5198
for u in "Sam Park:sampark:sam@example.com" "Rui Tan:ruitan:rui@example.com"; do
  IFS=: read -r n h e <<<"$u"
  curl -fsS -X POST localhost:8767/api/auth/register -H 'content-type: application/json' \
    -d "{\"name\":\"$n\",\"handle\":\"$h\",\"email\":\"$e\",\"password\":\"graphban1\"}" >/dev/null
done
TOK=$(curl -fsS -X POST localhost:8767/api/auth/login -H 'content-type: application/json' \
  -d '{"email":"rui@example.com","password":"graphban1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
curl -fsS -X POST localhost:8767/api/orgs -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
  -d '{"name":"Tan Studio"}' >/dev/null

python3 "$HERE/jobs.py"
cd "$DC_WORK"
node "$HERE/capture.mjs" jobs-self.json out-self
node "$HERE/capture.mjs" jobs-hosted.json out-hosted
node "$HERE/capture.mjs" jobs-extra.json out-extra
node "$HERE/capture.mjs" jobs-x.json out-x
node "$HERE/capture.mjs" jobs-unauth.json out-unauth
node "$HERE/capture.mjs" jobs-onboard-org.json out-unauth
node "$HERE/capture.mjs" jobs-onboard-project.json out-unauth

python3 "$HERE/build.py"
node "$HERE/rcheck.mjs"
echo "cards: $DC_WORK/bundle   render check: $DC_WORK/rc/*.png"
