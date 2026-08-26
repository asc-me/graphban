"""The execution tools — the only new surface this agent gets (PRD-24 D1, S1).

Three layers reach the model and only this one is new. **Coordination** is Graphban's 54 MCP
tools, role-narrowed at registration and unchanged. **Orientation** is eight of those same 54
(`code_neighbors`, `search_code`, `get_code_map`, …) which a vendor harness ignores and greps
instead — at 22–45 seconds a turn, a graph call that replaces a filesystem crawl is the whole
budget. This module is the third: read, list, grep, write, edit.

`run_tests` and `git_diff` are deliberately NOT here — they are S2, and there is no general
shell at all (D4). The rule for changing that: somebody names a task that genuinely cannot be
done without one, and records it. Not "it would be convenient".

**Every path goes through `safe_path` — including the ones nobody passed in.** A tool that
takes a path and does not check it is a hole in the one property this agent has over a vendor
child, so there is exactly one way in and it is the first line of every function here.

That sentence used to be the whole story and it was not enough (GRPH-487). `grep` checked its
argument and then walked to files nobody named, one of which was a symlink out of the tree —
so the boundary held for the path the model asked about and not for the path it got back.
**A tool that REACHES a path it was not given has to check that one too**, which is why the
walk in `grep` re-checks every entry rather than trusting where it started.

**Refusals are results.** Each raises `ToolError`, which the loop turns into a tool result the
model reads and can correct — a wrong path should cost a turn, not the run.
"""
from __future__ import annotations

import re
from pathlib import Path

from .workspace import OutsideWorktree, ToolError, safe_path

#: Read caps. A model that asks for a 40 MB file has made a mistake, and answering it would
#: blow the context window that D7's compaction is trying to protect.
MAX_READ_BYTES = 400_000

#: A whole-file write that replaces an established file with a fraction of itself.
#:
#: **FOUND BY THE S7 ACCEPTANCE WALK.** Told it could not move an item to review without
#: changing something, `qwen3-coder:30b` replaced 856 lines of `services/items.py` with six,
#: opening with the comment "This is a placeholder file to simulate the fix". `write_file`
#: writes what it is given; nothing objected.
#:
#: `edit_file` is the safe primitive for changing part of a file and it already refuses an
#: ambiguous anchor. `write_file` exists for NEW files, and using it on a large existing one
#: means the model is rewriting from memory — which is exactly where a weak model loses
#: everything it did not remember. The thresholds are deliberately conservative: a genuine
#: whole-file rewrite that halves a file still goes through.
SHRINK_MIN_LINES = 40
SHRINK_MAX_RATIO = 0.3
MAX_GREP_HITS = 200
MAX_LIST_ENTRIES = 500


def _text(path: Path, label: str) -> str:
    if not path.exists():
        # The hint is not decoration: a live run against `qwen3-coder:30b` asked for `calc.py`
        # when the file was `backend/calc.py`, having taken the test command's `cwd` for the
        # root. A refusal the model can act on costs one turn; one it cannot costs the run.
        raise ToolError(
            f"{label}: no such file. Paths are relative to the WORKTREE ROOT, not to the "
            "test command's working directory — use list_dir to see what is there."
        )
    if path.is_dir():
        raise ToolError(f"{label}: is a directory — use list_dir")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(
            f"{label}: {size} bytes, over the {MAX_READ_BYTES} limit. Use grep to find the "
            "part you need."
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{label}: not UTF-8 text") from None


def read_file(root: Path, path: str, *, start: int = 1, count: int = 0) -> dict:
    """Read a file, optionally a line range. Lines are 1-indexed, as every editor reports them.

    The range exists so a model chasing one function in a large file spends one turn rather
    than one turn plus a context window.
    """
    target = safe_path(root, path)
    lines = _text(target, path).splitlines()
    if start < 1:
        raise ToolError(f"start must be 1 or greater, got {start}")
    chunk = lines[start - 1: (start - 1 + count) if count else None]
    return {
        "path": path,
        "start": start,
        "lines": len(chunk),
        "total_lines": len(lines),
        "truncated": bool(count) and (start - 1 + count) < len(lines),
        "text": "\n".join(chunk),
    }


def list_dir(root: Path, path: str = ".") -> dict:
    """Names and kinds in one directory. Not recursive — that is what `grep` is for."""
    target = safe_path(root, path)
    if not target.exists():
        raise ToolError(f"{path}: no such directory")
    if not target.is_dir():
        raise ToolError(f"{path}: not a directory — use read_file")
    kids = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    shown = kids[:MAX_LIST_ENTRIES]
    return {
        "path": path,
        "entries": [
            {"name": k.name, "kind": "dir" if k.is_dir() else "file",
             "bytes": None if k.is_dir() else k.stat().st_size}
            for k in shown
        ],
        "truncated": len(kids) > len(shown),
    }


def grep(root: Path, pattern: str, *, path: str = ".", glob: str = "*") -> dict:
    """Search file contents. Returns path, line number and the matching line.

    Skips `.git` and anything unreadable rather than failing the call: a search that dies on
    one binary blob has spent a turn to tell the model nothing.

    **Every file the walk REACHES is checked, not just the path it was given** (GRPH-487).
    `safe_path` on the argument only proves where the search STARTS. `rglob` then yields
    whatever is under there, and a symlink to a file outside the worktree is an ordinary
    entry: it is not a directory, so nothing skipped it, and `read_text` followed it happily.
    The result was that `read_file` refused a path and `grep` printed the contents of the
    same file, which is a boundary in one tool and a suggestion in the other.

    Directories were never the hole — `rglob` does not descend into symlinked directories and
    the `is_dir()` skip drops them anyway. It was symlinks to FILES, which is the one shape
    the original fixture did not build. pnpm stores, bazel-* convenience links and venv links
    are all exactly that shape in ordinary trees.
    """
    base = safe_path(root, path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"bad regex {pattern!r}: {e}") from None
    if not base.exists():
        raise ToolError(f"{path}: no such directory")

    hits: list[dict] = []
    walk = base.rglob(glob) if base.is_dir() else [base]
    for f in walk:
        if len(hits) >= MAX_GREP_HITS:
            break
        if f.is_dir() or ".git" in f.parts:
            continue
        try:
            safe_path(root, str(f))
        except OutsideWorktree:
            # Reached through a symlink that leaves the worktree. Skipped rather than
            # raised: one stray link in a tree must not fail an otherwise good search,
            # for the same reason an unreadable blob does not.
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(content.splitlines(), 1):
            if rx.search(line):
                hits.append({
                    "path": str(f.relative_to(safe_path(root, "."))),
                    "line": n,
                    "text": line[:300],
                })
                if len(hits) >= MAX_GREP_HITS:
                    break
    return {"pattern": pattern, "hits": hits, "truncated": len(hits) >= MAX_GREP_HITS}


def write_file(root: Path, path: str, content: str) -> dict:
    """Write a file whole, creating any missing directories along the way.

    **The boundary is checked before any `mkdir`** — `safe_path` runs first, so a directory can
    only ever be created inside the worktree and a refused path leaves nothing behind. Writing
    `fleet/src/gbagent/loop.py` when `gbagent/` does not exist simply works, because a separate
    `make_dir` tool would cost a turn at ~30s every time a new directory appears and buy a
    distinction nobody reading the transcript needs (D2).
    """
    target = safe_path(root, path)
    if target.is_dir():
        raise ToolError(f"{path}: is a directory")
    created = not target.exists()
    if not created:
        _refuse_truncation(target, path, content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "created": created, "bytes": len(content.encode("utf-8"))}


def _refuse_truncation(target: Path, path: str, content: str) -> None:
    """Refuse to replace an established file with a fraction of itself — see the constants.

    A refusal rather than a warning, because the model is unattended and there is nobody to
    read a warning. It names `edit_file` because a refusal the model cannot act on costs the
    run, and because `edit_file` is the tool it should have reached for.
    """
    try:
        existing = target.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return  # not text, or unreadable: this guard has no opinion
    if len(existing) < SHRINK_MIN_LINES:
        return
    incoming = content.splitlines()
    if len(incoming) >= len(existing) * SHRINK_MAX_RATIO:
        return
    raise ToolError(
        f"{path}: refusing to replace {len(existing)} lines with {len(incoming)}. "
        "write_file rewrites a file WHOLE, so this would delete everything you did not "
        "reproduce — and rewriting a large file from memory is how that happens by accident. "
        "Use edit_file to change the part you mean, or read_file first if you genuinely "
        "intend to replace all of it."
    )


def edit_file(root: Path, path: str, old: str, new: str, *, count: int = 1) -> dict:
    """Replace an exact substring. Refuses when `old` is absent or ambiguous.

    Refusing an ambiguous match is the point. A model that asks to replace `return None` in a
    file holding nine of them and gets the first has made a wrong edit that looks like a right
    one — and at ~30s a turn, discovering that through a failing test costs far more than the
    turn this refusal spends. The refusal names the count so the next attempt can widen the
    anchor rather than guess.
    """
    target = safe_path(root, path)
    text = _text(target, path)
    found = text.count(old)
    if found == 0:
        raise ToolError(f"{path}: {old!r} does not appear — read the file and match it exactly")
    if count and found > count:
        raise ToolError(
            f"{path}: {old!r} appears {found} times, expected {count}. Widen the anchor with "
            "surrounding lines so the match is unique, or pass count."
        )
    target.write_text(text.replace(old, new), encoding="utf-8")
    return {"path": path, "replaced": found}
