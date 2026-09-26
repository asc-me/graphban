# design-capture

Keeps the **graphban-local** Claude Design project in step with the web app. It captures the app as it
actually renders; it does not re-describe it by hand. Each card holds the real DOM plus the compiled Tailwind
of one route, with scripts removed. Variants are stacked in 1440×900 frames.

A hand-transcribed generator was tried first. It drifted from `index.css` within weeks and was lost with
the session that made it. This one reads the app itself, so a token or copy change reaches the cards on the
next run.

## Run

```sh
scripts/design-capture/run.sh
# cards → $DC_WORK/bundle (default $TMPDIR/gb-design-capture), render check → $DC_WORK/rc/*.png
```

`run.sh` does the following:

1. Starts two seeded SQLite backends: self-host on :8766 and `HOSTED_MODE=true` on :8767. The seeded owner
   is a platform admin, so the operator console renders.
2. Starts two Vite dev servers against them (:5199, :5198).
3. Onboards the hosted owner through the real UI (org "Ascme Labs", project GRPH). It also makes two users
   who stop part-way through onboarding, so those screens can be captured.
4. Runs `capture.mjs` over the job files that `jobs.py` writes, then `build.py`, then `rcheck.mjs`.
   The render check exits non-zero on a blank frame or a failed request.

It needs `backend/.venv` and `web/node_modules`. In a worktree without them, point `GB_REPO` at a checkout
that has them. Don't `npm install` in `web/`: it is pnpm-locked.

## Push

This is not scripted: DesignSync is only reachable from a Claude session.

1. Run `/design-login` yourself.
2. Ask Claude to push `$DC_WORK/bundle` to graphban-local (`499ef95b-3483-4575-aa1c-fb88f2a7b543`).
   It runs `list_files`, then `finalize_plan` (`localDir` = the bundle), then `write_files` with `localPath`.
3. `pages/build-harnesses-proposed` is a proposal, not a capture. Never overwrite it.

## Where the variants come from

| Variant   | Source |
|-----------|--------|
| populated | self-host seed (`app.seed`, project Core Platform) |
| empty     | the fresh hosted project GRPH |
| loading   | `page.route` holds every `/api` call open, except the ones the shell needs to boot |
| error     | the same calls return 500 |

The error variants are the useful part. Each one checks that a failed fetch is not rendered as an empty
project ("absence reads as clean"). The first run found three pages that do exactly that. `build.py` flags
them in red on their cards. Update or remove a flag when its fix ships.

## Gotchas

- **Keep-list regex.** Match `/api/me(\/|\?|$)`, never a bare `/api/me` prefix. The bare prefix also
  matches `/api/memory`, so those pages quietly never fail.
- **CSS.** Use one capture's stylesheet whole. Deduping it line by line breaks the multi-line `@layer`
  blocks, and every card renders unstyled.
- **Frames.** They use `transform: translateZ(0)`, so fixed overlays (dialogs, drawers, the palette) stay
  inside their frame.
- **Localhost.** Playwright reaches localhost fine. Only desktop Chrome is intercepted on the dev Mac.
