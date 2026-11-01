"""PRD-P63 S2b (GRPH-1267): the signed rule-pack loader and the primitive contract (D12).

The pinned fixtures in `tests/fixtures/rule_pack/` are the current (`api1`) and previous
(`api0`) primitive major. They are signed here with a TEST-ONLY key; the suite needs no rules
repo. A primitive change that breaks a template fails these tests, and so the product PR.
"""
import io
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.models import User
from app.services import rule_pack as P
from app.services import rule_toggles as T

FIX = Path(__file__).parent / "fixtures" / "rule_pack"
KEY = Ed25519PrivateKey.from_private_bytes(
    bytes.fromhex((FIX / "TEST_ONLY_signing_seed.hex").read_text().strip()))
PUB = KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
TRUST = (PUB,)


def build(src: Path, edits: dict[str, str] | None = None) -> tuple[bytes, bytes]:
    """Tar a fixture directory (optionally replacing files' text) and sign it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                rel = f.relative_to(src).as_posix()
                data = (edits or {}).get(rel, f.read_text()).encode()
                ti = tarfile.TarInfo(rel)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    art = buf.getvalue()
    return art, KEY.sign(art)


@pytest.fixture(autouse=True)
def _clean():
    saved = (dict(T.LIBRARY), P.CURRENT)
    yield
    T.LIBRARY.clear()
    T.LIBRARY.update(saved[0])
    P.CURRENT = saved[1]


def _person():
    from app.db import SessionLocal
    db = SessionLocal()
    return db, db.query(User).first()


def test_current_major_fixture_loads_every_template():
    pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
    assert pack.refused == {}
    assert set(pack.templates) == {"review.sabotage_required", "review.evidence_attached",
                                   "review.distinct_vendor", "build.engine_evidence"}
    assert "standard" in pack.profiles and pack.mappings[0]["weight"] == 2


def test_previous_major_fixture_is_drawn_unmeasured_not_half_enforced():
    pack = P.load(*build(FIX / "api0"), trusted_keys=TRUST)
    assert set(pack.refused) == set(pack.templates)
    assert all("major mismatch" in r for r in pack.refused.values())


def test_enforced_claim_on_a_checked_primitive_is_refused():
    t = (FIX / "api1/templates/evidence.toml").read_text().replace(
        'claims = "checked"', 'claims = "enforced"')
    pack = P.load(*build(FIX / "api1", {"templates/evidence.toml": t}), trusted_keys=TRUST)
    assert "only provides 'checked'" in pack.refused["review.evidence_attached"]
    assert "review.sabotage_required" not in pack.refused


def test_missing_primitive_and_bad_param_are_refused_per_template():
    t = (FIX / "api1/templates/sabotage.toml").read_text().replace(
        "min_effort = 3", 'min_effort = "three"')
    e = (FIX / "api1/templates/evidence.toml").read_text().replace(
        'primitive = "evidence_attached"', 'primitive = "nope"')
    pack = P.load(*build(FIX / "api1", {"templates/sabotage.toml": t,
                                        "templates/evidence.toml": e}), trusted_keys=TRUST)
    assert "must be int" in pack.refused["review.sabotage_required"]
    assert "does not exist" in pack.refused["review.evidence_attached"]


def test_unparseable_template_is_refused_alone_and_the_pack_loads():
    pack = P.load(*build(FIX / "api1", {"templates/evidence.toml": "id = [oops"}),
                  trusted_keys=TRUST)
    assert "does not parse" in pack.refused["templates/evidence.toml"]
    assert "review.sabotage_required" not in pack.refused


def test_template_without_id_is_refused_visibly(client):
    t = (FIX / "api1/templates/evidence.toml").read_text().replace(
        'id = "review.evidence_attached"', "")
    art = build(FIX / "api1", {"templates/evidence.toml": t})
    pack = P.load(*art, trusted_keys=TRUST)
    assert "'id'" in pack.refused["templates/evidence.toml"]
    db, user = _person()
    try:
        P.install(db, user, *art, trusted_keys=TRUST)
        r = T.LIBRARY["templates/evidence.toml"]
        assert r.enforcement == "unmeasured" and "'id'" in r.unmeasured_reason
    finally:
        db.close()


def test_duplicate_template_ids_are_both_refused():
    t = (FIX / "api1/templates/evidence.toml").read_text().replace(
        'id = "review.evidence_attached"', 'id = "review.sabotage_required"')
    pack = P.load(*build(FIX / "api1", {"templates/evidence.toml": t}), trusted_keys=TRUST)
    reason = pack.refused["review.sabotage_required"]
    assert "duplicate" in reason and "evidence.toml" in reason and "sabotage.toml" in reason


def test_tampered_artifact_is_refused():
    art, sig = build(FIX / "api1")
    flipped = bytearray(art)
    flipped[len(flipped) // 2] ^= 1
    with pytest.raises(P.PackRefused, match="signature"):
        P.load(bytes(flipped), sig, trusted_keys=TRUST)


def test_no_trusted_key_fails_closed():
    with pytest.raises(P.PackRefused, match="no trusted public key"):
        P.load(*build(FIX / "api1"))


def test_newer_server_required_names_both_versions_and_last_good_stays(client):
    db, user = _person()
    try:
        good = P.install(db, user, *build(FIX / "api1"), trusted_keys=TRUST)
        newer = (FIX / "api1/pack.toml").read_text().replace("2026.10.1", "2099.1.1")
        with pytest.raises(P.PackRefused) as e:
            P.install(db, user, *build(FIX / "api1", {"pack.toml": newer}),
                      trusted_keys=TRUST, server_version="2026.10.27")
        assert "2099.1.1" in str(e.value) and "2026.10.27" in str(e.value)
        assert P.CURRENT is good and "review.sabotage_required" in T.LIBRARY
    finally:
        db.close()


def test_install_registers_refused_rule_unmeasured_with_reason(client):
    db, user = _person()
    try:
        P.install(db, user, *build(FIX / "api0"), trusted_keys=TRUST)
        r = T.LIBRARY["review.sabotage_required"]
        assert r.enforcement == "unmeasured" and "major mismatch" in r.unmeasured_reason
        P.install(db, user, *build(FIX / "api1"), trusted_keys=TRUST)
        assert T.LIBRARY["review.sabotage_required"].enforcement == "enforced"
        assert T.LIBRARY["review.sabotage_required"].default_params == {"min_effort": 3}
    finally:
        db.close()


def test_only_a_person_installs_a_pack(client):
    db, _ = _person()
    try:
        with pytest.raises(Exception) as e:
            P.install(db, ("engine", "recommend"), *build(FIX / "api1"), trusted_keys=TRUST)
        assert getattr(e.value, "rule_id", None) == T.PERSON_RULE
    finally:
        db.close()


def test_offer_diffs_without_applying(client):
    db, user = _person()
    try:
        P.install(db, user, *build(FIX / "api1"), trusted_keys=TRUST)
        t = (FIX / "api1/templates/sabotage.toml").read_text().replace("min_effort = 3",
                                                                       "min_effort = 5")
        pk = (FIX / "api1/pack.toml").read_text().replace('version = "1.0.0"',
                                                          'version = "1.1.0"')
        d = P.offer(*build(FIX / "api1", {"templates/sabotage.toml": t, "pack.toml": pk}),
                    trusted_keys=TRUST)
        assert d["from"] == "1.0.0" and d["to"] == "1.1.0"
        assert "review.sabotage_required" in d["rules"]["changed"]
        assert T.LIBRARY["review.sabotage_required"].default_params == {"min_effort": 3}
        assert P.CURRENT.version == "1.0.0"
    finally:
        db.close()


def test_contract_publishes_ids_levels_and_params():
    c = P.contract()
    assert c["api_version"] == P.PRIMITIVE_API
    by = {p["id"]: p for p in c["primitives"]}
    assert by["sabotage_receipt"]["level"] == "enforced"
    assert by["sabotage_receipt"]["params"] == [
        {"name": "min_effort", "type": "int", "required": False},
        {"name": "min_tests_failed", "type": "int", "required": False}]


def test_refused_template_reason_reaches_the_rules_flow(client):
    """GRPH-1268: the Rules page draws a refused template UNMEASURED with the loader's reason,
    so the reason must survive the REST response model, not only the in-process registry."""
    from tests.test_rule_toggles import _login
    db, user = _person()
    try:
        P.install(db, user, *build(FIX / "api0"), trusted_keys=TRUST)
    finally:
        db.close()
    auth = _login(client, "alex@ascme-labs.com")
    r = client.get("/api/rules/flow", params={"project_id": "core"}, headers=auth)
    assert r.status_code == 200, r.text
    rule = next(x for s in r.json()["stages"] for x in s["rules"]
                if x["id"] == "review.sabotage_required")
    assert rule["enforcement"] == "unmeasured" and "major mismatch" in rule["unmeasured_reason"]
