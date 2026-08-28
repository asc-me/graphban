#!/usr/bin/env bash
#
# Deploy a COMMIT to ubuntu-srv, not a working directory.
#
# `rsync` ships whatever is on disk. For as long as deploys ran from the main checkout that
# was fine — until agents started working in it. On 2026-08-20 a deploy was one command away
# from shipping 17 files and 281 lines of a fleet agent's uncommitted work to the live box,
# stamped with a `git_sha` that described none of it. Release identity would have been a lie,
# and the runbook's whole verification step is that identity.
#
# So this syncs from a DETACHED worktree pinned to the exact commit. Nothing can be mid-edit
# there, because nobody works there. The two failure modes it removes:
#
#   - an agent's in-flight changes riding along with a deploy
#   - a deploy disturbing an agent, since we no longer touch the shared checkout at all
#
# It also removes the `GIT_SHA` trap the runbook documents at length: the sha comes from the
# worktree itself rather than from a variable a human has to remember to export twice.
#
# WHERE it deploys is configuration, not a constant (GRPH-573). This script was hardcoded to
# one host, one directory, one pair of ports and one Postgres role — so the runbook the product
# recommends worked for exactly one person, and everybody else deployed by hand. Deploying by
# hand is what the two incidents above are.
#
# Ports and credentials are READ FROM THE TARGET'S `.env`, which is where they are already
# defined, rather than assumed. The old script asserted `localhost:8001`, which is true only of
# this repository's box because that box had `:8000` busy — on a default install it checked a
# port nothing was serving and would have reported a healthy deploy as broken.
#
# Usage:
#   scripts/deploy.sh                       # origin/main to $GRAPHBAN_DEPLOY_HOST
#   scripts/deploy.sh v1.2.3                # any ref
#   scripts/deploy.sh --host box.local      # any host
#   scripts/deploy.sh --local               # this machine, no ssh
#   GRAPHBAN_DEPLOY_HOST=box scripts/deploy.sh
set -euo pipefail

REMOTE="${GRAPHBAN_DEPLOY_HOST:-ubuntu-srv}"
TARGET_DIR="${GRAPHBAN_DEPLOY_DIR:-~/agentledger/}"
LOCAL=""
REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host) REMOTE="${2:?--host needs a hostname}"; shift 2 ;;
    --dir)  TARGET_DIR="${2:?--dir needs a path}"; shift 2 ;;
    # A local deploy still ships a COMMIT to a target directory rather than running compose in
    # your checkout. Same guarantee, one fewer hop — the worktree is the point, not the ssh.
    --local) LOCAL=1; REMOTE="(local)"; shift ;;
    -h|--help) sed -n '20,28p' "$0"; exit 0 ;;
    *) REF="$1"; shift ;;
  esac
done
REF="${REF:-origin/main}"
TARGET_DIR="${TARGET_DIR/#\~/$HOME}"

# One place that knows whether there is an ssh hop, so every later step reads the same for a
# remote and a local deploy and neither can drift from the other.
on_target() {
  if [ -n "$LOCAL" ]; then bash -lc "$1"; else ssh "$REMOTE" "$1"; fi
}

# A value the target already declares, or the compose default. Never a guess: a wrong port here
# fails a good deploy, which teaches people to ignore the verification.
target_env() {
  local key="$1" default="$2" val=""
  val="$(on_target "grep -sE '^${key}=' '${TARGET_DIR%/}/.env' | tail -1 | cut -d= -f2-" 2>/dev/null || true)"
  val="$(printf '%s' "$val" | tr -d '[:space:]\"'"'"'')"
  printf '%s' "${val:-$default}"
}

WORKTREE="$(cd "$(dirname "$0")/.." && pwd)/../agentledger-wt-deploy"

cd "$(dirname "$0")/.."
git fetch -q origin

if [ ! -d "$WORKTREE" ]; then
  echo "==> creating the deploy worktree (one time)"
  git worktree add --detach "$WORKTREE" "$REF"
fi

echo "==> pinning the deploy worktree to $REF"
git -C "$WORKTREE" fetch -q origin
git -C "$WORKTREE" checkout -q --detach "$REF"
# A worktree nobody works in should never have anything to clean. If it does, something has
# gone wrong that a deploy must not paper over — say so rather than shipping it.
if [ -n "$(git -C "$WORKTREE" status --porcelain)" ]; then
  echo "!! the deploy worktree is dirty — refusing:" >&2
  git -C "$WORKTREE" status --short >&2
  exit 1
fi

GIT_SHA="$(git -C "$WORKTREE" rev-parse --short HEAD)"
echo "==> shipping $GIT_SHA ($(git -C "$WORKTREE" log -1 --format=%s | cut -c1-60))"

# --exclude .env: there is no local one, so a bare --delete would DELETE the server's, and
#   compose then reverts to default ports while the persisted volume keeps the old password.
# --exclude sync: root-owned, container-written on the server; rsync fails exit 23 without it.
if [ -n "$LOCAL" ]; then
  mkdir -p "$TARGET_DIR"
  rsync -a --delete \
    --exclude .git --exclude .env --exclude sync \
    --exclude node_modules --exclude dist --exclude __pycache__ \
    --exclude .venv --exclude .serena \
    "$WORKTREE/" "$TARGET_DIR"
else
  rsync -az --delete \
    --exclude .git --exclude .env --exclude sync \
    --exclude node_modules --exclude dist --exclude __pycache__ \
    --exclude .venv --exclude .serena \
    "$WORKTREE/" "$REMOTE:$TARGET_DIR"
fi

echo "==> building on $REMOTE"
on_target "cd '$TARGET_DIR' && GIT_SHA=$GIT_SHA docker compose up -d --build" >/dev/null

# Read AFTER the sync, so a first deploy to a fresh box sees the .env it just arrived beside.
# Defaults are compose's own, so an install that sets nothing still verifies correctly.
API_PORT="$(target_env API_PORT 8000)"
WEB_PORT="$(target_env WEB_PORT 8080)"
PG_USER="$(target_env POSTGRES_USER agentledger)"
PG_DB="$(target_env POSTGRES_DB agentledger)"

echo "==> verifying release identity (api :$API_PORT, web :$WEB_PORT)"
for _ in $(seq 1 30); do
  LIVE="$(on_target "curl -s http://localhost:$API_PORT/health" 2>/dev/null || true)"
  echo "$LIVE" | grep -q '"status":"ok"' && break
  sleep 2
done

LIVE_SHA="$(echo "$LIVE" | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p')"
WEB_SHA="$(on_target "curl -s http://localhost:$WEB_PORT/version.txt" 2>/dev/null | tr -d '[:space:]')"
MIGRATION="$(on_target "cd '$TARGET_DIR' && docker compose exec -T db \
  psql -U '$PG_USER' -d '$PG_DB' -tAc 'select version_num from alembic_version'" | tr -d '[:space:]')"

echo "    api      $LIVE_SHA"
echo "    web      $WEB_SHA"
echo "    alembic  $MIGRATION"
echo "$LIVE" | grep -q '"db":"ok"' || { echo "!! db is not ok" >&2; exit 1; }

# The check the runbook exists for. A deploy that builds cleanly and serves the PREVIOUS
# revision looks identical to a successful one from the outside.
[ "$LIVE_SHA" = "$GIT_SHA" ] || { echo "!! api serves $LIVE_SHA, expected $GIT_SHA" >&2; exit 1; }
[ "$WEB_SHA" = "$GIT_SHA" ] || { echo "!! web serves $WEB_SHA, expected $GIT_SHA" >&2; exit 1; }
echo "==> $GIT_SHA is live"
