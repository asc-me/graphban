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
# Usage:  scripts/deploy.sh [ref]        # default: origin/main
set -euo pipefail

REF="${1:-origin/main}"
REMOTE="ubuntu-srv"
REMOTE_DIR="~/agentledger/"
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
rsync -az --delete \
  --exclude .git --exclude .env --exclude sync \
  --exclude node_modules --exclude dist --exclude __pycache__ \
  --exclude .venv --exclude .serena \
  "$WORKTREE/" "$REMOTE:$REMOTE_DIR"

echo "==> building on $REMOTE"
ssh "$REMOTE" "cd ~/agentledger && GIT_SHA=$GIT_SHA docker compose up -d --build" >/dev/null

echo "==> verifying release identity"
for _ in $(seq 1 30); do
  LIVE="$(ssh "$REMOTE" 'curl -s http://localhost:8001/health' 2>/dev/null || true)"
  echo "$LIVE" | grep -q '"status":"ok"' && break
  sleep 2
done

LIVE_SHA="$(echo "$LIVE" | sed -n 's/.*"git_sha":"\([^"]*\)".*/\1/p')"
WEB_SHA="$(ssh "$REMOTE" 'curl -s http://localhost:8080/version.txt' 2>/dev/null | tr -d '[:space:]')"
MIGRATION="$(ssh "$REMOTE" 'cd ~/agentledger && docker compose exec -T db \
  psql -U agentledger -d agentledger -tAc "select version_num from alembic_version"' | tr -d '[:space:]')"

echo "    api      $LIVE_SHA"
echo "    web      $WEB_SHA"
echo "    alembic  $MIGRATION"
echo "$LIVE" | grep -q '"db":"ok"' || { echo "!! db is not ok" >&2; exit 1; }

# The check the runbook exists for. A deploy that builds cleanly and serves the PREVIOUS
# revision looks identical to a successful one from the outside.
[ "$LIVE_SHA" = "$GIT_SHA" ] || { echo "!! api serves $LIVE_SHA, expected $GIT_SHA" >&2; exit 1; }
[ "$WEB_SHA" = "$GIT_SHA" ] || { echo "!! web serves $WEB_SHA, expected $GIT_SHA" >&2; exit 1; }
echo "==> $GIT_SHA is live"
