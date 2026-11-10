"""GRPH-1494: every GRPH-P74 rule can be carried by a signed pack, through the real loader.

GRPH-1424 (`build.generated_files`) and GRPH-1429 (`engines` on `build.test_scope`) were tested by
registering a `LibraryRule` directly, which skips `rule_pack.load`. On a real box the loader
refused both: one had no primitive, the other set a param its primitive did not declare. These
tests load the templates the rules repo ships (same ids, primitives, claims and params), then
write each new param as a person, and check that a pack without a rule still reads its default.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from app.models import User
from app.services import rule_pack as P
from app.services import rule_toggles as T

FIX = Path(__file__).parent / "fixtures" / "rule_pack"
KEY = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex((FIX / "TEST_ONLY_signing_seed.hex").read_text().strip()))
TRUST = (KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex(),)

PACK = 'name = "p74"\nversion = "0.0.1"\nprimitive_api = "1.8.0"\nmin_server = "2026.10.1"\n'

TEMPLATES = {
    "plan.exclusive_areas": '''id = "plan.exclusive_areas"
stage = "Plan"
title = "Exclusive file areas"
summary = "s"
primitive = "exclusive_areas"
claims = "enforced"
default = "on"
[params]
paths = ["backend/alembic/versions"]
''',
    "build.generated_files": '''id = "build.generated_files"
stage = "Build"
title = "Generated files"
summary = "s"
primitive = "generated_files"
claims = "checked"
default = "on"
[params]
paths = ["AGENTS.md", ".claude/agents/"]
''',
    "build.undeclared_files": '''id = "build.undeclared_files"
stage = "Build"
title = "Undeclared files"
summary = "s"
primitive = "undeclared_files"
claims = "checked"
default = "on"
[params]
mode = "hold"
''',
    "merge.done_predicates": '''id = "merge.done_predicates"
stage = "Merge"
title = "Predicates done requires"
summary = "s"
primitive = "done_predicates"
claims = "enforced"
default = "on"
[params]
predicates = []
''',
    "build.test_scope": '''id = "build.test_scope"
stage = "Build"
title = "Where the test suites run"
summary = "s"
primitive = "test_scope"
claims = "checked"
default = "on"
[params]
mode = "agent_and_pr"
engines = ["postgres", "sqlite"]
''',
}


def _pack(templates: dict[str, str]) -> tuple[bytes, bytes]:
    files = {"pack.toml": PACK, **{f"templates/{k}.toml": v for k, v in templates.items()}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel, text in sorted(files.items()):
            data = text.encode()
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    art = buf.getvalue()
    return art, KEY.sign(art)


@pytest.fixture()
def library(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    pack = P.load(*_pack(TEMPLATES), trusted_keys=TRUST)
    for tid, t in pack.templates.items():
        T.register(P._library_rule(tid, t, pack.refused.get(tid, "")))
    return pack


def test_every_p74_template_loads_through_the_pack_loader(library):
    assert library.refused == {}
    assert set(library.templates) == set(TEMPLATES)
    assert all(T.LIBRARY[r].enforcement in ("enforced", "checked") for r in TEMPLATES)


def test_a_person_sets_engines_and_generated_paths_after_a_pack_load(client, library):
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        user = db.query(User).first()
        T.set_project_toggle(db, "core", T.TEST_SCOPE_RULE,
                             {"params": {"mode": "agent_full", "engines": ["MySQL"]}},
                             actor=user, reason="this project runs on mysql")
        T.set_project_toggle(db, "core", T.GENERATED_FILES_RULE,
                             {"params": {"paths": ["client/generated/**"]}},
                             actor=user, reason="the openapi client is generated")
        db.commit()
        assert T.test_scope_engines(db, "core") == ["mysql"]
        assert T.generated_files(db, "core") == ["client/generated/**"]
    finally:
        db.close()


@pytest.mark.parametrize("read, default", [
    (lambda: T.exclusive_paths(None, None), T.EXCLUSIVE_AREAS_DEFAULT),
    (lambda: T.generated_files(None, None), T.GENERATED_FILES_DEFAULT),
    (lambda: T.undeclared_files_mode(None, None), T.UNDECLARED_FILES_DEFAULT),
    (lambda: T.test_scope_mode(None, None), T.TEST_SCOPE_DEFAULT),
    (lambda: T.test_scope_engines(None, None), T.TEST_SCOPE_ENGINES_DEFAULT),
    (lambda: T.done_predicate_names(None, None), None),
], ids=["exclusive_areas", "generated_files", "undeclared_files", "test_scope_mode",
        "test_scope_engines", "done_predicates"])
def test_a_pack_without_the_rule_reads_its_default_never_off(monkeypatch, read, default):
    """GRPH-1430: a box whose pack predates a rule behaves exactly as before it existed."""
    monkeypatch.setattr(T, "LIBRARY", {})
    if default is None:  # unset done predicates follow the deployment's list, live
        assert read() == T.deployment_predicates()
    else:
        assert list(read()) == list(default)
