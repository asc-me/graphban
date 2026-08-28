"""Where a supervisor keeps what has to outlive it.

PRD-22 D-h. A supervisor crash does not kill its children — they are separate
processes — so the next supervisor has to be able to find out what is already running
rather than starting blind beside a fleet it does not know about. That means state on
disk, and it means a supervisor crash costs the supervisor rather than the fleet.

Under the temp directory rather than a config or data directory, deliberately: on
reboot the children are gone too, so state that does not survive a reboot is state
nobody needed. What is left after a reboot is a stale lock (which the kernel already
released) and orphaned worktrees, and salvage handles those.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .hostos import user_tag

#: Only the owner. On Linux `/tmp` is shared, so the user tag is in the directory name
#: as well as the mode — not as a security boundary (PRD-22 D-k is explicit that there
#: isn't one) but so two accounts on one host do not silently contend for a lock.
#: Windows gives each account its own temp directory, so there the name is redundant
#: and the mode is close to meaningless; both are kept so the path reads the same.
_DIR_MODE = 0o700


class UnsupportedPlatform(RuntimeError):
    """Neither POSIX nor Windows. Kept because `hostos` implements exactly two hosts,
    and a third would silently take whichever branch was not guarded."""


def _require_supported() -> None:
    if os.name not in ("posix", "nt"):
        raise UnsupportedPlatform(
            f"gbfleet supports POSIX and Windows hosts (found os.name={os.name!r}). "
            "The supervisor's lock relies on the kernel releasing it when a process "
            "dies, and `hostos` implements that for those two only."
        )


def state_root() -> Path:
    """The directory this user's supervisors keep state in.

    Created 0700 explicitly rather than via `mkdir(mode=...)`, because that mode is
    masked by the umask and a permissive umask would leave it wider than it reads.
    """
    _require_supported()
    root = Path(tempfile.gettempdir()) / f"gbfleet-{user_tag()}"
    root.mkdir(exist_ok=True)
    try:
        root.chmod(_DIR_MODE)
    except (NotImplementedError, OSError):
        # Windows `chmod` only understands the read-only bit, and 0o700 there is a
        # gesture rather than a restriction. Not an error: each account already gets
        # its own temp directory, which is what the mode was buying on shared /tmp.
        if os.name != "nt":
            raise
    return root


class NotARepository(RuntimeError):
    pass


def repo_root(start: Path | str) -> Path:
    """The MAIN working tree of the repository containing `start`.

    Resolved through `--git-common-dir`, not `--show-toplevel`, and that difference is
    the whole point. The supervisor's job is to create linked worktrees, so a second
    supervisor started from inside one of them is overwhelmingly likely — and
    `--show-toplevel` would report that worktree, giving it a different lock key and
    letting it run alongside the first. `--max-workers` would then be a per-worktree
    cap rather than a per-repo one, which is precisely the hole D-h says closes.
    """
    start = Path(start)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise NotARepository(f"{start} is not inside a git repository") from exc

    # `--git-common-dir` answers relative to cwd on older git, absolute on newer.
    common = Path(out)
    if not common.is_absolute():
        common = start / common
    common = common.resolve()

    root = common.parent
    if not (root / ".git").exists():
        raise NotARepository(
            f"{start} resolves to {common}, which has no working tree. "
            "gbfleet supervises a checkout, not a bare repository."
        )
    return root


def repo_key(root: Path) -> str:
    """A filesystem-safe, collision-free name for one repository.

    The readable half is for whoever runs `ls` in the state directory; the digest is
    what actually distinguishes two repositories with the same directory name. Taken
    over the resolved path so `/repo`, `/repo/` and a symlink to it are one key.
    """
    resolved = Path(root).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{resolved.name}-{digest}"


def lock_path(root: Path | str, state: Path | str | None = None) -> Path:
    # `state` arrives as a string whenever it has crossed a process boundary (argv,
    # environment), which for a supervisor is the normal case rather than the odd one.
    root_dir = Path(state) if state else state_root()
    return root_dir / f"{repo_key(root)}.lock"
