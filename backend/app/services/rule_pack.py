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

import io
import tarfile
import tomllib
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.orm import Session

from app.services import rule_toggles
from app.services.rules import Enforcer
from app.version import __version__

#: The primitive API this server provides. Bump the minor for additive changes, the major for
#: a removed primitive or a new required param; the pinned fixtures then fail the PR (below).
PRIMITIVE_API = "1.2.0"

#: Raw Ed25519 public keys (hex) whose signatures are trusted. The graphban-rules signing job
#: holds the matching private key. Empty until that key is provided: no pack loads without one.
TRUSTED_PUBLIC_KEYS: tuple[str, ...] = ()

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
