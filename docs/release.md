# Cutting a Graphban release

The proven loop: stamp a CalVer on `main`, publish a GitHub Release whose
asset is the packed tarball, then apply from Settings → Updates → Install.
Walked end to end on ubuntu-srv for `2026.09.4` (`3991116`) — api and web
agreed, db ok.

```bash
python3 scripts/graphban_release.py stamp      # writes the three version files
# commit, PR, merge to main
python3 scripts/graphban_release.py publish    # pack + GitHub Release
```

Then on the box: **Settings → This box → Updates → Check → Install**. Confirm
names the tag. This script does not apply.

Point Gitops **Release defined in** at `docs/release.md` so agents find this
file from `get_context`. Unmeasured is not this path.

## What a release is

Product cuts are **CalVer `YYYY.MM.N` + `git_sha`**. `/health.version` is the
CalVer; `git_sha` is the exact build. `0.1.0` is not a release. `main` is not
a version. FSL licenses each named version we make available.

A git tag is not a release. GitHub's **source zip is not a release** — no
prebuilt `web/dist`, maybe a `.env`, no `GIT_SHA`. Native Install fetches
`graphban-<tag>.tar.gz`. A Release with only the zip looks published and
cannot be applied. That was `2026.09.1`.

Fleet (`fleet/pyproject.toml`) stays `0.1.0` until that distribution has its
own cut. Do not stamp it here.

## Stamp

Writes, and only these:

- `backend/app/version.py`
- `backend/pyproject.toml`
- `web/package.json`

```bash
python3 scripts/graphban_release.py next       # 2026.09.5
python3 scripts/graphban_release.py stamp      # or stamp 2026.09.5
```

Commit on a branch from `origin/main`, open a PR, merge. Do not tag from the
stamp commit if more work should ride on the cut — `publish` tags
`origin/main` after merge.

No `v` prefix. Tags are `2026.09.5`, not `v2026.09.5`.

## Publish

After the stamp is on `origin/main`:

```bash
python3 scripts/graphban_release.py publish
python3 scripts/graphban_release.py publish --dry-run
```

Packs **that commit** from a detached worktree (same trap as `deploy.sh` —
cwd is not the cut), attaches `dist-release/graphban-<tag>.tar.gz` to a
GitHub Release marked latest, and refuses if the tarball is missing. A
Release that already exists without the tarball gets `gh release upload`,
which is how you repair a source-zip-only cut.

Does not merge, does not push `main`, does not apply to a box.

`/releases/latest` can lag a few seconds after create. Check → Install
seeing the previous tag is that race, not a missed publish. Wait and Check
again.

## Apply (the operator gate)

Check may be a timer later. **Apply is a person clicking Install.** Hosted
never offers it. An agent must not call `POST /api/platform/update-apply`.

| Topology | What Install does | `apply` is true when |
|---|---|---|
| Compose | host helper runs `deploy.sh --local --dir <compose> <tag>` | unix socket at `/run/graphban-apply/apply.sock` |
| Native | fetch tarball, start `graphban_host.py upgrade` | `/opt/graphban/current/backend` exists |
| Hosted | — | never |

No helper and no native tree is a **disabled** Install, not a green one. The
API container does not get a Docker socket.

### Compose helper (once per box)

The rsync target (`~/agentledger` on ubuntu-srv) has no `.git`, so `deploy.sh`
cannot fetch from there. A **clone** is what the helper listens from.

```bash
git clone git@github.com:asc-me/graphban.git ~/graphban-src
mkdir -p ~/.graphban-apply ~/.config/systemd/user
cp ~/graphban-src/scripts/graphban-compose-host.service \
  ~/.config/systemd/user/
# Edit ExecStart paths if this box is not ~ / graphban-src / agentledger.
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now graphban-compose-host.service
```

In the compose project's `.env` (the **server's**, never rsynced):

```
GRAPHBAN_APPLY_DIR=/home/YOU/.graphban-apply
```

Recreate the API container so it mounts that directory at
`/run/graphban-apply`. `apply` is true only when `apply.sock` is a unix
socket.

To run without systemd, same binary:

```bash
python3 ~/graphban-src/scripts/graphban_compose_host.py listen \
  --repo ~/graphban-src --dir ~/agentledger \
  --socket ~/.graphban-apply/apply.sock
```

JWT in the API is the operator gate. The helper trusts whoever can write the
socket — keep it `0600`, do not put it on a TCP port.

`deploy.sh` runs detached and appends its output to `deploy.log` beside the
socket (`~/.graphban-apply/deploy.log` here). Install acks before the rebuild
finishes either way; when the box does not come back on the new cut, that log
is where the build says why.

### Native

`/opt/graphban/current` is enough; no helper. Unpack is what `publish`
already attached. [Native install](native-install.md).

## Verify

After Install, both facts, not one:

```bash
curl -s http://<api>/health
# version is the CalVer you published, git_sha matches the tag, db ok

curl -s http://<web>/version.txt
# same sha as /health.git_sha
```

A version bump with last week's sha is a cached image, not a cut. A sha
without the new CalVer is a deploy of `main` that nobody stamped. Unknown is
not current.

## Traps (each of these has shipped)

- **Packing cwd.** Uncommitted files ride along. `publish` packs the resolved
  `origin/main` sha. `graphban_pack.py <tag>` does the same for a lone pack.
- **Source zip.** GitHub always attaches one. Install does not use it.
- **Tag without an asset.** Updates shows *available*; Install has nothing to
  fetch. `publish` refuses to create that Release.
- **Stamping fleet.** Separate distribution, still `0.1.0`.
- **Applying from the script / MCP.** Authority gate, same family as
  `start.sh` refusing hosted seed.
- **Hosted Install.** Cloud never applies. Promote-from-tag is a different
  path and is not this document.
- **Comparing sha to HEAD.** Verify against the tag you published, then
  `/health`.
- **Silent apply.** Install acks and detaches `deploy.sh`; through 2026.09.6
  the output was discarded, so a box stuck on the old cut said nothing about
  why. The helper logs it to `deploy.log` beside the socket now.

Hand steps, if the script is the thing you do not trust yet:

```bash
TAG=$(python3 scripts/graphban_release.py next)  # or the version already on main
python3 scripts/graphban_pack.py "$TAG"
gh release create "$TAG" "dist-release/graphban-${TAG}.tar.gz" \
  --title "$TAG" --target "$(git rev-parse origin/main)" --latest
```

That is what `publish` runs. Prefer the script — the tarball argument is the
load-bearing one.

Compose deploy without a named cut remains [deploy.md](deploy.md)
(`scripts/deploy.sh`). This document is the cut that Updates can see.
