#!/usr/bin/env bash
# Graphban — first run (AL-283 / PRD-14 D3).
#
# Brings the stack up and, on an instance with no users yet, provisions an operator,
# a project, and one agent credential — then prints the MCP client config.
#
# Why a script rather than something the agent does: issuing the first credential is an
# AUTHORITY gate ("are you allowed?"), and PRD-14's rule is that those stay human. An
# agent cannot bootstrap itself into existing without that rule collapsing. So the gate
# is satisfied OUT OF BAND — by an operator, on the box they already control — instead
# of being relaxed. Authority never moves; it just stops needing a browser.
#
# Safe to re-run: provisioning keys off "this instance has no users", the same signal
# seed() uses, so a second run changes nothing and re-prints what it can.

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE=${COMPOSE:-docker compose}
API_PORT=${API_PORT:-8000}
WEB_PORT=${WEB_PORT:-8080}
API_URL="http://localhost:${API_PORT}"
PROJECT_NAME=${PROJECT_NAME:-$(basename "$PWD")}
CONFIG_DIR="${HOME}/.graphban"
# NOT config.json — that file belongs to `graphban link`, which stores the CLOUD SYNC
# credential under the same `api_key` name. Writing the local MCP key there would both
# clobber an existing link and leave `graphban sync` pushing with the wrong credential.
# Found by the AL-286 acceptance walk.
CONFIG_FILE="${CONFIG_DIR}/mcp.json"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

say "Starting Graphban… (the web app will be at http://localhost:${WEB_PORT} - open this to continue setup)"
$COMPOSE up -d --build

say "Waiting for the API to become healthy…"
for _ in $(seq 1 90); do
  if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
if ! curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
  echo "The API did not come up. Try: $COMPOSE logs api" >&2
  exit 1
fi

# Provisioning runs INSIDE the container (that's where DATABASE_URL points) and prints
# JSON, because the credential has to reach the host — the MCP client runs out here.
say "Provisioning…"
RESULT=$($COMPOSE exec -T api graphban init --json --project-name "$PROJECT_NAME")

# The JSON goes in as an ARGUMENT, not on stdin — stdin is already carrying this script.
python3 - "$CONFIG_FILE" "$API_URL" "$WEB_PORT" "$RESULT" <<'PY'
import json, sys, pathlib

config_file, api_url, web_port = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.loads(sys.argv[4])
web_url = f"http://localhost:{web_port}"

if not data.get("provisioned"):
    print(f"\nAlready set up — {data.get('reason')}.")
    print(f"Open {web_url} to sign in.")
    if pathlib.Path(config_file).exists():
        cfg = json.loads(pathlib.Path(config_file).read_text())
        print(f"\nYour MCP config is at {config_file} (project {cfg.get('project')}).")
    else:
        # Keys are stored only as a hash, so there is nothing to recover. Say that
        # plainly rather than failing in a way that reads like a bug.
        print(f"\nNo local config at {config_file}. API keys are stored only as a hash "
              "and cannot be shown again — mint a new one in Settings → API Keys.")
    raise SystemExit(0)

path = pathlib.Path(config_file)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"project": data["project_id"], "api_key": data["api_key"]}, indent=2) + "\n")
path.chmod(0o600)

print(f"""
Graphban is running.

  Web        {web_url}
  Sign in    {data['email']}
  Password   {data['password']}

  Project    {data['project_name']}  (keys render as {data['project_tag']}-1, {data['project_tag']}-2, …)
  Memory     {data['memory_write_mode']} — the agent's notes publish on write so it can read
             them back. Nobody has vetted them: they're labelled "no review" in
             Memory review and can be undone there. Change it in project settings.

Add this to your MCP client (~/.claude.json, or .cursor/mcp.json):

  "graphban": {{
    "type": "http",
    "url": "{api_url}/api/mcp",
    "headers": {{ "X-API-Key": "{data['api_key']}" }}
  }}

Saved to {config_file} (chmod 600).
The password and key are shown ONCE — keys are stored only as a hash and cannot be
recovered. If you lose them, mint a new key in Settings → API Keys.
""")
PY
