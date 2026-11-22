# Fleet

The **Fleet** page shows who is working on this project and what they can be spawned as.
This is the user guide to the page. The operator runbook for running a remote fleet is
[fleet-remote.md](fleet-remote.md); adapters are in [fleet-adapters.md](fleet-adapters.md).

It has two tabs: **Fleet management** and **Harness config**.

## Fleet management

- **Roster**: the agents registered on this project and what each holds. Offline agents fade
  rather than vanish, because one that died holding a branch is what you need to see. Rows can
  read "not enrolled", "branch orphaned" or "holding nothing".
- **Provision a wave**: mint one seat per agent. A seat grants a role for one session and expires;
  paste it into the agent's prompt, not its MCP config. A wave is one round of work: its name
  becomes the branch prefix. **End wave** revokes every seat and releases every lease it
  issued, is irreversible, and names which wave. A seat is not an API key: the key is minted on
  Settings → API keys. A wave key minted here is swept by End wave; a key you made yourself is never swept.
- **Review queue**: items waiting for a second pair of eyes, with who built each.
- **Ready work**: non-colliding work and why anything held back is waiting.
- Prompt snippets and install steps for an all-in-one agent or planner, and supervisor setup.

### Which key supervises a wave

A supervisor (`gbfleet up` / `until`) records each child's base, lease and PR on
`/api/fleet/observations`. It may record a seat only when it runs on the key that minted the
seat, or on that seat's wave key. A planner on an **unscoped** key (one spanning several
projects) mints seats that no supervisor can record, so run the planner on the project-scoped
key `gban setup` mints, and run gbfleet on that key too. `mint_enrolment` and
`delegate(seat=true)` return a `warning` when the calling key is unscoped (GRPH-1648).

Before spawning anything, `up` and `until` ask the server whether this key can record every
seat in the seats file. If it can't, they refuse to start and name the agent that minted the
seat and the key to run on. If the route refuses this key outright mid-wave (403, or the
masked 404), the supervisor reports it once, stops spawning and ends the wave not-ok, rather
than printing a `FAILED` line per receipt.

### Draining the maintenance line

`gbfleet until --line` drains the project's maintenance line instead of a PRD. Bare `--line`
is the line in `get_context.maintenance_line`; `--line <id>` names it. A PRD id is refused
with "use --prd", `--prd` with `--line` is refused, and a project with no line is refused
with a pointer to `create_line`. The line is never resolved through `get_prd`. Its review
seats (`mint_enrolment`) carry the line. `delegate` cannot carry one, so a build seat is bound
`bound_only` to its line item and exits once that item is in review. `collision_clusters`
has no line filter, so the run filters the clusters to the line's items itself.

- A `--line` run claims, reviews and exits on line items only. A `--prd` run does not claim
  line items, and a line row in review is listed under `review_outside_prd` there: it spawns
  no reviewer and does not fail the run. Rows with neither a PRD nor a line are listed the
  same way and are staffed by nobody.
- `review.staff_every_row` still widens a `--prd` run across other PRDs (its reviewer seat has
  no PRD scope, as before). It does not widen to rows with `line_id` or to rows with neither
  scope, and a `--line` run never widens into PRD reviews.
- A `--line` run's JSON always has `unready`, a list of `{id, missing}` (`acceptance`,
  `touchpoints`). `[]` means it looked and found none. Which items count is the server's
  claim pool: an open, unheld, unblocked line item that `collision_clusters` leaves out. When nothing is claimable and
  `unready` is not empty the run finishes `ok: false`, `reason: "unready"`, exit 0.
- Run the line drain in its own clone. There is one supervisor lock per checkout, unchanged,
  so do not start `--line` on a checkout a `--prd` drain holds.

## Harness config

- **Tier map**: harness × tier, editable for this deployment. An override is stored beside the
  committed matrix, so clearing one falls back to the packaged model. "Inherit from performance
  grading" fills only cells grading has measured; unmeasured cells are left as they were and say
  so. Save is off until the draft differs from what is stored.
- **Catalog**: the harness × model × tier table is the committed matrix and is read-only here.
- **Mix**: your share of recent spawns. Off means the scorer always picks the winner.
  Unregistered harnesses cannot take a share. Performance and rankings are on [harness.md](harness.md).

## gbfleet bench

A bench suite is a TOML manifest: a `[suite]` header (`id`, `version` as semver) and
`[[task]]` rows. Each task has `id`, `kind` (`build` or `review`), `base_sha`, a `[task.brief]`
(`title`, `description`, `acceptance`), `touchpoints`, `capabilities` (PRD-41 leaves, mirrored in
`gbfleet.bench.suite.CAPABILITY_LEAVES`), and `size_band`.

- **build**: `reference` (the reference solution's ref in the suite's repo, never copied into a
  child's worktree) and `[task.check]` (`command`, `tests`).
- **review**: `expect` (`bounce` or `sign_off`), `ref` (the planted-defect or clean ref) and a
  `[task.check]`.

The loader refuses a build task with no `base_sha` or `check`, a review task with no `expect`,
and a capability that is not a leaf, naming the task id and the field.

`gbfleet bench validate MANIFEST [--repo DIR]` runs each build task's check at `base_sha` (must
fail; else `green_at_base`) and at the reference (must pass). A review task's planted-defect ref
must make a check red and its clean ref must leave it green. A check command that cannot start
(a missing binary) reports `check_unrunnable`, never a red check. One line per task; exit 1 if
any task does not discriminate.

## Reading an empty result

"No ready work to partition" means nothing is ready, not that everything is done. "Nothing
waiting for a second pair of eyes" is about the review queue only. If no API key reaches the
project, the page says so and points to Settings.
