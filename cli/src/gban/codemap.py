"""`gban code map` — build and refresh the code graph from the repo (GRPH-1706).

Nothing else builds the graph: it is written only when an agent calls `describe_code`, so it
drifts to a few hundred hand-written nodes out of a repo of thousands of files. This reads
what HEAD tracks and posts every non-binary file as a node, stamped `revision=HEAD`.

- Files are read from the git object store (`git cat-file --batch`), never the working
  directory, so `content_hash` describes exactly the commit `revision` names. The hash is
  `sha256:` + sha256 of the rstripped text (backend/app/hashing.py).
- A node's `summary` is sent ONLY for a file the graph does not have, or has with an empty
  summary. A hand-written summary is never resent, and `describe_code` keeps an omitted
  `summary`/`name`, so a rerun leaves it byte for byte.
- Edges: Python absolute imports mapped through the package roots, and relative TS/TSX imports.
- Batches never carry `prune`: it would mark every node outside the batch stale. Paths in the
  graph but gone at HEAD are retired in one final `describe_code` call with `stale: [...]`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import re
import subprocess
from pathlib import Path

BATCH = 80
SKIP_PREFIXES = ("dist-release/",)
LOCKFILES = {
    "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "bun.lockb", "composer.lock", "go.sum",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".pdf", ".zip",
    ".gz", ".tgz", ".tar", ".whl", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3",
    ".mp4", ".mov", ".wasm", ".so", ".dylib", ".pyc", ".sqlite", ".db", ".bin", ".jar",
}
_LANG = {".py": "python", ".ts": "ts", ".tsx": "ts", ".js": "js", ".jsx": "js",
         ".md": "markdown", ".json": "json", ".toml": "toml", ".yml": "yaml",
         ".yaml": "yaml", ".sh": "shell", ".css": "css", ".html": "html", ".sql": "sql"}
_TS_IMPORT = re.compile(
    r"""(?:import|export)\s[^'"]*?from\s*['"](\.[^'"]*)['"]|import\s*['"](\.[^'"]*)['"]"""
    r"""|import\(\s*['"](\.[^'"]*)['"]\s*\)""")
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


class MapError(Exception):
    """The repo cannot be mapped (not git, no commit)."""


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                              capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MapError(f"git could not run: {exc}") from exc
    if proc.returncode != 0:
        raise MapError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def head(repo: Path, rev: str = "HEAD") -> str:
    return _git(repo, "rev-parse", rev).decode().strip()


def tracked(repo: Path, rev: str = "HEAD") -> list[tuple[str, str]]:
    """(path, blob sha) for every regular file at HEAD. Symlinks and submodules are skipped."""
    out = _git(repo, "ls-tree", "-r", "-z", rev).decode("utf-8", "replace")
    rows = []
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, kind, sha = meta.split()
        if kind == "blob" and mode in ("100644", "100755"):
            rows.append((path, sha))
    return sorted(rows)


def skipped(path: str) -> bool:
    name = posixpath.basename(path)
    return (path.startswith(SKIP_PREFIXES) or name in LOCKFILES
            or posixpath.splitext(name)[1].lower() in BINARY_SUFFIXES)


def read_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """Blob contents by sha, through one `git cat-file --batch`."""
    if not shas:
        return {}
    unique = sorted(set(shas))
    raw = _git(repo, "cat-file", "--batch", stdin=("\n".join(unique) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        end = raw.index(b"\n", pos)
        header = raw[pos:end].split()
        size = int(header[2])
        blobs[sha] = raw[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.rstrip().encode("utf-8", "replace")).hexdigest()


def decode(data: bytes) -> str | None:
    """The text, or None for a binary file."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def derive_summary(path: str, text: str) -> str:
    """Python docstring, first markdown heading, or first comment; '' when none."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError):
            doc = None
        if doc:
            return " ".join(doc.split("\n\n")[0].split())[:400]
    if suffix == ".md":
        for line in text.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip()[:400]
        return ""
    for line in text.splitlines()[:30]:
        s = line.strip()
        for lead in ("//", "#!", "#", "/*", "*", "--"):
            if s.startswith(lead):
                body = s[len(lead):].strip(" */")
                if body:
                    return body[:400]
                break
    return ""


def package_roots(paths: list[str]) -> dict[str, str]:
    """Top-level python package name -> its directory, from tracked __init__.py files."""
    inits = {posixpath.dirname(p) for p in paths if p.endswith("/__init__.py")}
    roots = {}
    for d in sorted(inits):
        if posixpath.dirname(d) not in inits:
            roots.setdefault(posixpath.basename(d), d)
    return roots


def _py_target(module: str, roots: dict[str, str], known: set[str]) -> str | None:
    top, _, rest = module.partition(".")
    if top not in roots:
        return None
    base = posixpath.join(roots[top], *rest.split(".")) if rest else roots[top]
    for cand in (base + ".py", base + "/__init__.py"):
        if cand in known:
            return cand
    return None


def python_edges(path: str, text: str, roots: dict[str, str], known: set[str]) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for mod in mods:
            target = _py_target(mod, roots, known)
            if target and target != path:
                found.add(target)
    return found


def ts_edges(path: str, text: str, known: set[str]) -> set[str]:
    found: set[str] = set()
    for m in _TS_IMPORT.finditer(text):
        spec = next(g for g in m.groups() if g)
        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        cands = [base] + [base + s for s in _TS_SUFFIXES] + [
            posixpath.join(base, "index" + s) for s in _TS_SUFFIXES]
        for cand in cands:
            if cand in known and cand != path:
                found.add(cand)
                break
    return found


def build(repo: Path, rev: str = "HEAD") -> dict:
    """Everything the command needs, read at HEAD."""
    rev = head(repo, rev)
    rows = tracked(repo, rev)
    wanted = [(p, s) for p, s in rows if not skipped(p)]
    blobs = read_blobs(repo, [s for _, s in wanted])
    texts: dict[str, str] = {}
    for path, sha in wanted:
        text = decode(blobs[sha])
        if text is not None:
            texts[path] = text
    known = set(texts)
    roots = package_roots(sorted(known))
    edges: list[dict] = []
    for path, text in sorted(texts.items()):
        suffix = posixpath.splitext(path)[1].lower()
        if suffix == ".py":
            dsts, kind = python_edges(path, text, roots, known), "imports"
        elif suffix in _TS_SUFFIXES:
            dsts, kind = ts_edges(path, text, known), "imports"
        else:
            continue
        edges.extend({"src": path, "dst": d, "type": kind} for d in sorted(dsts))
    return {"revision": rev, "tracked": len(rows), "paths": [p for p, _ in rows],
            "texts": texts, "edges": edges}


def node_for(path: str, text: str, keep_summary: bool) -> dict:
    node = {"path": path, "kind": "file", "lang": _LANG.get(posixpath.splitext(path)[1].lower(), ""),
            "content_hash": content_hash(text)}
    if not keep_summary:
        node["summary"] = derive_summary(path, text)
    return node


def existing_map(call) -> dict[str, dict]:
    """path -> existing node, via `get_code_map limit:0`."""
    result = call("get_code_map", {"limit": 0})
    return {n["path"]: n for n in result.get("nodes", [])}


def present_paths(tracked_paths: list[str]) -> set[str]:
    """Every path HEAD still has: each tracked file (skipped and binary ones included) and
    every directory above one. A graph path in neither is gone."""
    out: set[str] = set()
    for p in tracked_paths:
        out.add(p)
        d = posixpath.dirname(p)
        while d and d not in out:
            out.add(d)
            d = posixpath.dirname(d)
    return out


def plan(built: dict, existing: dict[str, dict]) -> dict:
    texts = built["texts"]
    nodes, kept, new = [], 0, 0
    for path in sorted(texts):
        old = existing.get(path)
        has_summary = bool(old and (old.get("summary") or "").strip())
        if old:
            kept += has_summary
        else:
            new += 1
        nodes.append(node_for(path, texts[path], keep_summary=has_summary))
    # A tracked path the mapper skips (lockfile, binary, SKIP_PREFIXES) is never described, so a
    # full run would leave it at its old revision. Restamp a fresh one at the target, keeping its
    # kind/lang/content_hash, as `plan_since` does. A gone path or retired node is never restamped.
    tracked_set = set(built["paths"])
    for path, old in sorted(existing.items()):
        if (path in texts or path not in tracked_set or "::" in path
                or not old.get("fresh", True) or not (old.get("content_hash") or "")):
            continue
        nodes.append({"path": path, "kind": old.get("kind", "file"), "lang": old.get("lang", ""),
                      "content_hash": old["content_hash"]})
    alive = present_paths(built["paths"])
    gone = sorted(p for p, n in existing.items()
                  if "::" not in p and n.get("fresh", True) and p.rstrip("/") not in alive)
    return {"nodes": nodes, "existing": len(existing), "new": new, "kept": kept, "gone": gone, "edges": built["edges"]}


def apply(call, built: dict, planned: dict, batch: int = BATCH) -> dict:
    """Post nodes then edges in batches (no prune), then retire the gone paths."""
    rev = built["revision"]
    nodes, edges = planned["nodes"], planned["edges"]
    for i in range(0, len(nodes), batch):
        call("describe_code", {"nodes": nodes[i:i + batch], "revision": rev})
    for i in range(0, len(edges), batch * 4):
        call("describe_code", {"edges": edges[i:i + batch * 4], "revision": rev})
    marked = 0
    if planned["gone"]:
        reply = call("describe_code", {"stale": planned["gone"], "revision": rev})
        marked = reply.get("marked_stale", len(planned["gone"]))
    return {"nodes": len(nodes), "edges": len(edges), "marked_stale": marked}


def mcp_caller(client, project: str):
    """A `call(tool, args)` over POST /api/mcp (tools/call), returning structuredContent."""
    counter = [0]

    def call(tool: str, args: dict, timeout: float | None = None) -> dict:
        counter[0] += 1
        if project:
            args = {**args, "project_id": project}
        reply = client.call("POST", "/api/mcp", {
            "jsonrpc": "2.0", "id": counter[0], "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, timeout=timeout)
        if "error" in reply:
            raise MapError(f"{tool}: {reply['error'].get('message', reply['error'])}")
        result = reply.get("result", {})
        if result.get("isError"):
            raise MapError(f"{tool}: {json.dumps(result.get('structuredContent') or result)[:300]}")
        return result.get("structuredContent") or {}

    return call


# --- `gban code map --since` (GRPH-1820): the incremental path ------------------------------

class FetchError(MapError):
    """`git fetch` failed: the run is recorded as failed and posts nothing."""


def default_target(repo: Path) -> tuple[str, str]:
    """(ref, sha) of `origin/<default branch>` after a `git fetch`. Never the working tree."""
    try:
        _git(repo, "fetch", "--quiet", "origin")
    except MapError as exc:
        raise FetchError(str(exc)) from exc
    ref = ""
    try:
        ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").decode().strip()
    except MapError:
        for cand in ("origin/main", "origin/master"):
            try:
                _git(repo, "rev-parse", "--verify", "--quiet", cand)
                ref = cand
                break
            except MapError:
                continue
    if not ref:
        raise MapError("cannot find origin's default branch (no origin/HEAD, origin/main or origin/master)")
    return ref, _git(repo, "rev-parse", ref).decode().strip()


def pinned_revision(result: dict) -> tuple[str | None, str]:
    """(pinned sha or None, why it is None) from a `get_code_map limit:0` reply. None means:
    no single revision to diff from."""
    rev = result.get("revision")
    if rev:
        return rev, ""
    nodes = result.get("nodes", [])
    if not nodes:
        return None, "the map is empty"
    # The server's `revision` counts stale nodes, and a retired path keeps the revision it was
    # last described at, so one deletion would read as "mixed" forever. A stale node says
    # nothing about what the live map describes: judge the fresh ones.
    # Directory nodes the mapper never maps carry no content_hash and an empty revision, and a
    # symbol (`::`) is a hand-described annotation: neither says what the file map was built at.
    live = [n for n in nodes
            if n.get("fresh", True) and (n.get("content_hash") or "") and "::" not in n.get("path", "")]
    seen = {n.get("revision") or "" for n in live}
    if len(seen) == 1 and "" not in seen:
        return next(iter(seen)), ""
    known = sorted(r for r in seen if r)
    if len(known) > 1:
        return None, f"the map is mixed ({len(known)} revisions)"
    return None, "the map has no pinned revision"


def fallback_reason(repo: Path, pinned: str | None, why: str, target: str) -> str:
    """Why `--since` must fall back to a full scan, or '' when the diff can run."""
    if not pinned:
        return why
    try:
        _git(repo, "cat-file", "-e", f"{pinned}^{{commit}}")
    except MapError:
        return f"pinned revision {pinned[:12]} is missing or unreachable"
    try:
        _git(repo, "merge-base", "--is-ancestor", pinned, target)
    except MapError:
        return f"pinned revision {pinned[:12]} is not an ancestor of {target[:12]}"
    return ""


def name_status(repo: Path, pinned: str, target: str) -> list[tuple[str, str, str]]:
    """(status letter, path, old path) from `git diff --name-status -M`. old is '' unless renamed."""
    out = _git(repo, "diff", "--name-status", "-M", "-z", f"{pinned}..{target}").decode("utf-8", "replace")
    parts = out.split("\0")
    rows, i = [], 0
    while i < len(parts) and parts[i]:
        status = parts[i][0]
        if status in "RC":
            rows.append((status, parts[i + 2], parts[i + 1]))
            i += 3
        else:
            rows.append((status, parts[i + 1], ""))
            i += 2
    return rows


def plan_since(repo: Path, pinned: str, target: str, existing: dict[str, dict]) -> dict:
    """The incremental plan: changed files re-described, deleted and renamed-from paths stale,
    every other fresh node re-stamped (hash only) so the whole map ends pinned at `target`."""
    rows = tracked(repo, target)
    shas = dict(rows)
    known = {p for p, _ in rows if not skipped(p)}
    roots = package_roots(sorted(known))
    changes = name_status(repo, pinned, target)
    want = [p for st, p, _ in changes if st != "D" and p in shas and not skipped(p)]
    blobs = read_blobs(repo, [shas[p] for p in want])
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    gone: list[str] = []
    n_skipped = 0
    counts = {"added": 0, "modified": 0, "renamed": 0, "deleted": 0}
    for st, path, old_path in changes:
        if st == "D":
            counts["deleted"] += 1
            if path in existing and "::" not in path:
                gone.append(path)
            continue
        counts["renamed" if old_path else "added" if st == "A" else "modified"] += 1
        text = decode(blobs[shas[path]]) if shas.get(path) in blobs else None
        if old_path and old_path in existing:
            gone.append(old_path)
        if text is None:
            n_skipped += 1
            continue
        old = existing.get(path)
        src = existing.get(old_path) if old_path else None
        node = node_for(path, text, keep_summary=bool(old and (old.get("summary") or "").strip()))
        if src and not old:
            if (src.get("summary") or "").strip():
                node["summary"] = src["summary"]
            if src.get("name"):
                node["name"] = src["name"]
        nodes[path] = node
        suffix = posixpath.splitext(path)[1].lower()
        if suffix == ".py":
            dsts = python_edges(path, text, roots, known)
        elif suffix in _TS_SUFFIXES:
            dsts = ts_edges(path, text, known)
        else:
            dsts = set()
        edges.extend({"src": path, "dst": d, "type": "imports"} for d in sorted(dsts))
    gone_set = set(gone)
    restamp = [{"path": p, "kind": n.get("kind", "file"), "lang": n.get("lang", ""),
                "content_hash": n.get("content_hash", "")}
               for p, n in sorted(existing.items())
               if p not in nodes and p not in gone_set and n.get("fresh", True)]
    return {"nodes": restamp + [nodes[p] for p in sorted(nodes)], "edges": edges,
            "gone": sorted(gone_set), "changed": len(nodes), "restamped": len(restamp),
            "skipped": n_skipped, "counts": counts, "revision": target}
