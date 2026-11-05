"""PRD-P63 S2b (GRPH-1267): the signed rule-pack loader and the primitive contract (D12).

The pinned fixtures in `tests/fixtures/rule_pack/` are the current (`api1`) and previous
(`api0`) primitive major. They are signed here with a TEST-ONLY key; the suite needs no rules
repo. A primitive change that breaks a template fails these tests, and so the product PR.
"""
import base64
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
    saved = (dict(T.LIBRARY), P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA)
    yield
    T.LIBRARY.clear()
    T.LIBRARY.update(saved[0])
    P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA = saved[1:]


def _person():
    from app.db import SessionLocal
    db = SessionLocal()
    return db, db.query(User).first()


def test_current_major_fixture_loads_every_template():
    pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
    assert pack.refused == {}
    assert set(pack.templates) == {"review.sabotage_required", "review.evidence_attached",
                                   "review.distinct_vendor", "build.engine_evidence",
                                   "review.staff_every_row", "merge.reopen_on_red",
                                   "accept.human_clause_blocks"}
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
        P.load(*build(FIX / "api1"), trusted_keys=())


def test_default_trusted_key_is_the_graphban_rules_key():
    assert P.TRUSTED_PUBLIC_KEYS == (
        "27fbd4565cc36d610e846acf1c04ab0fb62aa3332b6b9010959e757dac3a336e",)


def test_throwaway_key_loads_when_trusted_and_is_refused_by_default_keys():
    art, sig = build(FIX / "api1")
    assert P.load(art, sig, trusted_keys=TRUST) is not None
    with pytest.raises(P.PackRefused,
                       match="pack signature does not verify against any trusted key"):
        P.load(art, sig)


def test_flipped_byte_fails_verify():
    art, sig = build(FIX / "api1")
    P.verify(art, sig, TRUST)
    flipped = bytearray(art)
    flipped[0] ^= 1
    with pytest.raises(P.PackRefused):
        P.verify(bytes(flipped), sig, TRUST)


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


# ---- the published contract (GRPH-1303) --------------------------------------------------

import json  # noqa: E402

CONTRACT_FILE = Path(__file__).resolve().parents[2] / "docs" / "rule-primitive-contract.json"


def test_contract_file_matches_contract():
    on_disk = json.loads(CONTRACT_FILE.read_text())
    assert on_disk == json.loads(json.dumps(P.contract(), sort_keys=True)), (
        "docs/rule-primitive-contract.json is stale: run scripts/gen_rule_contract.py")
    assert CONTRACT_FILE.read_text() == json.dumps(P.contract(), indent=2, sort_keys=True) + "\n"
    assert on_disk["api_version"] == P.PRIMITIVE_API


def test_contract_file_goes_stale_when_a_param_changes(monkeypatch):
    prim = next(p for p in P.PRIMITIVES.values() if p.params)
    name = next(iter(prim.params))
    old = prim.params[name]
    monkeypatch.setitem(prim.params, name, P.Param("bool" if old.type != "bool" else "str"))
    assert json.loads(CONTRACT_FILE.read_text()) != json.loads(json.dumps(P.contract()))


def test_contract_route(client):
    from tests.test_rule_toggles import _login
    _person()[0].close()
    auth = _login(client, "alex@ascme-labs.com")
    r = client.get("/api/rules/contract", headers=auth)
    assert r.status_code == 200
    assert r.json() == json.loads(CONTRACT_FILE.read_text())
    assert r.json()["api_version"] == P.PRIMITIVE_API
    assert client.get("/api/rules/contract").status_code == 401


def test_contract_route_answers_an_agent_api_key(client, auth):
    """Pack CI holds a key, not a login: the route must answer `X-API-Key` (GRPH-1303 bounce)."""
    key = client.post("/api/api-keys", json={"name": "pack-ci"}, headers=auth).json()
    for headers in ({"X-API-Key": key["plaintext"]},
                    {"Authorization": f"Bearer {key['plaintext']}"}):
        r = client.get("/api/rules/contract", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == json.loads(CONTRACT_FILE.read_text())
        assert r.json()["api_version"] == P.PRIMITIVE_API
    assert client.get("/api/rules/contract",
                      headers={"X-API-Key": "not-a-key"}).status_code == 401


# ---- install on a server (GRPH-1329) -----------------------------------------------------


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com",
                                             "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _body(art, sig, **extra):
    return {"artifact_b64": base64.b64encode(art).decode(),
            "signature_b64": base64.b64encode(sig).decode(), **extra}


@pytest.fixture()
def trusted(monkeypatch):
    monkeypatch.setattr(P, "TRUSTED_PUBLIC_KEYS", TRUST)
    _forget()


def _forget():
    """What a fresh process holds: no library, no pack, nothing refused."""
    T.LIBRARY.clear()
    P.CURRENT, P.CURRENT_SHA, P.LOAD_REFUSAL, P._REFUSED_SHA = None, "", "", ""


def _restart():
    """Run the app's real startup again (the lifespan), as a new worker process would."""
    from fastapi.testclient import TestClient
    from app.main import app
    _forget()
    with TestClient(app):
        pass


def test_offer_applies_nothing_then_accept_installs_and_survives_restart(client, trusted):
    h = _login(client)
    art, sig = build(FIX / "api1")
    r = client.post("/api/rules/pack/offer", json=_body(art, sig), headers=h)
    assert r.status_code == 200, r.text
    offered = r.json()
    assert offered["from"] is None and offered["rules"]["added"]
    assert offered["sha256"] == P.sha256_of(art)
    st = client.get("/api/rules/pack", headers=h).json()
    assert st["installed"] is False and "No rule pack" in st["reason"]
    assert not T.library_loaded()

    r = client.post("/api/rules/pack/accept",
                    json=_body(art, sig, sha256=offered["sha256"]), headers=h)
    assert r.status_code == 200, r.text
    st = client.get("/api/rules/pack", headers=h).json()
    assert st["installed"] and st["loaded"] and st["sha256"] == offered["sha256"]
    assert st["version"] == "1.0.0" and st["installed_by"] and st["installed_at"]
    assert len(T.LIBRARY) == len(offered["rules"]["added"])

    from app.db import SessionLocal
    from app.models import Event
    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.action == "install_rule_pack").all()
        assert len(ev) == 1
        assert ev[0].meta["old_version"] is None and ev[0].meta["new_version"] == "1.0.0"
        assert ev[0].meta["sha256"] == offered["sha256"]
    finally:
        db.close()

    # Restart: process state gone, the table remains; the app's own startup reloads it.
    _restart()
    assert P.CURRENT is not None and P.CURRENT.version == "1.0.0"
    assert len(T.LIBRARY) == len(offered["rules"]["added"])
    st = client.get("/api/rules/pack", headers=h).json()
    assert st["loaded"] and st["sha256"] == offered["sha256"]


def test_a_worker_started_before_the_accept_catches_up_on_its_next_read(client, trusted):
    h = _login(client)
    art, sig = build(FIX / "api1")
    client.post("/api/rules/pack/accept", json=_body(art, sig, sha256=P.sha256_of(art)),
                headers=h)
    _forget()  # this worker never saw the accept
    t = client.get("/api/rules/toggles", params={"project_id": "core"}, headers=h).json()
    assert t["library_loaded"] is True and P.CURRENT_SHA == P.sha256_of(art)


def test_accept_with_a_different_artifact_is_refused(client, trusted):
    h = _login(client)
    art, sig = build(FIX / "api1")
    other, osig = build(FIX / "api1", {"pack.toml": (FIX / "api1/pack.toml").read_text()
                                       .replace('version = "1.0.0"', 'version = "1.0.1"')})
    r = client.post("/api/rules/pack/accept",
                    json=_body(other, osig, sha256=P.sha256_of(art)), headers=h)
    assert r.status_code == 409
    assert client.get("/api/rules/pack", headers=h).json()["installed"] is False


def test_tampered_artifact_is_refused_and_previous_pack_stays(client, trusted):
    h = _login(client)
    art, sig = build(FIX / "api1")
    assert client.post("/api/rules/pack/accept",
                       json=_body(art, sig, sha256=P.sha256_of(art)), headers=h).status_code == 200
    bad = art[:-1] + bytes([art[-1] ^ 1])
    msg = "pack signature does not verify against any trusted key"
    r = client.post("/api/rules/pack/offer", json=_body(bad, sig), headers=h)
    assert r.status_code == 422 and msg in r.text
    r = client.post("/api/rules/pack/accept",
                    json=_body(bad, sig, sha256=P.sha256_of(bad)), headers=h)
    assert r.status_code == 422 and msg in r.text
    st = client.get("/api/rules/pack", headers=h).json()
    assert st["sha256"] == P.sha256_of(art) and st["loaded"]


def test_agent_key_cannot_offer_or_accept(client, trusted):
    h = _login(client)
    pid = client.get("/api/projects", headers=h).json()[0]["id"]
    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid},
                      headers=h).json()["plaintext"]
    art, sig = build(FIX / "api1")
    k = {"X-API-Key": key}
    assert client.post("/api/rules/pack/offer", json=_body(art, sig),
                       headers=k).status_code == 401
    assert client.post("/api/rules/pack/accept",
                       json=_body(art, sig, sha256=P.sha256_of(art)),
                       headers=k).status_code == 401
    db, _ = _person()
    try:
        from app.models import ApiKey
        ak = db.query(ApiKey).first()
        for fn in (lambda: P.offer_pack(db, ak, art, sig, trusted_keys=TRUST),
                   lambda: P.accept(db, ak, art, sig, P.sha256_of(art), trusted_keys=TRUST)):
            with pytest.raises(Exception) as e:
                fn()
            assert getattr(e.value, "rule_id", None) == T.PERSON_RULE
    finally:
        db.close()
    assert client.get("/api/rules/pack", headers=h).json()["installed"] is False


def test_persisted_pack_that_no_longer_verifies_is_refused_with_its_reason(client, trusted,
                                                                         monkeypatch):
    h = _login(client)
    art, sig = build(FIX / "api1")
    client.post("/api/rules/pack/accept", json=_body(art, sig, sha256=P.sha256_of(art)),
                headers=h)
    _forget()
    monkeypatch.setattr(P, "TRUSTED_PUBLIC_KEYS", ("00" * 32,))
    db, _ = _person()
    try:
        assert P.reload_persisted(db) is None
    finally:
        db.close()
    assert not T.library_loaded()
    st = client.get("/api/rules/pack", headers=h).json()
    assert st["installed"] and not st["loaded"] and "does not verify" in st["reason"]
    t = client.get("/api/rules/toggles", headers=h).json()
    assert t["library_loaded"] is False and "does not verify" in t["library_reason"]
