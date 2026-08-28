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

    **What this guarantees, exactly: nothing reachable BY PATH resolves outside the worktree.**
    That covers every shape above — traversal, absolute paths, and symlinks at any depth.

    **It does not cover a hardlink, and that is accepted rather than overlooked** (GRPH-561).
    A hardlink to a file outside the worktree is not a reference to that file, it IS that file,
    listed in two directories at once. Demonstrated against this function rather than reasoned:
    the link is ALLOWED, reads return the outside file's contents, `st_nlink` is 2, and
    `is_symlink()` returns **False** — so the check that catches the symlink case is blind here
    and `resolve()` has nothing to follow. There is no path-based way to tell.

    `st_nlink > 1` is not the missing check. It refuses legitimate files, it still misses the
    case on a filesystem that does not report link counts, and it would have the tool rejecting
    things a model can neither predict nor repair.

    This paragraph exists because the rest of this docstring reads as a complete account of the
    boundary, and a reader concludes the worktree is sealed. It is sealed against everything a
    path can express. Someone has to already know about hardlinks to know that — which is the
    shape of gap this repository keeps rediscovering and re-filing.
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
        # Quoted, not `!r`. `repr` of a Windows path doubles every separator, so the
        # refusal named the boundary as `C:\\Users\\...` — a path nobody would type,
        # in the one message whose whole job is to tell the model where it may go
        # instead. Invisible on POSIX, where the two renderings are identical
        # (GRPH-591).
        raise OutsideWorktree(
            f"'{requested}' resolves to '{target}', which is outside the worktree "
            f"('{base}'). Paths are relative to the worktree root; nothing above it is "
            "reachable from here."
        ) from None
    return target
