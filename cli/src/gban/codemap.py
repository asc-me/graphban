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


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def tracked(repo: Path) -> list[tuple[str, str]]:
    """(path, blob sha) for every regular file at HEAD. Symlinks and submodules are skipped."""
    out = _git(repo, "ls-tree", "-r", "-z", "HEAD").decode("utf-8", "replace")
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


def build(repo: Path) -> dict:
    """Everything the command needs, read at HEAD."""
    rev = head(repo)
    rows = tracked(repo)
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
    return {"revision": rev, "tracked": len(rows), "texts": texts, "edges": edges}


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
    gone = sorted(p for p, n in existing.items()
                  if p not in texts and "::" not in p and n.get("fresh", True))
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

    def call(tool: str, args: dict) -> dict:
        counter[0] += 1
        if project:
            args = {**args, "project_id": project}
        reply = client.call("POST", "/api/mcp", {
            "jsonrpc": "2.0", "id": counter[0], "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        if "error" in reply:
            raise MapError(f"{tool}: {reply['error'].get('message', reply['error'])}")
        result = reply.get("result", {})
        if result.get("isError"):
            raise MapError(f"{tool}: {json.dumps(result.get('structuredContent') or result)[:300]}")
        return result.get("structuredContent") or {}

    return call
