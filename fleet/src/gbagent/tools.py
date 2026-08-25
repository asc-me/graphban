"""The execution tools — the only new surface this agent gets (PRD-24 D1, S1).

Three layers reach the model and only this one is new. **Coordination** is Graphban's 54 MCP
tools, role-narrowed at registration and unchanged. **Orientation** is eight of those same 54
(`code_neighbors`, `search_code`, `get_code_map`, …) which a vendor harness ignores and greps
instead — at 22–45 seconds a turn, a graph call that replaces a filesystem crawl is the whole
budget. This module is the third: read, list, grep, write, edit.

`run_tests` and `git_diff` are deliberately NOT here — they are S2, and there is no general
shell at all (D4). The rule for changing that: somebody names a task that genuinely cannot be
done without one, and records it. Not "it would be convenient".

**Every path goes through `safe_path`.** A tool that takes a path and does not is a hole in the
one property this agent has over a vendor child, so there is exactly one way in and it is the
first line of every function here.

**Refusals are results.** Each raises `ToolError`, which the loop turns into a tool result the
model reads and can correct — a wrong path should cost a turn, not the run.
"""
from __future__ import annotations

import re
from pathlib import Path

from .workspace import ToolError, safe_path

#: Read caps. A model that asks for a 40 MB file has made a mistake, and answering it would
#: blow the context window that D7's compaction is trying to protect.
MAX_READ_BYTES = 400_000
MAX_GREP_HITS = 200
MAX_LIST_ENTRIES = 500


def _text(path: Path, label: str) -> str:
    if not path.exists():
        raise ToolError(f"{label}: no such file")
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "created": created, "bytes": len(content.encode("utf-8"))}


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
