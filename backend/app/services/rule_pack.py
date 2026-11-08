"""Signed rule-pack loader (GRPH-1267 / PRD-P63 §8, S2b, D12).

A pack is a versioned artifact built in the private rules repo: `pack.toml`, `templates/*.toml`
(one rule each), `profiles/*.toml` and `mappings/observations.toml`. This module is the
product's half of the contract: it verifies the signature, validates each template against the
**primitive contract** published here, and registers the survivors as library rules
(`rule_toggles.register`).

**Signature scheme.** The artifact is a tar archive. Its signature is a raw Ed25519 signature
over the artifact's exact bytes, checked against `TRUSTED_PUBLIC_KEYS` (raw 32-byte public keys,
hex). One flipped byte fails verification. The rules repo signs with the matching private key;
the product never holds it. With no trusted key configured every pack is refused — fail closed.

**Refusal (D12).**
- *The whole pack* only for a signature that fails to verify, or a server older than the pack's
  `min_server`. The last good pack stays in force and the reason names both versions.
- *One template* for a missing primitive, a primitive-API major mismatch, a missing required
  param, or `claims` stronger than the primitive provides. That rule is registered drawn
  UNMEASURED with the reason; the rest load. A rule is never half-enforced.

**Primitive API semver.** A minor change is additive (a new primitive, an optional param), so
older packs keep loading. A major change removes a primitive or adds a required param.

**Updates are offered, not applied (D10).** `offer` verifies a newer pack and returns what it
changes in each rule and profile; `install` applies it and takes the acting `User`.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import tarfile
import tomllib
from dataclasses import dataclass, field

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import InstalledRulePack
from app.services import events as events_svc
from app.services import rule_toggles
from app.services.rules import Enforcer
from app.version import __version__

#: The primitive API this server provides. Bump the minor for additive changes, the major for
#: a removed primitive or a new required param; the pinned fixtures then fail the PR (below).
PRIMITIVE_API = "1.6.0"

#: Raw Ed25519 public keys (hex) whose signatures are trusted. The graphban-rules signing job
#: holds the matching private key; it never enters this repo. An empty tuple would refuse every pack.
TRUSTED_PUBLIC_KEYS: tuple[str, ...] = (
    # graphban-rules pack signing key (public half, raw Ed25519 hex)
    "27fbd4565cc36d610e846acf1c04ab0fb62aa3332b6b9010959e757dac3a336e",
)

STAGES = ("Spec", "Plan", "Build", "Review", "Merge", "Release", "Learn")
#: Strongest first; a template may claim no more than its primitive provides.
CLAIM_ORDER = ("enforced", "checked")
PARAM_TYPES = {"int": int, "bool": bool, "str": str}
MAX_ARTIFACT_BYTES = 5_000_000


class PackRefused(Exception):
    """The whole pack is refused (D12): a bad signature, or a server too old for it."""


# ---- the published primitive contract ----------------------------------------------------

@dataclass(frozen=True)
class Param:
    type: str
    required: bool = False


@dataclass(frozen=True)
class Primitive:
    id: str
    #: What the primitive provides: `enforced` | `checked`.
    level: str
    enforcer: Enforcer
    params: dict[str, Param] = field(default_factory=dict)


PRIMITIVES: dict[str, Primitive] = {}


def register_primitive(p: Primitive) -> None:
    """Publish a primitive in the contract. Called at import by the module that provides it."""
    if p.level not in CLAIM_ORDER:
        raise ValueError(f"primitive {p.id!r}: level must be one of {CLAIM_ORDER}")
    for name, spec in p.params.items():
        if spec.type not in PARAM_TYPES:
            raise ValueError(f"primitive {p.id!r} param {name!r}: type must be one of "
                             f"{tuple(PARAM_TYPES)}")
    PRIMITIVES[p.id] = p


register_primitive(Primitive(
    "sabotage_receipt", "enforced",
    Enforcer("graphban_source", "app/services/items.py"),
    {"min_effort": Param("int"), "min_tests_failed": Param("int")}))
register_primitive(Primitive(
    "gap_followup_link", "enforced",
    Enforcer("graphban_source", "app/services/fleet.py")))
register_primitive(Primitive(
    "reviewer_property", "enforced",
    Enforcer("graphban_source", "app/services/fleet.py"),
    {"property": Param("str")}))
register_primitive(Primitive(
    "evidence_required", "enforced",
    Enforcer("graphban_source", "app/services/items.py"),
    {"path": Param("str"), "engine": Param("str")}))
# GRPH-1273. `checked`: the server refuses nothing; `gbfleet until` reads the toggle each cycle
# and staffs every unheld review row visible to its key, so the enforcer is the supervisor.
register_primitive(Primitive(
    "staffing_scope", "checked",
    Enforcer("supervisor", "gbfleet until")))
register_primitive(Primitive(
    "reopen_on_red", "enforced",
    Enforcer("graphban_source", "app/services/fleet.py")))
register_primitive(Primitive(
    "human_only_clause", "enforced",
    Enforcer("graphban_source", "app/services/fleet.py")))
# GRPH-1405. `checked`: nothing refuses on the mode itself; the delegation brief states it and
# the operating loop / merge gates (GRPH-1403, GRPH-1404) read it. `mode` is one of
# `rule_toggles.TEST_SCOPE_MODES`, enforced when a person sets the param.
register_primitive(Primitive(
    "test_scope", "checked",
    Enforcer("doc", "docs/rules.md"),
    {"mode": Param("str")}))
# GRPH-1425. `checked`: the supervisor reads `mode` (`hold` | `warn`) when it decides whether to
# open a PR; the merge-time `covering` check refuses an uncovered file in either mode.
register_primitive(Primitive(
    "undeclared_files", "checked",
    Enforcer("supervisor", "gbfleet until"),
    {"mode": Param("str")}))
register_primitive(Primitive(
    "evidence_attached", "checked",
    Enforcer("graphban_source", "app/services/items.py"),
    {"kinds": Param("str")}))


def contract() -> dict:
    """The primitive contract as data, for the rules repo's pack CI to load templates against."""
    return {
        "api_version": PRIMITIVE_API,
        "primitives": [
            {"id": p.id, "level": p.level,
             "params": [{"name": n, "type": s.type, "required": s.required}
                        for n, s in sorted(p.params.items())]}
            for p in sorted(PRIMITIVES.values(), key=lambda p: p.id)],
    }


# ---- versions ----------------------------------------------------------------------------

def _ver(text: object) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(text).split("."))
    except ValueError:
        raise PackRefused(f"unreadable version {text!r}") from None


# ---- the loaded pack ---------------------------------------------------------------------

@dataclass
class Pack:
    name: str
    version: str
    primitive_api: str
    min_server: str
    #: Every template as the pack wrote it, by rule id.
    templates: dict[str, dict]
    #: rule id -> reason, for templates refused at load.
    refused: dict[str, str]
    profiles: dict[str, dict]
    mappings: list[dict]


#: The last good pack: what is in force. A refused pack never replaces it.
CURRENT: Pack | None = None


def verify(artifact: bytes, signature: bytes, trusted_keys: tuple[str, ...] | None = None) -> None:
    keys = TRUSTED_PUBLIC_KEYS if trusted_keys is None else trusted_keys
    if not keys:
        raise PackRefused("no trusted public key is configured, so no pack signature can verify")
    for k in keys:
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(k)).verify(signature, artifact)
            return
        except (InvalidSignature, ValueError):
            continue
    raise PackRefused("pack signature does not verify against any trusted key")


def _files(artifact: bytes) -> dict[str, bytes]:
    if len(artifact) > MAX_ARTIFACT_BYTES:
        raise PackRefused("pack artifact is too large")
    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(artifact)) as tf:
            for m in tf.getmembers():
                if m.isfile():
                    out[m.name.lstrip("./")] = tf.extractfile(m).read()
    except tarfile.TarError as e:
        raise PackRefused(f"pack artifact is not a readable archive: {e}") from None
    return out


def _toml(files: dict[str, bytes], name: str) -> dict:
    try:
        return tomllib.loads(files[name].decode())
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PackRefused(f"{name} is missing or unreadable: {e}") from None


def _check_template(t: dict, api: str) -> str:
    """The reason a template is refused, or "" when it loads."""
    for key in ("id", "stage", "title", "summary", "primitive", "claims"):
        if not isinstance(t.get(key), str) or not t[key]:
            return f"template field {key!r} is missing"
    if t["stage"] not in STAGES:
        return f"unknown stage {t['stage']!r}"
    if t["claims"] not in CLAIM_ORDER:
        return f"unknown claim {t['claims']!r}"
    if t.get("default", "off") not in ("on", "off"):
        return "default must be 'on' or 'off'"
    if _ver(api)[0] != _ver(PRIMITIVE_API)[0]:
        return (f"primitive API major mismatch: the pack targets {api}, "
                f"this server provides {PRIMITIVE_API}")
    prim = PRIMITIVES.get(t["primitive"])
    if prim is None:
        return f"primitive {t['primitive']!r} does not exist in primitive API {PRIMITIVE_API}"
    if CLAIM_ORDER.index(t["claims"]) < CLAIM_ORDER.index(prim.level):
        return (f"claims {t['claims']!r} but primitive {prim.id!r} only provides "
                f"{prim.level!r}")
    params = t.get("params", {})
    if not isinstance(params, dict):
        return "params must be a table"
    for name, v in params.items():
        spec = prim.params.get(name)
        if spec is None:
            return f"param {name!r} is not a param of primitive {prim.id!r}"
        want = PARAM_TYPES[spec.type]
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            return f"param {name!r} must be {spec.type}"
    missing = sorted(n for n, s in prim.params.items() if s.required and n not in params)
    if missing:
        return f"required param(s) {', '.join(missing)} of primitive {prim.id!r} not set"
    return ""


def load(artifact: bytes, signature: bytes, *, trusted_keys: tuple[str, ...] | None = None,
         server_version: str | None = None) -> Pack:
    """Verify and validate a pack without installing it. Raises `PackRefused` for the
    whole-pack refusals; per-template refusals are in `Pack.refused`."""
    verify(artifact, signature, trusted_keys)
    files = _files(artifact)
    meta = _toml(files, "pack.toml")
    for key in ("name", "version", "primitive_api", "min_server"):
        if not isinstance(meta.get(key), str):
            raise PackRefused(f"pack.toml has no {key!r}")
    server = server_version or __version__
    if _ver(server) < _ver(meta["min_server"]):
        raise PackRefused(
            f"pack {meta['name']} {meta['version']} needs server {meta['min_server']} or newer; "
            f"this server is {server}")
    pack = Pack(meta["name"], meta["version"], meta["primitive_api"], meta["min_server"],
                {}, {}, {}, [])
    seen: dict[str, list[str]] = {}
    for path in sorted(files):
        if path.startswith("templates/") and path.endswith(".toml"):
            try:
                t = tomllib.loads(files[path].decode())
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                # One unreadable template is that rule's problem, not the pack's (D12).
                pack.templates[path] = {}
                pack.refused[path] = f"template does not parse: {e}"
                continue
            tid = t.get("id") if isinstance(t.get("id"), str) and t["id"] else path
            seen.setdefault(tid, []).append(path)
            pack.templates[tid] = t
            reason = _check_template(t, pack.primitive_api)
            if reason:
                pack.refused[tid] = reason
        elif path.startswith("profiles/") and path.endswith(".toml"):
            p = _toml(files, path)
            pack.profiles[p.get("id", path)] = p
    for tid, paths in seen.items():
        if len(paths) > 1:
            # Neither half loads: every template sharing the id is refused, naming the clash.
            pack.refused[tid] = f"duplicate template id {tid!r} in {', '.join(paths)}"
    if "mappings/observations.toml" in files:
        pack.mappings = _toml(files, "mappings/observations.toml").get("map", [])
    return pack


def _library_rule(tid: str, t: dict, reason: str) -> rule_toggles.LibraryRule:
    prim = PRIMITIVES.get(t.get("primitive", ""))
    ok = not reason
    return rule_toggles.LibraryRule(
        id=tid, stage=str(t.get("stage", "")).lower(), title=str(t.get("title") or tid),
        summary=t.get("summary", ""),
        enforcement=prim.level if ok else "unmeasured",
        enforcer=prim.enforcer if prim else Enforcer("doc", "docs/data-model.md"),
        default_on=ok and t.get("default") == "on",
        default_params=dict(t.get("params", {})) if ok else {},
        unmeasured_reason=reason)


def install(db: Session, actor: object, artifact: bytes, signature: bytes, *,
            trusted_keys: tuple[str, ...] | None = None,
            server_version: str | None = None) -> Pack:
    """Apply a pack: a person's request (D10). A refused pack raises and leaves `CURRENT`
    and the library exactly as they were."""
    global CURRENT
    rule_toggles._require_person(db, actor)
    pack = load(artifact, signature, trusted_keys=trusted_keys, server_version=server_version)
    rules = [_library_rule(tid, t, pack.refused.get(tid, ""))
             for tid, t in pack.templates.items()]
    rule_toggles.LIBRARY.clear()
    for r in rules:
        rule_toggles.register(r)
    CURRENT = pack
    return pack


# ---- offered updates ---------------------------------------------------------------------

def _changes(old: dict, new: dict) -> dict:
    keys = sorted(set(old) | set(new))
    return {k: {"from": old.get(k), "to": new.get(k)} for k in keys if old.get(k) != new.get(k)}


def diff(old: Pack | None, new: Pack) -> dict:
    """What `new` changes against the adopted pack, per rule and per profile."""
    def side(a: dict, b: dict) -> dict:
        return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                "changed": {k: _changes(a[k], b[k]) for k in sorted(set(a) & set(b))
                            if a[k] != b[k]}}
    ot, op = (old.templates, old.profiles) if old else ({}, {})
    return {"from": old.version if old else None, "to": new.version,
            "rules": side(ot, new.templates), "profiles": side(op, new.profiles),
            "newly_refused": {k: v for k, v in new.refused.items()
                              if k not in (old.refused if old else {})}}


def offer(artifact: bytes, signature: bytes, *, trusted_keys: tuple[str, ...] | None = None,
          server_version: str | None = None) -> dict:
    """Verify a newer pack and say what it would change. Nothing is applied (D10)."""
    return diff(CURRENT, load(artifact, signature, trusted_keys=trusted_keys,
                              server_version=server_version))


# ---- install on a server: offer, accept, persist, reload (GRPH-1329 / D10, D12) ----------

#: Why the persisted pack is not in force, when it is not. The Library group reads this in
#: place of "no rule pack loaded". "" when a pack is in force or none was ever installed.
LOAD_REFUSAL = ""

#: sha256 of the artifact in force in THIS process, so a worker can tell that another worker
#: accepted a newer pack (`sync`). "" when none is.
CURRENT_SHA = ""

#: sha256 of the persisted pack this process refused at reload, so `sync` does not retry it.
_REFUSED_SHA = ""


class PackMismatch(Exception):
    """Accept was given a different artifact than the one that was offered."""


def sha256_of(artifact: bytes) -> str:
    return hashlib.sha256(artifact).hexdigest()


def offer_pack(db: Session, actor: object, artifact: bytes, signature: bytes, *,
               trusted_keys: tuple[str, ...] | None = None,
               server_version: str | None = None) -> dict:
    """A person's Offer: the diff against the pack in force plus the artifact's sha256, which
    Accept must echo. Applies nothing, persists nothing."""
    rule_toggles._require_person(db, actor)
    return {**offer(artifact, signature, trusted_keys=trusted_keys, server_version=server_version),
            "sha256": sha256_of(artifact)}


def accept(db: Session, actor: object, artifact: bytes, signature: bytes, sha256: str, *,
           trusted_keys: tuple[str, ...] | None = None,
           server_version: str | None = None) -> dict:
    """A person's Accept (the only path that installs a pack). Pinned to the offered sha256, so
    what was previewed is what is applied. Persists, installs, and writes one audit event."""
    global LOAD_REFUSAL, CURRENT_SHA
    user = rule_toggles._require_person(db, actor)
    actual = sha256_of(artifact)
    if actual != (sha256 or "").lower():
        raise PackMismatch(f"artifact sha256 {actual} is not the offered {sha256!r}; "
                           "offer it again")
    old = CURRENT.version if CURRENT else None
    # Verifies the signature and server version first: a refused pack raises here and the
    # previous pack, in memory and in the table, stays in force.
    pack = install(db, user, artifact, signature, trusted_keys=trusted_keys,
                   server_version=server_version)
    db.add(InstalledRulePack(name=pack.name, version=pack.version, sha256=actual,
                             artifact=artifact, signature=signature, installed_by=user.id))
    db.commit()
    LOAD_REFUSAL = ""
    CURRENT_SHA = actual
    events_svc.record_user(
        db, user, action="install_rule_pack", target_type="rule_pack", target_id=pack.name,
        meta={"old_version": old, "new_version": pack.version, "sha256": actual})
    return status(db)


def _latest(db: Session) -> InstalledRulePack | None:
    return db.scalars(select(InstalledRulePack)
                      .order_by(InstalledRulePack.id.desc()).limit(1)).first()


def _latest_sha(db: Session) -> str | None:
    return db.scalars(select(InstalledRulePack.sha256)
                      .order_by(InstalledRulePack.id.desc()).limit(1)).first()


def sync(db: Session) -> None:
    """Bring this worker up to the installed pack when another worker accepted a newer one
    since this one started. One indexed read; reloads only on a change, and never retries a
    pack this process already refused."""
    sha = _latest_sha(db)
    if sha and sha != CURRENT_SHA and sha != _REFUSED_SHA:
        reload_persisted(db)


def status(db: Session) -> dict:
    """The installed pack, or the server's own words for why there is none. `loaded` says
    whether it is in force in this process."""
    sync(db)
    row = _latest(db)
    if row is None:
        return {"installed": False, "loaded": False, "name": None, "version": None,
                "sha256": None, "installed_at": None, "installed_by": None,
                "reason": "No rule pack has been installed on this server"}
    loaded = CURRENT is not None and CURRENT_SHA == row.sha256
    reason = LOAD_REFUSAL
    if not loaded and not reason:
        # Installed but not in force and no word why: say so rather than read as all clear.
        reason = (f"installed pack {row.name} {row.version} is not in force on this server "
                  "and no reason was recorded")
    return {"installed": True, "loaded": loaded,
            "name": row.name, "version": row.version, "sha256": row.sha256,
            "installed_at": row.installed_at, "installed_by": row.installed_by,
            "reason": reason,
            "warning": None if loaded else reason}


def compat(db: Session, tag: str, contract_api: str | None) -> dict | None:
    """Will the release `tag` still load the installed pack? `compatible`, `refused` (with the
    reason) or `unknown` (with the reason); None when no pack is installed. Unknown is never
    compatible. Reports only: nothing here changes what is in force (D10)."""
    row = _latest(db)
    if row is None:
        return None
    try:
        meta = _toml(_files(row.artifact), "pack.toml")
        need, api = str(meta["min_server"]), str(meta["primitive_api"])
        if _ver(tag) < _ver(need):
            return {"state": "refused",
                    "reason": f"installed pack {row.name} {row.version} needs server {need} "
                              f"or newer; the available release is {tag}"}
        if not contract_api:
            return {"state": "unknown",
                    "reason": f"release {tag} did not publish its rule-primitive contract"}
        if _ver(api)[0] != _ver(contract_api)[0]:
            return {"state": "refused",
                    "reason": f"installed pack {row.name} {row.version} targets primitive API "
                              f"{api}; release {tag} provides {contract_api}"}
    except (PackRefused, KeyError, ValueError, tarfile.TarError) as e:
        return {"state": "unknown", "reason": f"could not read the installed pack: {e}"}
    return {"state": "compatible", "reason": ""}


def reload_persisted(db: Session) -> Pack | None:
    """Startup, in every worker: re-verify the persisted pack and register its library. One
    that no longer verifies, or needs a newer server, is refused with its reason (`LOAD_REFUSAL`)
    and registers nothing."""
    global CURRENT, LOAD_REFUSAL, CURRENT_SHA, _REFUSED_SHA
    row = _latest(db)
    if row is None:
        return None
    try:
        pack = load(row.artifact, row.signature)
    except PackRefused as e:
        LOAD_REFUSAL = (f"installed pack {row.name} {row.version} is not in force: {e}")
        _REFUSED_SHA = row.sha256
        return None
    rule_toggles.LIBRARY.clear()
    for tid, t in pack.templates.items():
        rule_toggles.register(_library_rule(tid, t, pack.refused.get(tid, "")))
    CURRENT = pack
    CURRENT_SHA = row.sha256
    LOAD_REFUSAL = ""
    _REFUSED_SHA = ""
    return pack


# ---- discover and fetch packs from the releases feed (GRPH-1334 / D10) -------------------

logger = logging.getLogger(__name__)

_TAG_PREFIX = "pack-v"
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
_FEED_TIMEOUT = 5.0
#: A definite feed answer is reused this long; `refresh` bypasses it. A feed failure is
#: never cached, so the next read retries.
FEED_TTL = 60.0
_available_cache: dict[str, tuple[float, dict]] = {}


class PackFeedError(Exception):
    """The releases feed, or an asset it lists, could not be read."""


def _get(url: str, *, accept: str | None = None) -> httpx.Response:
    headers = {"User-Agent": "graphban-api"}
    if accept:
        headers["Accept"] = accept
    try:
        resp = httpx.get(url, headers=headers, timeout=_FEED_TIMEOUT, follow_redirects=True)
    except (httpx.HTTPError, OSError) as e:
        raise PackFeedError(f"could not reach {url}: {e}") from e
    if resp.status_code != 200:
        raise PackFeedError(f"{url} returned {resp.status_code}")
    return resp


def _releases() -> list[dict]:
    resp = _get(settings.rule_pack_feed_url, accept="application/vnd.github+json")
    try:
        data = resp.json()
    except ValueError as e:
        raise PackFeedError(f"the releases feed is not JSON: {e}") from e
    if not isinstance(data, list):
        raise PackFeedError("the releases feed is not a list of releases")
    out = []
    for rel in data:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        if tag.startswith(_TAG_PREFIX) and _VERSION_RE.match(tag[len(_TAG_PREFIX):]):
            out.append(rel)
    out.sort(key=lambda r: _ver(str(r["tag_name"])[len(_TAG_PREFIX):]), reverse=True)
    return out


def _download(rel: dict, version: str) -> tuple[bytes, bytes]:
    tar_name, sig_name = f"graphban-rules-{version}.tar", f"graphban-rules-{version}.tar.sig"
    urls = {tar_name: "", sig_name: ""}
    for a in rel.get("assets") or []:
        if isinstance(a, dict) and a.get("name") in urls:
            urls[a["name"]] = str(a.get("browser_download_url") or "")
    for name, url in urls.items():
        if not url:
            raise PackFeedError(f"release {rel.get('tag_name')} has no asset {name}")
    artifact, sig = _get(urls[tar_name]).content, _get(urls[sig_name]).content
    if len(artifact) > MAX_ARTIFACT_BYTES:
        raise PackRefused("pack artifact is too large")
    return artifact, sig


def fetch_version(version: str) -> tuple[bytes, bytes]:
    """The artifact and signature published for `version`. Unverified: callers load them."""
    version = (version or "").strip()
    if not _VERSION_RE.match(version):
        raise PackRefused(f"unreadable version {version!r}")
    for rel in _releases():
        if rel["tag_name"] == _TAG_PREFIX + version:
            return _download(rel, version)
    raise PackFeedError(f"no release {_TAG_PREFIX}{version} in the feed")


def _loadable(artifact: bytes, signature: bytes, server_version: str | None) -> Pack | None:
    """The verified pack when this server can load it: min_server met and a primitive API of
    the same major. None otherwise (a pack this server cannot load is never offered)."""
    try:
        pack = load(artifact, signature, server_version=server_version)
        if _ver(pack.primitive_api)[0] != _ver(PRIMITIVE_API)[0]:
            return None
    except PackRefused:
        return None
    return pack


def available(db: Session, *, refresh: bool = False, server_version: str | None = None) -> dict:
    """The newest pack on the feed this server can load, with its notes. `state` is `current`
    (nothing newer), `available`, or `unknown` with `feed_error` (a failed feed is never
    `current`). Reads only: nothing is installed (D10)."""
    row = _latest(db)
    installed = row.version if row is not None else None
    base = {"installed_version": installed, "version": None, "notes": None, "feed_error": None}
    key = f"{settings.rule_pack_feed_url}|{server_version or __version__}"
    hit = _available_cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < FEED_TTL:
        newest = hit[1]
    else:
        try:
            newest = {}
            for rel in _releases():
                version = str(rel["tag_name"])[len(_TAG_PREFIX):]
                artifact, sig = _download(rel, version)
                pack = _loadable(artifact, sig, server_version)
                if pack is not None:
                    newest = {"version": pack.version, "notes": str(rel.get("body") or "")}
                    break
        except (PackFeedError, PackRefused, KeyError, ValueError) as e:
            logger.info("rule pack feed failed: %s", e)
            return {**base, "state": "unknown", "feed_error": str(e)}
        _available_cache[key] = (time.monotonic(), newest)
    if newest and (installed is None or _ver(newest["version"]) > _ver(installed)):
        return {**base, **newest, "state": "available"}
    return {**base, "state": "current"}
