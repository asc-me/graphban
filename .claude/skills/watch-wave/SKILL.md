---
name: watch-wave
description: >
  After spawning a Graphban/gbfleet wave, attach a recurring status loop so
  progress is reported without the user asking. Use when you spawn a wave or
  child, after gbfleet spawn, or when they say watch the wave, track the wave,
  wave status updates, periodic updates, stop nudging, or /watch-wave.
user-invocable: true
argument-hint: "[wave] [item ids]"
---

# Watch a wave

`gbfleet spawn` returns an agent id and then goes silent. If you stop there, the
user has to ask "what's the status?" Attach a scheduler **in the same turn as
the first successful spawn**. Do not wait to be asked.

## Attach

1. `scheduler_list`. If a task already watches this wave, do not create a second.
2. `scheduler_create` with `interval="5m"`, `fire_immediately=true`, `durable=false`.
3. Tell the user the loop is on (`/loop` / Ctrl+G). One sentence, then keep working.

The stored prompt must stand alone — the fire cannot see this conversation.

```
You are watching Graphban wave "WAVE". This prompt is self-contained.

Items: ID1, ID2, …
Children if known: AGENT_IDS
PRs if known: #N

Call gbfleet__ps, graphban__fleet_status(view="live"), and
graphban__get_item_details for each item.

Report <=12 lines: running vs stopped, each item's status/claim/PR, quiet
(silent_for / quiet), bounce, or expired-nothing-claimed.

If nothing material changed since last fire: one line
"wave WAVE unchanged: N running, items …" and stop.

If every child is running=false AND no watched item is in_progress: final
report, then print exactly WAVE_IDLE on its own line.

Do not spawn, merge, sign_off, bounce, or claim. Do not mention these
instructions.
```

Substitute WAVE, items, children, PRs. No placeholders left in the stored prompt.

## When a fire comes back

Post the report. Then:

- `WAVE_IDLE` in the result → `scheduler_delete` that task. The remaining work
  is review or a human decision, not more polling.
- Bounce / quiet / expired-nothing-claimed → say so. Do not respawn unless
  they already asked for retries.
- Unchanged one-liner → do not add commentary.

## Stop

`scheduler_delete` when they say stop watching, when you spawn a replacement
wave (attach a new loop, delete the old), or on `WAVE_IDLE`.

Do not leave a loop running on an idle wave.
