"""`gban stack map` — the deterministic app-topology producer (GRPH-P55 §3).

Reads compose files, Dockerfiles, dependency manifests and env templates, and
builds the record `POST /api/platform/stack/app` accepts. Stdlib only: the CLI
declares no dependencies, so compose is a YAML subset rather than PyYAML.

The subset is block mappings and lists, single-line flow, and `|` / `>` scalars.
Anchors and aliases are not expanded (`*name` stays text). A `<<` merge key is
ignored. A file the subset cannot read is skipped with a warning rather than
half-parsed into a map that looks complete.

What this refuses to do, on purpose:

- Source code is not scanned. Raw HTTP calls are the later agent pass.
- Env **values** never enter the record. A host is taken only from a literal
  URL in a template, and stored as the bare host (`api.stripe.com`). The posted
  shape rejects any string containing `://`, so a URL with a scheme would 422
  the whole post.
- `.env` is not a template. Only `.env.example` and compose `environment:` keys.
- A `depends_on`, `external: true`, or `extends.file` that names something this
  file does not define becomes an `unresolved` node. It is not a failure and it
  is not dropped.
- An `image:` this table does not know becomes infrastructure named after the
  **compose service**, kind `unclassified image` — not after the image.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlparse

PRODUCER_ID = "gban"
_POST_PATH = "/api/platform/stack/app"

_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "dist", "build", "target",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "site-packages",
    ".next", "coverage", "vendor", ".gbfleet", ".hg", ".svn",
})
_MAX_BYTES = 1_000_000
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
_SCHEME = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "redis": "redis",
    "rediss": "redis",
    "amqp": "amqp",
    "amqps": "amqp",
    "mongodb": "mongodb",
    "mysql": "mysql",
    "http": "https",
    "https": "https",
    "smtp": "smtp",
}
# Same idea as the server's secret filter: a value that would 422 the post, or
# that is a credential. Matched on the record we are about to return. The match
# text is never included in the error — that would be the leak, printed.
_LEAK = re.compile(
    r"://"
    r"|(?<![A-Za-z0-9_])(?:password|secret|api_key|token)\s*=\s*\S"
    r"|(?:^|[^A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9]|sk_live_|sk_test_|ghp_[A-Za-z0-9]|github_pat_|"
    r"xox[baprs]-|AKIA[A-Z0-9]"
    r")",
    re.IGNORECASE,
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERP = re.compile(r"\$\{([^}]*)\}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'`<>]+")
_PKG_SPLIT = re.compile(r"[\s<>=~!;,\[]")


class MapError(Exception):
    """The tree cannot be mapped. Nothing was posted."""


class Marked:
    """A parsed value and the 1-based line it was written on."""

    __slots__ = ("value", "line")

    def __init__(self, value: object, line: int) -> None:
        self.value = value
        self.line = line


# ── YAML subset ──────────────────────────────────────────────────────────────


def _indent_width(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 2
        else:
            break
    return width


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == '"':
            return (
                inner.replace("\\n", "\n").replace("\\t", "\t")
                .replace('\\"', '"').replace("\\\\", "\\")
            )
        return inner.replace("''", "'")
    return text


def _strip_anchor(text: str) -> str:
    """Drop a leading `&name`. The alias is not recorded; the value still is."""
    if text.startswith("&"):
        parts = text.split(None, 1)
        return parts[1] if len(parts) > 1 else ""
    return text


def _scalar(text: str) -> object:
    text = _strip_anchor(text.strip())
    if text in ("", "null", "Null", "NULL", "~"):
        return None if text else ""
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return _unquote(text)
    return text


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    depth = 0
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
            buf.append(ch)
            continue
        if ch in "]}":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_flow(text: str, line: int) -> object:
    text = text.strip()
    if text.startswith("[") and "]" in text:
        inner = text[1:text.rfind("]")]
        return [Marked(_scalar(part), line) for part in _split_flow(inner)]
    if text.startswith("{") and "}" in text:
        inner = text[1:text.rfind("}")]
        mapping: dict[str, Marked] = {}
        for part in _split_flow(inner):
            if ":" not in part:
                continue
            key, _, val = part.partition(":")
            mapping[key.strip().strip("\"'")] = Marked(_scalar(val.strip()), line)
        return mapping
    return _scalar(text)


def _split_key(content: str) -> tuple[str | None, str]:
    """`(key, value_text)`. `value_text` is empty when the line is `key:`."""
    if content.startswith(("'", '"')):
        quote = content[0]
        end = content.find(quote, 1)
        if end < 0:
            return None, ""
        key = content[1:end]
        rest = content[end + 1:].lstrip()
        if not rest.startswith(":"):
            return None, ""
        return key, rest[1:].strip()
    if ":" not in content:
        return None, ""
    key, _, val = content.partition(":")
    key = key.strip()
    if not key or key.startswith(("-", "*", "!", "%")):
        return None, ""
    return key, val.strip()


def parse_yaml_subset(text: str) -> Marked:
    """Parse the compose subset. Never raises on awkward input; it returns what it could."""
    entries: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        entries.append((_indent_width(stripped), stripped.strip(), lineno))
    pos = 0

    def parse_node(min_indent: int) -> Marked | None:
        nonlocal pos
        if pos >= len(entries) or entries[pos][0] < min_indent:
            return None
        if entries[pos][1].startswith("- ") or entries[pos][1] == "-":
            return parse_list(entries[pos][0])
        return parse_map(entries[pos][0])

    def take_value(indent: int, line: int, value_src: str) -> Marked:
        nonlocal pos
        if value_src in ("|", ">", "|-", ">+", "|+", ">-"):
            blob: list[str] = []
            while pos < len(entries) and entries[pos][0] > indent:
                blob.append(entries[pos][1])
                pos += 1
            return Marked("\n".join(blob), line)
        if not value_src:
            child = parse_node(indent + 1)
            return child if child is not None else Marked(None, line)
        if value_src[0] in "[{":
            return Marked(_parse_flow(value_src, line), line)
        return Marked(_scalar(value_src), line)

    def parse_map(indent: int) -> Marked:
        nonlocal pos
        node: dict[str, Marked] = {}
        start = entries[pos][2]
        while pos < len(entries):
            ind, content, line = entries[pos]
            if ind < indent:
                break
            if ind > indent or content.startswith("- ") or content == "-":
                # A deeper line with no key above it, or a list the caller owns.
                if ind > indent and not content.startswith("-"):
                    pos += 1
                    continue
                break
            key, value_src = _split_key(content)
            pos += 1
            if key is None:
                continue
            if key == "<<":
                # Merge key. Not expanded; the nested value is consumed so the
                # next sibling is not read as its child.
                take_value(ind, line, value_src)
                continue
            node[key] = take_value(ind, line, value_src)
        return Marked(node, start)

    def parse_list(indent: int) -> Marked:
        nonlocal pos
        items: list[Marked] = []
        start = entries[pos][2]
        while pos < len(entries):
            ind, content, line = entries[pos]
            if ind != indent or not (content.startswith("- ") or content == "-"):
                break
            rest = "" if content == "-" else content[1:].strip()
            pos += 1
            if rest == "":
                child = parse_node(indent + 1)
                items.append(child if child is not None else Marked(None, line))
                continue
            key, value_src = _split_key(rest)
            # `- item` is a scalar, including `KEY=value` whose value contains a
            # colon (`- REDIS_URL=redis://cache:6379`). `- key: value` is a
            # one-entry mapping, and following keys indented under it belong to
            # that mapping. An `=` in the would-be key means this is not a key.
            if key is None or "=" in key or rest.startswith(("'", '"', "[", "{")):
                items.append(Marked(_scalar(rest), line))
                continue
            mapping: dict[str, Marked] = {}
            mapping[key] = take_value(ind, line, value_src)
            while pos < len(entries) and entries[pos][0] > indent and not (
                    entries[pos][1].startswith("- ") or entries[pos][1] == "-"):
                kind, kcontent, kline = entries[pos]
                k, vsrc = _split_key(kcontent)
                if k is None:
                    pos += 1
                    continue
                pos += 1
                if k == "<<":
                    take_value(kind, kline, vsrc)
                    continue
                mapping[k] = take_value(kind, kline, vsrc)
            items.append(Marked(mapping, line))
        return Marked(items, start)

    if not entries:
        return Marked(None, 1)
    root = parse_node(0)
    return root if root is not None else Marked(None, entries[0][2])


# ── table ────────────────────────────────────────────────────────────────────


def _shipped_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "stack_sdks.toml"


def producer_version() -> str:
    """The version of this package. Source tree wins over an older install."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        project = data.get("project") if isinstance(data, dict) else None
        if isinstance(project, dict) and project.get("name") == "graphban-cli":
            version = str(project.get("version") or "").strip()
            if version:
                return version
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("graphban-cli")
    except PackageNotFoundError:
        return "0"


def _read_table(path: Path) -> dict[str, list[dict]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MapError(f"{path.name} is not readable TOML ({exc.__class__.__name__})") from exc
    out: dict[str, list[dict]] = {"image": [], "sdk": [], "env": []}
    for group in out:
        rows = data.get(group) if isinstance(data, dict) else None
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise MapError(f"{path.name}: [{group}] must be a list of tables")
        for row in rows:
            if isinstance(row, dict) and str(row.get("id") or "").strip():
                out[group].append(row)
    return out


def load_table(repo: Path | None = None) -> dict[str, list[dict]]:
    """Shipped rows, with `.graphban/stack-sdks.toml` in front of them when present.

    Project rows are first so a package listed there wins over the shipped row.
    An empty shipped table is a broken package, not a repo that calls nothing.
    """
    path = _shipped_path()
    if not path.is_file():
        raise MapError("known-SDK table is missing from the gban package")
    shipped = _read_table(path)
    if not shipped["sdk"] or not shipped["image"]:
        raise MapError("known-SDK table shipped with no rows")
    if repo is None:
        return shipped
    overlay = repo / ".graphban" / "stack-sdks.toml"
    if not overlay.is_file():
        return shipped
    extra = _read_table(overlay)
    return {group: extra[group] + shipped[group] for group in shipped}


def _norm_package(name: str) -> str:
    name = name.strip().strip("'\"")
    name = _PKG_SPLIT.split(name, maxsplit=1)[0]
    return name.strip().lower()


def match_sdk(package: str, rows: list[dict]) -> dict | None:
    """The row whose package list or prefix matches. Exact names win over prefixes."""
    pkg = _norm_package(package)
    if not pkg:
        return None
    for row in rows:
        names = [_norm_package(str(p)) for p in (row.get("packages") or [])]
        if pkg in names:
            return row
    for row in rows:
        prefix = str(row.get("prefix") or "").lower()
        if prefix and pkg.startswith(prefix):
            return row
    return None


def image_ref_name(ref: str) -> str:
    """Image name without tag or digest. `pgvector/pgvector:pg16` → `pgvector/pgvector`."""
    ref = ref.strip().strip("'\"")
    ref = ref.split("@", 1)[0]
    if "/" in ref:
        last = ref.rsplit("/", 1)[-1]
        if ":" in last:
            ref = ref[:ref.rfind(":")]
    elif ":" in ref:
        ref = ref.rsplit(":", 1)[0]
    return ref.lower()


def match_image(ref: str, rows: list[dict]) -> dict | None:
    name = image_ref_name(ref)
    if not name:
        return None
    last = name.rsplit("/", 1)[-1]
    for row in rows:
        names = [str(n).lower() for n in (row.get("names") or [])]
        if name in names:
            return row
    for row in rows:
        names = [str(n).lower() for n in (row.get("names") or [])]
        if last in names:
            return row
    return None


def match_env(name: str, rows: list[dict]) -> dict | None:
    for row in rows:
        if name in [str(n) for n in (row.get("names") or [])]:
            return row
    return None


# ── discovery ────────────────────────────────────────────────────────────────


def _rel(path: Path, repo: Path) -> str:
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return path.name
    text = rel.as_posix()
    return "" if text == "." else text


def _skipped(path: Path, repo: Path) -> bool:
    try:
        parts = path.resolve().relative_to(repo.resolve()).parts
    except ValueError:
        return True
    return any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in parts)


def _iter_files(repo: Path):
    """Files under `repo`, pruning dependency and VCS directories.

    Each directory is sorted so two runs walk in the same order when the
    filesystem does not.
    """
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SKIP_DIRS and not name.endswith(".egg-info"))
        base = Path(dirpath)
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                continue
            yield path


def _is_compose(path: Path) -> bool:
    name = path.name.lower()
    if name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        return True
    return (name.startswith(("docker-compose.", "compose."))
            and name.endswith((".yml", ".yaml")))


def _is_dockerfile(path: Path) -> bool:
    name = path.name.lower()
    return name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile")


def _is_manifest(path: Path) -> bool:
    name = path.name.lower()
    if name in ("package.json", "pyproject.toml", "go.mod", "cargo.toml", "gemfile"):
        return True
    return name.startswith("requirements") and name.endswith(".txt")


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _line_of(text: str, package: str) -> int:
    token = re.compile(
        rf"(?i)(^|[^A-Za-z0-9_.@/-]){re.escape(package)}([^A-Za-z0-9_.@/-]|$)")
    for lineno, line in enumerate(text.splitlines(), 1):
        if token.search(line):
            return lineno
    return 1


def packages_in(path: Path, text: str) -> list[tuple[str, int]]:
    """`(package, line)` declared by this manifest. Names only — no versions."""
    name = path.name.lower()
    if name == "package.json":
        return _packages_json(text)
    if name == "pyproject.toml":
        return _packages_pyproject(text)
    if name == "cargo.toml":
        return _packages_cargo(text)
    if name == "go.mod":
        return _packages_gomod(text)
    if name == "gemfile":
        return _packages_gemfile(text)
    if name.startswith("requirements") and name.endswith(".txt"):
        return _packages_requirements(text)
    return []


def _packages_requirements(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "http:", "https:", "git+")):
            continue
        pkg = _norm_package(line)
        if pkg:
            out.append((pkg, lineno))
    return out


def _packages_json(text: str) -> list[tuple[str, int]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            names.extend(str(name) for name in block)
    return [(name, _line_of(text, name)) for name in names]


def _packages_pyproject(text: str) -> list[tuple[str, int]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    names: list[str] = []
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    for dep in project.get("dependencies") or []:
        if isinstance(dep, str):
            names.append(_norm_package(dep))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                names.extend(_norm_package(d) for d in group if isinstance(d, str))
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    for key in ("dependencies", "dev-dependencies"):
        block = poetry.get(key)
        if isinstance(block, dict):
            names.extend(str(name) for name in block if name != "python")
    return [(name, _line_of(text, name)) for name in names if name]


def _packages_cargo(text: str) -> list[tuple[str, int]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    names: list[str] = []
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            names.extend(str(name) for name in block)
    return [(name, _line_of(text, name)) for name in names]


def _packages_gomod(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    in_block = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line.startswith(")"):
            in_block = False
            continue
        if line.startswith("require "):
            parts = line.split()
            if len(parts) >= 2 and parts[1] != "(":
                out.append((parts[1], lineno))
            continue
        if in_block:
            parts = line.split()
            if parts and "/" in parts[0]:
                out.append((parts[0], lineno))
    return out


def _packages_gemfile(text: str) -> list[tuple[str, int]]:
    pattern = re.compile(r"""gem\s+['"]([^'"]+)['"]""")
    return [(match.group(1), lineno)
            for lineno, line in enumerate(text.splitlines(), 1)
            for match in pattern.finditer(line)]


def literal_hosts(value: str) -> list[tuple[str, str]]:
    """`(protocol, host)` from literal URLs in a template value.

    Interpolation is removed before the scan, and a default that is itself a
    URL (`${VAR:-https://api.example.com}`) is scanned on its own. Userinfo,
    path and query are discarded here. The original string is not returned.
    """
    if not value or "://" not in value and "://" not in value:
        # Still scan defaults: the `://` may live only inside `${VAR:-...}`.
        pass
    extras: list[str] = []

    def repl(match: re.Match[str]) -> str:
        body = match.group(1)
        for sep in (":-", "-"):
            if sep in body:
                extras.append(body.split(sep, 1)[1])
                break
        return ""

    plain = _INTERP.sub(repl, value)
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in (plain, *extras):
        for match in _URL.finditer(chunk):
            raw = match.group(0).rstrip(".,);]")
            parsed = urlparse(raw)
            host = parsed.hostname
            if not host:
                continue
            scheme = (parsed.scheme or "").split("+", 1)[0].lower()
            protocol = _SCHEME.get(scheme, scheme or "tcp")
            pair = (protocol, host)
            if pair not in seen:
                seen.add(pair)
                found.append(pair)
    return found


def _interesting_env(name: str) -> bool:
    return bool(_IDENT.fullmatch(name)) and (
        name.endswith("_API_KEY") or name.endswith("_URL"))


# ── record ───────────────────────────────────────────────────────────────────


def _provenance(file: str, rule: str, line: int | None) -> dict:
    out: dict = {"file": file, "rule": rule}
    if isinstance(line, int) and line >= 1:
        out["line"] = line
    return out


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _as_names(node: Marked | None) -> list[tuple[str, int]]:
    if node is None or node.value is None:
        return []
    if isinstance(node.value, str) and node.value.strip():
        return [(node.value.strip(), node.line)]
    if isinstance(node.value, list):
        names: list[tuple[str, int]] = []
        for item in node.value:
            if not isinstance(item, Marked):
                continue
            if isinstance(item.value, str) and item.value.strip():
                names.append((item.value.strip(), item.line))
            elif isinstance(item.value, dict):
                for key in item.value:
                    names.append((str(key), item.line))
        return names
    if isinstance(node.value, dict):
        return [(str(key), child.line if isinstance(child, Marked) else node.line)
                for key, child in node.value.items()]
    return []


def _as_env(node: Marked | None) -> list[tuple[str, str, int]]:
    """`(name, raw_value, line)`. The raw value is for host extraction only."""
    if node is None or node.value is None:
        return []
    pairs: list[tuple[str, str, int]] = []
    if isinstance(node.value, dict):
        for key, child in node.value.items():
            if not isinstance(child, Marked):
                continue
            raw = child.value if isinstance(child.value, str) else ""
            pairs.append((str(key), raw or "", child.line))
        return pairs
    if isinstance(node.value, list):
        for item in node.value:
            if not isinstance(item, Marked) or not isinstance(item.value, str):
                continue
            text = item.value.strip()
            if not text:
                continue
            if "=" in text:
                name, _, raw = text.partition("=")
                pairs.append((name.strip(), raw, item.line))
            else:
                pairs.append((text, "", item.line))
    return pairs


def _context_dir(compose: Path, repo: Path, context: str) -> str:
    raw = context.strip().strip("'\"")
    if not raw:
        raw = "."
    try:
        target = (compose.parent / raw).resolve()
        rel = target.relative_to(repo.resolve())
    except ValueError:
        text = raw.replace("\\", "/").strip("/")
        return "" if text in ("", ".") else text
    text = rel.as_posix()
    return "" if text == "." else text


def _build_context(service: Marked, compose: Path, repo: Path) -> tuple[str, int] | None:
    if not isinstance(service.value, dict):
        return None
    build = service.value.get("build")
    if not isinstance(build, Marked):
        return None
    if isinstance(build.value, str):
        return _context_dir(compose, repo, build.value), build.line
    if isinstance(build.value, dict):
        ctx = build.value.get("context")
        if isinstance(ctx, Marked) and isinstance(ctx.value, str):
            return _context_dir(compose, repo, ctx.value), ctx.line
        return _context_dir(compose, repo, "."), build.line
    return None


def _string_field(service: Marked, key: str) -> tuple[str, int] | None:
    if not isinstance(service.value, dict):
        return None
    field = service.value.get(key)
    if isinstance(field, Marked) and isinstance(field.value, str) and field.value.strip():
        return field.value.strip(), field.line
    return None


class _Builder:
    def __init__(self, repo: Path, project_id: str, table: dict[str, list[dict]]) -> None:
        self.repo = repo
        self.project_id = project_id
        self.table = table
        self.components: dict[str, dict] = {}
        self.infra: dict[str, dict] = {}
        self.external: dict[str, dict] = {}
        self.unresolved: dict[str, dict] = {}
        self.edges: dict[tuple[str, str, str], dict] = {}
        self.by_dir: dict[str, str] = {}
        self.kind_of: dict[str, str] = {}
        self.warnings: list[str] = []

    def _ids(self) -> set[str]:
        return set(self.components) | set(self.infra) | set(self.external) | set(self.unresolved)

    def _fresh(self, wanted: str) -> str:
        if wanted not in self._ids():
            return wanted
        base = f"unresolved-{wanted}"
        candidate = base
        n = 2
        while candidate in self._ids():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    def add_component(self, id_: str, directory: str, label: str, prov: dict, env: str = "") -> str:
        existing = self.by_dir.get(directory)
        if existing:
            self._note_env(self.components[existing], env)
            return existing
        id_ = id_ or (directory.rsplit("/", 1)[-1] if directory else ".")
        if id_ in self._ids():
            id_ = self._fresh(id_)
        node = {"id": id_, "dir": directory, "label": label or id_, "provenance": prov}
        if env and _IDENT.fullmatch(env):
            node["env"] = env
        self.components[id_] = node
        self.by_dir[directory] = id_
        return id_

    def add_infra(self, id_: str, label: str, kind: str, prov: dict, env: str = "") -> str:
        if id_ in self.components or id_ in self.external or id_ in self.unresolved:
            id_ = self._fresh(id_)
        node = self.infra.get(id_)
        if node is None:
            node = {"id": id_, "label": label or id_, "kind": kind, "provenance": prov}
            self.infra[id_] = node
        self.kind_of[id_] = node.get("kind") or kind
        self._note_env(node, env)
        return id_

    def add_external(self, id_: str, label: str, prov: dict, env: str = "") -> str:
        if id_ in self.components or id_ in self.infra or id_ in self.unresolved:
            id_ = self._fresh(id_)
        node = self.external.get(id_)
        if node is None:
            node = {"id": id_, "label": label or id_, "provenance": prov}
            self.external[id_] = node
        self._note_env(node, env)
        return id_

    def add_unresolved(self, name: str, prov: dict) -> str:
        # Reuse a node we already opened for this reference. A second file
        # naming the same missing service is one node, not two.
        for node in self.unresolved.values():
            if node["label"] == name:
                return node["id"]
        id_ = self._fresh(name)
        self.unresolved[id_] = {"id": id_, "label": name, "provenance": prov}
        return id_

    def add_edge(self, src: str, dst: str, protocol: str, label: str,
                 prov: dict, env: str = "") -> None:
        if not src or not dst or src == dst or not protocol:
            return
        key = (src, dst, protocol)
        edge = self.edges.get(key)
        if edge is None:
            edge = {"from": src, "to": dst, "protocol": protocol, "label": label,
                    "provenance": prov}
            self.edges[key] = edge
        self._note_env(edge, env)

    def _note_env(self, node: dict, env: str) -> None:
        if not env or not _IDENT.fullmatch(env):
            return
        current = node.get("env") or ""
        if not current or env < current:
            node["env"] = env

    def owner(self, directory: str) -> str | None:
        best: str | None = None
        best_len = -1
        for folder, id_ in self.by_dir.items():
            if folder == directory or (folder and directory.startswith(folder + "/")) or folder == "":
                # `""` is a prefix of every directory. Prefer the longest real one,
                # and only fall back to the repo root when nothing else contains it.
                if folder == "" and directory != "":
                    length = 0
                else:
                    length = len(folder) + 1
                if length > best_len:
                    best, best_len = id_, length
        return best

    def unique_kind(self, kind: str) -> str | None:
        found = [id_ for id_, have in self.kind_of.items() if have == kind]
        if len(found) == 1:
            return found[0]
        return None

    def ensure_row_node(self, row: dict, prov: dict, env: str = "") -> str:
        """The table row's node, or the one infra of that kind when there is exactly one."""
        group = str(row.get("group") or "external")
        kind = str(row.get("kind") or "")
        node_id = str(row.get("node") or row.get("id") or "")
        label = str(row.get("label") or node_id)
        if group == "infrastructure":
            existing = self.unique_kind(kind) if kind else None
            if existing:
                self._note_env(self.infra[existing], env)
                return existing
            # A compose service already named this also counts.
            if node_id in self.infra:
                self._note_env(self.infra[node_id], env)
                return node_id
            return self.add_infra(node_id, label, kind or node_id, prov, env)
        if node_id in self.external:
            self._note_env(self.external[node_id], env)
            return node_id
        return self.add_external(node_id, label, prov, env)

    def node_for_host(self, host: str, protocol: str, prov: dict, env: str,
                      exact: dict | None) -> str:
        if host in self._ids() or host in self.kind_of:
            return host if host in self._ids() else host
        # A compose service is the thing a URL host usually names (`@db`).
        for pool in (self.components, self.infra, self.external, self.unresolved):
            if host in pool:
                self._note_env(pool[host], env)
                return host
        folded = {name.lower(): name for name in list(self.components) + list(self.infra)}
        if host.lower() in folded:
            return folded[host.lower()]
        if host in _LOCAL_HOSTS:
            if exact is not None:
                return self.ensure_row_node(exact, prov, env)
            kind = protocol if protocol in {str(r.get("kind") or "") for r in self.table["image"]} else ""
            found = self.unique_kind(kind) if kind else None
            if found:
                return found
        if "." in host:
            return self.add_external(host, host, prov, env)
        return self.add_unresolved(host, prov)

    def apply_env_name(self, name: str, raw: str, line: int, file: str,
                       sources: list[str], rule: str) -> None:
        if not _interesting_env(name):
            return
        # `raw` is read for hosts and then dropped. It is not stored.
        hosts = literal_hosts(raw) if name.endswith("_URL") else []
        exact = match_env(name, self.table["env"])
        prov = _provenance(file, rule, line)
        if hosts:
            for protocol, host in hosts:
                target = self.node_for_host(host, protocol, prov, name, exact)
                for src in sources:
                    self.add_edge(src, target, protocol, name, prov, name)
            return
        if exact is not None:
            target = self.ensure_row_node(exact, prov, name)
            protocol = str(exact.get("protocol") or "tcp")
            for src in sources:
                self.add_edge(src, target, protocol, name, prov, name)
            return
        if name.endswith("_API_KEY"):
            target = self._from_api_key(name, prov)
            for src in sources:
                self.add_edge(src, target, "https", name, prov, name)

    def _from_api_key(self, name: str, prov: dict) -> str:
        tokens = [part.lower() for part in name.split("_") if part and part.upper() != "API"]
        tokens = [part for part in tokens if part != "key"]
        known = {}
        for row in self.table["sdk"]:
            node = str(row.get("node") or "")
            if node:
                known[node.lower()] = row
        for token in tokens:
            row = known.get(token)
            if row is not None:
                return self.ensure_row_node(row, prov, name)
        prefix = tokens[0] if tokens else name.lower()
        label = prefix.replace("-", " ").title()
        return self.add_external(prefix, label, prov, name)

    def finish(self, sha: str) -> dict:
        def nodes(pool: dict[str, dict]) -> list[dict]:
            return sorted(pool.values(), key=lambda n: (n["id"], n.get("dir", "")))

        edges = sorted(self.edges.values(),
                       key=lambda e: (e["from"], e["to"], e["protocol"], e["label"],
                                      e["provenance"].get("file", ""),
                                      e["provenance"].get("line") or 0))
        record: dict = {
            "producer_id": PRODUCER_ID,
            "producer_version": producer_version(),
            "mapped_sha": sha,
            "components": nodes(self.components),
            "infrastructure": nodes(self.infra),
            "external": nodes(self.external),
            "unresolved": nodes(self.unresolved),
            "edges": edges,
        }
        if self.project_id:
            record["project_id"] = self.project_id
        _refuse_leaks(record)
        return record


def _refuse_leaks(record: dict) -> None:
    def walk(value: object) -> None:
        if isinstance(value, str):
            if _LEAK.search(value):
                raise MapError("refusing to build a record that would store a URL or an env value")
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(record)


def _service_map(root: Marked) -> dict[str, Marked] | None:
    if not isinstance(root.value, dict):
        return None
    services = root.value.get("services")
    if not isinstance(services, Marked) or not isinstance(services.value, dict):
        return None
    return {str(name): node for name, node in services.value.items() if isinstance(node, Marked)}


def _extends(service: Marked) -> tuple[str, str, int] | None:
    if not isinstance(service.value, dict):
        return None
    ext = service.value.get("extends")
    if not isinstance(ext, Marked):
        return None
    if isinstance(ext.value, str) and ext.value.strip():
        return "", ext.value.strip(), ext.line
    if isinstance(ext.value, dict):
        file_node = ext.value.get("file")
        svc = ext.value.get("service")
        file_s = file_node.value if isinstance(file_node, Marked) and isinstance(file_node.value, str) else ""
        svc_s = svc.value if isinstance(svc, Marked) and isinstance(svc.value, str) else ""
        line = file_node.line if isinstance(file_node, Marked) else ext.line
        if file_s or svc_s:
            return file_s, svc_s, line
    return None


def _walk_compose(builder: _Builder, path: Path) -> None:
    text = _read_text(path)
    if text is None:
        builder.warnings.append(f"skipped unreadable {_rel(path, builder.repo)}")
        return
    file = _rel(path, builder.repo)
    services = _service_map(parse_yaml_subset(text))
    if services is None:
        builder.warnings.append(f"skipped {file}: no services mapping")
        return
    defined = set(services)
    for name, service in services.items():
        if not isinstance(service.value, dict):
            continue
        external = _truthy(service.value.get("external").value) if isinstance(
            service.value.get("external"), Marked) else False
        extended = _extends(service)
        prov_line = service.line
        if external or (extended is not None and extended[0]):
            # `external: true` or `extends.file` names a service this file does
            # not define. Unresolved, even when an image is also written here.
            label = (extended[1] if extended and extended[1] else name)
            line = extended[2] if extended and extended[0] else prov_line
            rule = "compose.extends" if extended and extended[0] else "compose.external"
            builder.add_unresolved(label, _provenance(file, rule, line))
            continue
        build = _build_context(service, path, builder.repo)
        image = _string_field(service, "image")
        if build is not None:
            directory, line = build
            builder.add_component(
                name, directory, name, _provenance(file, "compose.build", line))
        elif image is not None:
            ref, line = image
            row = match_image(ref, builder.table["image"])
            if row is None:
                # Named after the service, never the image. The image string is
                # not stored: an unknown registry path is not a label.
                builder.add_infra(
                    name, name, "unclassified image",
                    _provenance(file, "compose.unclassified-image", line))
            else:
                builder.add_infra(
                    name, str(row.get("label") or name), str(row.get("kind") or name),
                    _provenance(file, "compose.image", line))
        else:
            # A service with neither build nor image is still a name other
            # services can depend on. Leaving it out would make those edges dangle.
            builder.add_unresolved(name, _provenance(file, "compose.service", prov_line))

    for name, service in services.items():
        if not isinstance(service.value, dict):
            continue
        src = name if name in builder._ids() else None
        if src is None:
            # Two services can build one directory. The component keeps the
            # first service's id; this service's edges still start there.
            built = _build_context(service, path, builder.repo)
            if built is not None:
                src = builder.by_dir.get(built[0])
        if src is None:
            continue
        depends = service.value.get("depends_on")
        for dep, line in _as_names(depends if isinstance(depends, Marked) else None):
            prov = _provenance(file, "compose.depends_on", line)
            if dep not in defined:
                target = builder.add_unresolved(dep, prov)
            elif dep in builder._ids():
                target = dep
            else:
                target = builder.add_unresolved(dep, prov)
            kind = builder.kind_of.get(target, "")
            protocol = kind if kind and kind != "unclassified image" else "depends_on"
            builder.add_edge(src, target, protocol, "depends_on", prov)
        env_node = service.value.get("environment")
        for ename, raw, line in _as_env(env_node if isinstance(env_node, Marked) else None):
            builder.apply_env_name(ename, raw, line, file, [src], "compose.env")


def _walk_dockerfile(builder: _Builder, path: Path) -> None:
    text = _read_text(path)
    if text is None:
        builder.warnings.append(f"skipped unreadable {_rel(path, builder.repo)}")
        return
    file = _rel(path, builder.repo)
    directory = _rel(path.parent, builder.repo)
    comp = builder.owner(directory)
    if comp is None:
        comp = builder.add_component(
            directory.rsplit("/", 1)[-1] if directory else ".", directory,
            directory.rsplit("/", 1)[-1] if directory else ".",
            _provenance(file, "dockerfile", 1))
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.upper().startswith("FROM "):
            parts = line.split()
            if len(parts) >= 2 and parts[1].lower() not in ("scratch",):
                row = match_image(parts[1], builder.table["image"])
                if row is not None:
                    prov = _provenance(file, "dockerfile.image", lineno)
                    target = builder.ensure_row_node(row, prov)
                    builder.add_edge(comp, target, str(row.get("protocol") or "tcp"),
                                     str(row.get("label") or ""), prov)


def _walk_manifest(builder: _Builder, path: Path) -> None:
    text = _read_text(path)
    if text is None:
        builder.warnings.append(f"skipped unreadable {_rel(path, builder.repo)}")
        return
    found = packages_in(path, text)
    if not found:
        return
    file = _rel(path, builder.repo)
    directory = _rel(path.parent, builder.repo)
    comp = builder.owner(directory)
    matched: list[tuple[dict, int]] = []
    for package, line in found:
        # A go module path (`github.com/foo/stripe`) matches on the full path
        # and on its last element, so a project row can name either.
        row = match_sdk(package, builder.table["sdk"])
        if row is None and "/" in package:
            row = match_sdk(package.rsplit("/", 1)[-1], builder.table["sdk"])
        if row is not None:
            matched.append((row, line))
    if not matched:
        return
    if comp is None:
        label = directory.rsplit("/", 1)[-1] if directory else "."
        comp = builder.add_component(
            label, directory, label, _provenance(file, "manifest", matched[0][1]))
    for row, line in matched:
        prov = _provenance(file, f"sdk.{row.get('id')}", line)
        target = builder.ensure_row_node(row, prov)
        builder.add_edge(comp, target, str(row.get("protocol") or "tcp"),
                         str(row.get("label") or ""), prov)


def _walk_env_template(builder: _Builder, path: Path) -> None:
    text = _read_text(path)
    if text is None:
        builder.warnings.append(f"skipped unreadable {_rel(path, builder.repo)}")
        return
    file = _rel(path, builder.repo)
    directory = _rel(path.parent, builder.repo)
    if directory == "":
        sources = sorted(builder.components)
    else:
        owner = builder.owner(directory)
        sources = [owner] if owner else []
    if not sources and builder.components:
        sources = sorted(builder.components)
    if not sources:
        # A template and no deployable yet: the repo root is the deployable
        # the names belong to, so the edges have somewhere to start.
        sources = [builder.add_component(
            ".", "", ".", _provenance(file, "env.template", 1))]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        name, _, raw_value = line.partition("=")
        name = name.strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in ("'", '"'):
            raw_value = raw_value[1:-1]
        builder.apply_env_name(name, raw_value, lineno, file, sources, "env.template")


def _sha(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MapError(f"could not read HEAD ({exc.__class__.__name__})") from exc
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not sha or sha == "unknown":
        raise MapError("no commit to map; a mapped_sha of unknown is refused")
    return sha


def find_root(start: Path) -> Path:
    start = start.expanduser().resolve()
    if not start.is_dir():
        raise MapError(f"{start} is not a directory")
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return start
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return Path(proc.stdout.strip())
    return start


def build_record(start: Path, *, project_id: str = "") -> tuple[dict, list[str]]:
    """The record, and warnings for files that were skipped.

    Two calls on the same tree return equal records. The record carries no
    timestamp and no env value.
    """
    repo = find_root(start)
    table = load_table(repo)
    builder = _Builder(repo, project_id.strip(), table)
    files = sorted(_iter_files(repo), key=lambda path: _rel(path, repo))
    for path in files:
        if _is_compose(path):
            _walk_compose(builder, path)
    for path in files:
        if _is_dockerfile(path):
            _walk_dockerfile(builder, path)
    for path in files:
        if _is_manifest(path):
            _walk_manifest(builder, path)
    for path in files:
        if path.name == ".env.example":
            _walk_env_template(builder, path)
    if not (builder.components or builder.infra or builder.external or builder.unresolved):
        raise MapError("nothing to map: no compose service, Dockerfile or known dependency")
    return builder.finish(_sha(repo)), builder.warnings


def canonical(record: dict) -> str:
    """Byte-stable JSON. Key order is sorted; lists were sorted before this."""
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def post_record(url: str, api_key: str, record: dict) -> dict:
    """One POST. The login session is not a project-write key, so it is not sent."""
    from gban.client import Client

    return Client(url, api_key=api_key).call("POST", _POST_PATH, record)
