"""The boundary. Everything else in this package writes through it (PRD-24 D2, S1).

**This is the one property a vendor child cannot have.** PRD-22 D-k is explicit that the
supervisor claims no sandbox: a vendor CLI runs headless with `--dangerously-skip-permissions`
and could write anywhere on the machine. We cannot fix that — we do not own their write path.

We own this one. So for `gbagent`, and only for `gbagent`, "writes stay inside the worktree" is
a property enforced in the tool rather than a rule stated in a prompt and hoped for.

**Resolve first, then check.** The check is on the RESOLVED target, never on the string the
model asked for. That matters three ways, and each is a real attack rather than a hypothetical:

- `../../etc/passwd` — resolved, it leaves; the string alone looks like a relative path.
- `/etc/passwd` — absolute, and joining it to a root in the obvious way silently discards
  the root (`Path("/a") / "/etc"` is `/etc`).
- `worktree/link -> /etc` then `link/passwd` — the string is entirely inside the worktree and
  the target is not. Only resolution can tell.

**The ROOT is resolved too.** On macOS a worktree under `/tmp` really lives at `/private/tmp`,
so comparing an unresolved root against a resolved path would refuse every legitimate write on
the machine this was written on.

**A refusal is data, not a crash.** The model sees `OutsideWorktree` as a tool error and can
correct itself — that is the whole point of returning it rather than raising past the loop.
It names both the request and what it resolved to, because a refusal that echoes only
`../../etc/x` hides the fact a reader needs.
"""
from __future__ import annotations

from pathlib import Path


class ToolError(Exception):
    """A tool refused. Reaches the model as a result it can act on, not a traceback."""


class OutsideWorktree(ToolError):
    """A path that resolved outside the worktree. Names both forms — see the module docstring."""


def safe_path(root: Path | str, requested: str) -> Path:
    """Resolve `requested` against the worktree and refuse anything that lands outside.

    Returns the resolved absolute path, which is what callers must use — resolving twice, or
    using the original string after checking the resolved one, is how this kind of guard gets
    quietly defeated.

    Works for paths that do not exist yet: `Path.resolve()` is non-strict, so it resolves the
    components that do exist (which is where a symlink would be) and appends the rest. That is
    what makes it usable for `write_file` creating a new file in a new directory.
    """
    base = Path(root).resolve()
    # `Path("/a") / "/etc"` is `/etc` — an absolute request wins the join and is then judged
    # like anything else. That is deliberate: an absolute path INSIDE the worktree is a
    # legitimate thing for a model holding the root to construct, and `/etc/passwd` is
    # refused because it resolves outside, not because of a rule about leading slashes.
    #
    # The first draft re-rooted absolutes to `<worktree>/etc/passwd` instead. That is worse
    # twice over: it silently writes somewhere the model did not ask for, and it means the
    # absolute case can never be refused, so AC-1 could not be tested for it.
    target = (base / Path(requested)).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise OutsideWorktree(
            f"{requested!r} resolves to {str(target)!r}, which is outside the worktree "
            f"({str(base)!r}). Paths are relative to the worktree root; nothing above it is "
            "reachable from here."
        ) from None
    return target
