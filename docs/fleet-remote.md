# Running a fleet on another machine

A drain on a box that is not your laptop: a spare Linux server, an old desktop, anything with
memory and a network path to Graphban. This is a runbook for a person to read and run, not a
command — see [Why there is no `gban fleet remote`](#why-there-is-no-gban-fleet-remote) at the
bottom for why that is deliberate rather than unfinished.

## Why you would

The measured case (2026-09-10), a 24 GB laptop against a 30 GB Ubuntu box:

| | laptop | server |
| --- | --- | --- |
| memory available to spawn into | 6.6 GB | **22.8 GB** |
| children the gate will allow | 6 | **30** |
| cores | 10 | 16 |
| a harness that may stop the supervisor | yes | **no** |

The last row is the one that prompted this. A wave launched from a Claude Code background task
was stopped mid-run with *"the system is running low on memory"* — the harness's monitor
reacting to system-wide pressure by killing the process it happens to own, which was the
supervisor, which took its children with it. Nothing is watching a `systemd --user` unit on a
server.

**Not always the answer.** One or two children on a laptop with a browser closed is fine, costs
nothing to set up, and you can watch it. Move when you want a drain that runs while you are not
there, or when `gbfleet doctor` starts reporting a memory headroom you do not like.

## What cannot be scripted, and it is most of the work

Installing gbfleet on the box is one command. **Authenticating four vendor CLIs is not**, and
that is the whole reason this is a runbook:

- every vendor's login is an interactive browser or device flow, and each one is different;
- a headless Linux box has no Keychain, so `claude` cannot store a login the way it does on a
  Mac — it needs `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in the service's environment,
  which `gbfleet doctor --adapter claude` will tell you;
- the children push branches, so the box needs git identity and push credentials of its own.

Budget an hour the first time, mostly waiting on logins.

## 0. What has to be true first

- Python **3.12 or newer** on the box (`gbfleet` refuses older; `graphban-fleet` declares it).
- `git`, and a **clone** of the repository — see [the lock](#the-lock-wants-its-own-clone).
- A network path from the box to the Graphban server. If the server is *on* this box, that is
  `http://localhost:8080`.
- A planner credential. **Not one from `gban keys mint`** — that mints a wave key with
  `FLEET_KEY_DAYS = 1`, and a drain whose credential dies overnight reports "set up" and stops
  being true. `gban setup` mints the non-expiring one; use that, or copy the key from a working
  `gbfleet until` you already run.

## 1. Install the supervisor

```bash
ssh box
curl -LsSf https://astral.sh/uv/install.sh | sh        # if uv is not there yet
uv tool install graphban-fleet
gbfleet --version
```

`uv tool install` gives it its own environment. Do **not** `pip install` it beside something
else: a fleet child is a subprocess of this program and inherits its environment, and sharing a
venv with the backend is how a thin install surface stops being thin.

## 2. Install and authenticate the vendors

Only the ones you will actually run. `docs/fleet-adapters.md` is the list, with the version
each adapter was verified against. For each:

```bash
gbfleet doctor --adapter claude --server http://localhost:8080 --project graphban
```

and fix what it names, in order. The checks worth knowing about here:

- **`kernel sandbox`** — if you run this from inside a sandboxed session, every vendor inherits
  the profile and dies before registering. On a plain ssh session this reads `none`.
- **`memory headroom`** — how many children fit right now. UNKNOWN means this host could not be
  asked, and the spawn gate does not bind on it.
- **adapter** — resolves the binary and checks its version against the matrix.

For `claude` specifically, on a box with no Keychain, put a token in the environment. It will
end up in the service's environment file in step 5, not in a shell profile.

## 3. Clone, and give the box a git identity

```bash
git clone git@github.com:you/yourrepo.git ~/drain
git -C ~/drain config user.name  "drain on box"
git -C ~/drain config user.email "drain@example.com"
git -C ~/drain push --dry-run    # prove the credentials before a child needs them
```

That last line is not ceremony. A child that finishes its work and cannot push leaves a salvage
branch nobody can review, and the failure surfaces at reap rather than at setup.

### The lock wants its own clone

The supervisor's repository lock is keyed on the git **common dir**, so it covers a whole clone
— every linked worktree included. While a drain runs, an interactive `gbfleet up` on that clone
is refused, and `git worktree add` does not get you a second one. Two drains want two clones.
`gbfleet service install` prints the path it will hold.

## 4. Prove one wave by hand before you supervise it

```bash
export GBFLEET_API_KEY=…                      # the non-expiring planner key
gbfleet until --repo ~/drain --server http://localhost:8080 \
    --project graphban --adapter claude --max-workers 2 --prd GRPH-P41
```

Read the terminal JSON: `spawned`, `spend`, `gated`, `headroom_bytes`, `reason`. Fix anything
that fails here **before** step 5 — debugging a vendor login through a service's log file is
strictly worse than debugging it in front of you.

## 5. Hand it to systemd

```bash
gbfleet service install --every 300 -- \
    --repo /home/you/drain --server http://localhost:8080 \
    --project graphban --adapter claude --max-workers 2
```

`--repo` must be absolute; a unit has no working directory worth inheriting. The API key is
read from `$GBFLEET_API_KEY` in **this** shell and written to an owner-only file the unit
references — never into the unit, which is world-readable.

Two things the install prints that are worth reading rather than scrolling past:

- **the PATH it captured.** A supervisor's job gets a minimal PATH and no vendor CLI is on it,
  so the unit carries the PATH of the shell you ran this from. Install from a login shell that
  can see your vendors, or the service starts, reports fine, and resolves no adapter.
- **`--every` is the polling interval**, because `until` exits when there is no ready work.
  Each cycle registers one planner agent — `register_agent` always creates a row and never
  reuses one by label — so seconds are the wrong unit here. 300 is the default for that reason.

### Linger, or it dies when you log out

```bash
loginctl enable-linger "$USER"
```

A `systemd --user` unit belongs to your session manager, which is torn down when your last
session ends. Without linger a drain installed over ssh runs beautifully until you disconnect
and is then stopped, enabled, and not running — everything reads fine. `install` warns when
this is off and `status` prints it every time.

## 6. Check it, and know what healthy looks like

```bash
gbfleet service status
gbfleet doctor --repo ~/drain --server http://localhost:8080 --project graphban
tail -f ~/.config/gbfleet/logs/drain.err.log
```

**`NOT running` is usually correct.** A drain is stopped for most of every cycle, because
`until` exits when the backlog is empty. The last exit code is what separates the two:

```
drain: idle between runs, last exit 0 (systemd, …)     ← healthy
drain: NOT running, last exit 3 (systemd, …)           ← something is wrong
```

`doctor` reads them the same way, and reports every drain installed on the box rather than the
one whose name it guessed.

The ledger is the other half: the Live page shows the children registering, claiming and
heartbeating. A drain that reports `idle between runs` forever while the backlog is not empty is
not idle — read `.err.log`, and look at whether the items are actually `next`.

## 7. Rolling it back

```bash
gbfleet service uninstall        # stops it, removes the unit AND the key file
```

Uncommitted child work is not lost by this: worktrees and their salvage branches survive, and
the next supervisor on that clone adopts or salvages what it finds. What does *not* survive is
the credential file, deliberately — a live key left in `~/.config` after an uninstall is the
leftover nobody goes looking for.

## Why there is no `gban fleet remote`

`gban swamp` settled this shape already: *"Does not install Swamp; a remote install script is
for a person to read and run."* The reasons are stronger here.

A command that SSHes into a box and provisions it would do the easy fifth of the work — write a
unit, install a package — and leave you SSHing in anyway for the four interactive vendor logins
that are the actual job. A tool that gets you most of the way and does not say so is worse than
a document, because it implies the rest is handled.

It would also make `gban` a secrets-distribution tool. Writing a live key into a config file on
the machine you are sitting at is one thing; pushing one onto another host over a transport
`gban` does not own is a different thing, and D-k saying none of this is a security boundary is
not a reason to add the capability.

## See also

- [`fleet/README.md`](../fleet/README.md) — the supervisor's own guide: waves, the memory gate,
  spend, and `gbfleet service` in detail.
- [`docs/fleet-adapters.md`](fleet-adapters.md) — which vendor CLIs run, and at which versions.
- [`docs/delegation.md`](delegation.md) — handing ONE item to a child, which is the other shape
  and does not need any of this.
- [`docs/native-install.md`](native-install.md) — putting the Graphban **server** on the same
  box, under the same supervisor.
