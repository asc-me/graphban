#!/bin/sh
# Prove that nginx follows the backend to a NEW address without being restarted (GRPH-523).
#
# WHY THIS IS A SCRIPT AND NOT A PYTEST. The property is "nginx re-resolves DNS at request
# time", and nothing smaller than a real nginx talking to a real backend over real container
# DNS can observe it. A unit test would be reduced to grepping the config for the word
# `resolver`, which passes even if `proxy_pass` still holds a literal and nothing re-resolves
# — a spelling check wearing a behaviour test's clothes. So this needs Docker, takes ~90s,
# and is run by hand when the proxy config changes.
#
#   ./web/verify-upstream-reresolution.sh
#
# It runs the SAME scenario against the committed template and against the pre-fix one from
# git, and requires the old to FAIL and the new to PASS. A fix that cannot be shown to fail
# without it has not been shown to do anything.
#
# THE FIXTURE'S LOAD-BEARING PART is that the replacement backend gets a DIFFERENT address.
# The first version of this test removed the old container before starting the new one,
# Docker recycled the freed IP, and the pre-fix config "passed" — the cached address still
# happened to be right. The new backend is therefore started WHILE the old one still holds
# its IP, and the two are compared before anything is asserted.
set -eu

NET=gbverify
TMPL_NEW="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/nginx.conf.template"
TMPL_OLD=$(mktemp)
trap 'docker rm -f nginx-verify be-one be-two >/dev/null 2>&1 || true; docker network rm ${NET} >/dev/null 2>&1 || true; rm -f "${TMPL_OLD}" "${ECHO_PY}"' EXIT

git -C "$(dirname -- "${TMPL_NEW}")/.." show origin/main~1:web/nginx.conf.template > "${TMPL_OLD}" 2>/dev/null \
  || git -C "$(dirname -- "${TMPL_NEW}")/.." show HEAD~1:web/nginx.conf.template > "${TMPL_OLD}"

ECHO_PY=$(mktemp)
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

scenario() {  # $1 = template, $2 = label; echoes PASS or FAIL
    docker rm -f nginx-verify be-one be-two >/dev/null 2>&1 || true
    IP1=$(backend be-one A)
    docker run -d --name nginx-verify --network ${NET} -p 18099:80 \
        -e PORT=80 -e API_SCHEME=http -e API_UPSTREAM=upstream:8000 \
        -e NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
        -e 'NGINX_ENVSUBST_FILTER=^(PORT|API_SCHEME|API_UPSTREAM|NGINX_LOCAL_RESOLVERS)$' \
        -v "$1":/etc/nginx/templates/default.conf.template nginx:1.27-alpine >/dev/null
    sleep 4
    curl -sf --max-time 10 http://127.0.0.1:18099/health >/dev/null \
        || { echo "  ${2}: baseline never worked — the scenario proves nothing" >&2; return 3; }

    IP2=$(backend be-two B)          # started while be-one still holds IP1
    docker rm -f be-one >/dev/null 2>&1
    [ "${IP1}" != "${IP2}" ] || { echo "  ${2}: FIXTURE TOOTHLESS — B reused ${IP1}" >&2; return 3; }
    echo "  ${2}: A=${IP1} B=${IP2} (nginx NOT restarted)" >&2
    sleep 14
    if curl -sf --max-time 12 http://127.0.0.1:18099/health 2>/dev/null | grep -q '"who":"B"'; then
        echo "PASS"
    else
        echo "FAIL"
    fi
}

docker network rm ${NET} >/dev/null 2>&1 || true
docker network create ${NET} >/dev/null

echo "== pre-fix template (literal proxy_pass) =="
OLD=$(scenario "${TMPL_OLD}" "old" | tail -1)
echo "== committed template (resolver + variable) =="
NEW=$(scenario "${TMPL_NEW}" "new" | tail -1)

echo
echo "old=${OLD}  new=${NEW}"
[ "${OLD}" = "FAIL" ] || { echo "CONTROL BROKEN: the pre-fix config also followed the move, so this run does not show the fix is load-bearing. Most likely the replacement reused the address."; exit 1; }
[ "${NEW}" = "PASS" ] || { echo "REGRESSION: the committed config did not follow the backend."; exit 1; }
echo "OK — the fix is load-bearing: old config stops serving, new config follows."
