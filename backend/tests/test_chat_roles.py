"""Per-task chat roles (GRPH-316).

Unset inherits the project pointer. A role that names an unusable credential is
stub/`role_unusable`, not a silent fall back to the project's model.
"""
import pytest

from app.models import Credential, MemoryShard, Project
from app.security import secrets
from app.services import platform as platform_svc



@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def project(db):
    p = Project(id="p1", name="P1", tag="P1")
    db.add(p)
    db.commit()
    return p


def _credential(db, cid="cred_1", *, kind="anthropic", model="claude-x",
                state="valid", key="sk-live"):
    c = Credential(id=cid, kind=kind, org_id=None, model=model,
                   api_key=secrets.encrypt(key), label=cid, state=state)
    db.add(c)
    db.commit()
    return c


def test_unknown_role_is_refused(db, project):
    with pytest.raises(ValueError, match="unknown chat role"):
        platform_svc.resolve_role(db, "p1", "not.a.role")


def test_unset_role_inherits_the_project_pointer(db, project):
    cred = _credential(db, "cred_p", model="claude-project")
    project.credential_id = cred.id
    db.commit()
    inherited = platform_svc.resolve_chat(db, "p1")
    role = platform_svc.resolve_role(db, "p1", "memory.judge")
    assert role.credential_id == inherited.credential_id
    assert role.source == inherited.source


def test_a_role_override_uses_that_credential(db, project):
    project_cred = _credential(db, "cred_p", model="claude-project")
    judge_cred = _credential(db, "cred_j", kind="ollama", model="gpt-oss:20b")
    project.credential_id = project_cred.id
    db.commit()
    platform_svc.set_project_roles(db, "p1", {
        "memory.judge": {"credential_id": judge_cred.id},
    })
    role = platform_svc.resolve_role(db, "p1", "memory.judge")
    chat = platform_svc.resolve_chat(db, "p1")
    assert role.source == "role"
    assert role.credential_id == "cred_j"
    assert role.model == "gpt-oss:20b"
    assert chat.credential_id == "cred_p"


def test_an_unusable_role_credential_is_not_a_weaker_grade(db, project):
    """The grill's rule: record nothing rather than grade with a weaker bar."""
    project_cred = _credential(db, "cred_p", model="claude-project")
    broken = _credential(db, "cred_dead", model="claude-dead", state="unreachable")
    project.credential_id = project_cred.id
    db.commit()
    platform_svc.set_project_roles(db, "p1", {
        "memory.judge": {"credential_id": broken.id},
    })
    role = platform_svc.resolve_role(db, "p1", "memory.judge")
    assert role.provider_id == "stub"
    assert role.source == "role_unusable"
    assert role.fell_back_from == "cred_dead"
    chat = platform_svc.resolve_chat(db, "p1")
    assert chat.credential_id == "cred_p"


def test_review_judge_asks_the_memory_judge_role(db, monkeypatch):
    """Sabotage the CALL: a judge that still hits resolve_chat would ignore the role."""
    from app.services import memory as mem_svc
    from app.services import platform as plat

    called = {"role": None}
    real = plat.resolve_role

    def wrap(db, project_id, role):
        called["role"] = role
        return real(db, project_id, role)

    monkeypatch.setattr(plat, "resolve_role", wrap)
    shard = MemoryShard(id="s_role", project_id="core", text="always pin pg16")
    db.add(shard)
    db.commit()
    mem_svc.review_judge(db, shard)
    assert called["role"] == "memory.judge"


def test_put_roles_round_trips(client, auth):
    r = client.put("/api/platform/credentials/roles?project_id=core",
                   json={"roles": {}}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["chat_roles"] == {}
    assert "memory.judge" in body["known_roles"]


def test_put_unknown_role_is_422(client, auth):
    r = client.put("/api/platform/credentials/roles?project_id=core",
                   json={"roles": {"not.a.role": {"model_override": "x"}}},
                   headers=auth)
    assert r.status_code == 422
