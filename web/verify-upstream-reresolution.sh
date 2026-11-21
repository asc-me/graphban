#!/bin/sh
# Prove that the web image follows the backend to a NEW address without being restarted
# (GRPH-523, kept for Caddy in GRPH-1635).
#
# The property is "the proxy re-resolves DNS at request time". Nothing smaller than the real
# image talking to a real backend over real container DNS can observe it, so this needs Docker
# and takes about a minute. Run it by hand when web/Caddyfile or web/Dockerfile changes:
#
#   ./web/verify-upstream-reresolution.sh
#
# The fixture's load-bearing part: the replacement backend gets a DIFFERENT address. It is
# started WHILE the old one still holds its IP, because Docker recycles a freed IP and a run
# that removed the old container first would pass with a cached address. The check also asserts
# the request path arrives unchanged (`/health`, `/api/items?x=1`): a path rewrite still follows
# the move, so who=B alone would not catch it.
set -eu

NET=gbverify
IMG=gbverify-web
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ECHO_PY=$(mktemp)
trap 'docker rm -f web-verify be-one be-two >/dev/null 2>&1 || true; docker network rm ${NET} >/dev/null 2>&1 || true; rm -f "${ECHO_PY}"' EXIT

cat > "${ECHO_PY}" <<'PY'
import http.server, os, socketserver
WHO = os.environ.get("WHO", "?")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f'{{"who":"{WHO}","uri":"{self.path}"}}'.encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(("0.0.0.0", 8000), H).serve_forever()
PY

backend() {  # $1 = name, $2 = WHO
    docker run -d --name "$1" --network ${NET} --network-alias upstream -e WHO="$2" \
        -v "${ECHO_PY}":/echo.py python:3.12-alpine python /echo.py >/dev/null
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"
}

docker network rm ${NET} >/dev/null 2>&1 || true
docker network create ${NET} >/dev/null
docker build -q -t ${IMG} "${HERE}" >/dev/null

IP1=$(backend be-one A)
docker run -d --name web-verify --network ${NET} -p 18099:80 \
    -e API_SCHEME=http -e API_UPSTREAM=upstream:8000 ${IMG} >/dev/null
sleep 4
curl -sf --max-time 10 http://127.0.0.1:18099/health >/dev/null \
    || { echo "baseline never worked — the scenario proves nothing" >&2; exit 3; }

IP2=$(backend be-two B)          # started while be-one still holds IP1
docker rm -f be-one >/dev/null 2>&1
[ "${IP1}" != "${IP2}" ] || { echo "FIXTURE TOOTHLESS — B reused ${IP1}" >&2; exit 3; }
echo "A=${IP1} B=${IP2} (web NOT restarted)" >&2
sleep 5
health=$(curl -sf --max-time 12 http://127.0.0.1:18099/health 2>/dev/null || true)
items=$(curl -sf --max-time 12 "http://127.0.0.1:18099/api/items?x=1" 2>/dev/null || true)
echo "$health" | grep -q '"who":"B"' \
    && echo "$health" | grep -q '"uri":"/health"' \
    && echo "$items" | grep -q '"who":"B"' \
    && echo "$items" | grep -q '"uri":"/api/items?x=1"' \
    || { echo "REGRESSION: the web image did not follow the backend to its new address."; exit 1; }
echo "OK — the web image follows the backend to its new address, path unchanged."
