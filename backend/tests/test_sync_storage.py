"""GRPH-1320: sync storage on Specs — folder path, blob directory, index; row sync states."""
import hashlib

from app.config import settings
from app.db import SessionLocal
from app.models import Prd
from app.services import documents as docs_svc
from app.services import drive_sync
from app.services import prds as prd_svc

URL = "/api/platform/sync-storage"


def _project(client, auth):
    client.post("/api/projects", json={"name": "Sync"}, headers=auth)  # id: sync


def _prd(title, body, status=None):
    db = SessionLocal()
    prd = prd_svc.create_prd(db, title=title, project_id="sync", body=body)
    if status:
        db.get(Prd, prd.id).status = status
        db.commit()
    pid = prd.id
    db.close()
    return pid


def _configure(client, auth, folder, blobs=None):
    body = {"folder_path": str(folder)}
    if blobs is not None:
        body["blob_dir"] = str(blobs)
    r = client.put(f"{URL}?project_id=sync", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(out):
    return {r["prd_id"]: r for r in out["rows"]}


def test_three_facts_and_rows_after_configuring(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("One", "# One\n\nbody\n")
    (tmp_path / "blobs").mkdir()
    out = _configure(client, auth, tmp_path, tmp_path / "blobs")
    assert out["folder"]["ok"] and out["folder"]["configured"]
    assert out["folder"]["path"] == str(tmp_path)
    assert out["folder"]["project_path"] == str(tmp_path / "sync")
    assert out["blob_dir"]["ok"] and out["blob_dir"]["path"] == str(tmp_path / "blobs")
    assert out["index"]["available"] is True and out["index"]["reason"] == ""
    assert _rows(out)[pid]["sync_state"] == "not_exported"
    # A GET is a read: nothing was written into the folder.
    assert not (tmp_path / "sync").exists()


def test_relative_path_is_refused(client, auth):
    _project(client, auth)
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": "relative/dir"}, headers=auth)
    assert r.status_code == 422 and "absolute" in r.text


def test_unreadable_folder_is_a_failure_on_that_fact_and_keeps_rows(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Kept", "# Kept\n\nbody\n")
    missing = tmp_path / "not-mounted"
    out = _configure(client, auth, missing)
    # Saved anyway, shown as a failure on the folder fact only.
    assert out["folder"]["path"] == str(missing)
    assert out["folder"]["ok"] is False and "does not exist" in out["folder"]["error"]
    assert out["index"]["available"] is True
    assert _rows(out)[pid]["sync_state"] == "unknown"
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"] is None and ran["last_run"]["export_status"] == "not_run"
    assert str(missing) in ran["last_run"]["error"]
    assert _rows(ran)[pid]["title"] == "Kept"
    assert not missing.exists()


def test_run_exports_then_in_sync_and_approved_file_edit_is_a_conflict(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Gov", "# Gov\n\noriginal\n")
    _configure(client, auth, tmp_path)
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert pid in ran["run"]["exported"] and ran["last_run"]["export_status"] == "ok"
    assert ran["last_run"]["at"].endswith("+00:00")  # an instant, not a local wall time
    assert _rows(ran)[pid]["sync_state"] == "in_sync"

    db = SessionLocal()
    db.get(Prd, pid).status = "approved"
    db.commit()
    db.close()
    fp = next((tmp_path / "sync" / "PRDs").glob("*.md"))
    fp.write_text(fp.read_text().replace("original", "folder edit"))

    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert _rows(out)[pid]["sync_state"] == "conflict"
    (c,) = out["report"]["conflict_details"]
    assert c["prd_id"] == pid and c["ledger_hash"] != c["file_hash"]
    assert "folder edit" not in str(out) and "original" not in str(c)

    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["conflicts"] == [pid] and ran["run"]["updated_db"] == []
    db = SessionLocal()
    assert "original" in db.get(Prd, pid).body
    db.close()


def test_corrupt_file_is_a_read_failure_not_a_conflict_or_blank(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Bytes", "# Bytes\n\nthe ledger body\n")
    _configure(client, auth, tmp_path)
    client.post(f"{URL}/run?project_id=sync", headers=auth)
    fp = next((tmp_path / "sync" / "PRDs").glob("*.md"))
    corrupt = b"\xff\xfe\x00garbage\x80"
    fp.write_bytes(corrupt)
    stray = tmp_path / "sync" / "PRDs" / "stray.md"
    stray.write_bytes(b"\xc3\x28 not utf-8")

    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    row = _rows(out)[pid]
    assert row["sync_state"] == "read_failed" and "UTF-8" in row["error"]
    assert out["report"]["conflicts"] == []
    names = {f["file_name"]: f for f in out["report"]["read_failures"]}
    assert names[fp.name]["prd_id"] == pid and names["stray.md"]["prd_id"] is None

    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["conflicts"] == [] and ran["run"]["imported"] == []
    assert ran["run"]["exported"] == [] and ran["run"]["updated_file"] == []
    assert fp.read_bytes() == corrupt  # not exported over
    db = SessionLocal()
    assert db.get(Prd, pid).body == "# Bytes\n\nthe ledger body\n"  # not blanked
    assert db.query(Prd).filter(Prd.project_id == "sync").count() == 1  # stray not imported
    db.close()


def test_empty_body_whose_hash_matches_is_in_sync(client, auth, tmp_path):
    _project(client, auth)
    pid = _prd("Empty", "")
    _configure(client, auth, tmp_path)
    client.post(f"{URL}/run?project_id=sync", headers=auth)
    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert _rows(out)[pid]["sync_state"] == "in_sync"
    assert out["report"]["read_failures"] == [] and out["report"]["conflicts"] == []


def test_failed_export_does_not_roll_the_ledger_back(client, auth, tmp_path, monkeypatch):
    _project(client, auth)
    pid = _prd("Fail", "# Fail\n\nbody\n")
    _configure(client, auth, tmp_path)

    def broken(self, subdir, name, content):
        raise OSError("drive offline")

    monkeypatch.setattr(drive_sync.LocalFolderBackend, "write", broken)
    ran = client.post(f"{URL}/run?project_id=sync", headers=auth).json()
    assert ran["run"]["export_status"] == "failed" and ran["run"]["conflicts"] == []
    assert ran["last_run"]["export_status"] == "failed"
    assert _rows(ran)[pid]["sync_state"] == "not_exported"
    db = SessionLocal()
    assert db.get(Prd, pid).body == "# Fail\n\nbody\n"
    db.close()


def test_blob_directory_is_where_bytes_are_stored(client, auth, tmp_path):
    _project(client, auth)
    blobs = tmp_path / "my-blobs"
    blobs.mkdir()
    _configure(client, auth, tmp_path, blobs)
    data = b"\x89PNG canvas bytes"
    db = SessionLocal()
    docs_svc.put_blob(db, project_id="sync", kind="canvas", data=data, mime="image/png",
                      set_id="S1", screen="home")
    key = hashlib.sha256(data).hexdigest()
    assert (blobs / key[:2] / key).read_bytes() == data
    db.close()


def test_hosted_box_refuses_paths(client, auth, tmp_path, monkeypatch):
    _project(client, auth)
    monkeypatch.setattr(settings, "hosted_mode", True)
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": str(tmp_path)}, headers=auth)
    assert r.status_code in (400, 403, 404)  # authz answers first; then the hosted refusal
    monkeypatch.setattr(settings, "hosted_mode", False)
    out = client.get(f"{URL}?project_id=sync", headers=auth).json()
    assert out["folder"]["configured"] is False  # nothing was saved


def test_hosted_refusal_after_authz(client, auth, tmp_path, monkeypatch):
    from app.routers import platform as platform_router

    _project(client, auth)
    monkeypatch.setattr(platform_router.settings, "hosted_mode", False)
    monkeypatch.setattr(platform_router, "_require_self_hosted_box",
                        lambda: (_ for _ in ()).throw(platform_router.HTTPException(400, "self-hosted")))
    r = client.put(f"{URL}?project_id=sync", json={"folder_path": str(tmp_path)}, headers=auth)
    assert r.status_code == 400 and "self-hosted" in r.text


# ---- publish destination (GRPH-1761) -------------------------------------------------------

DEST = "/api/sync/destination"


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _org(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "hosted_mode", True)
    org = client.post("/api/orgs", json={"name": "Acme"}, headers=auth).json()
    return org


def _seat(org_id, user_id, role):
    from app.models import OrgMembership

    db = SessionLocal()
    db.add(OrgMembership(org_id=org_id, user_id=user_id, role=role))
    db.commit()
    db.close()


def _fact(client, auth, project_id="core"):
    r = client.get(f"{URL}?project_id={project_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["destination"]


def test_org_admin_saves_url_and_branch_and_a_linked_box_shows_them(client, auth, monkeypatch):
    org = _org(client, auth, monkeypatch)
    r = client.patch(f"/api/orgs/{org['id']}/destination",
                     json={"url": "github.com/acme/app", "branch": "policy"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "configured"
    assert r.json()["url"] == "github.com/acme/app" and r.json()["branch"] == "policy"

    monkeypatch.setattr(settings, "hosted_mode", False)
    assert client.post("/api/sync/link",
                       json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"},
                       headers=auth).status_code == 200

    def fetch(url, key):
        assert key == "gb_sk_x" and url.startswith("https://cloud.example")
        return _Resp(200, {"url": "github.com/acme/app", "branch": "policy"})

    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination", fetch)
    pulled = client.post(f"{DEST}/pull", headers=auth)
    assert pulled.status_code == 200, pulled.text
    body = pulled.json()
    assert body["url"] == "github.com/acme/app" and body["branch"] == "policy"
    assert body["source"] == "org" and body["state"] == "configured"
    shown = _fact(client, auth)
    assert shown["url"] == "github.com/acme/app" and shown["branch"] == "policy"


def test_member_and_viewer_can_read_and_cannot_save(client, auth, monkeypatch):
    org = _org(client, auth, monkeypatch)
    client.patch(f"/api/orgs/{org['id']}/destination",
                 json={"url": "github.com/acme/app", "branch": "policy"}, headers=auth)
    _seat(org["id"], "u2", "member")
    _seat(org["id"], "u4", "viewer")
    dana = _login(client, "dana@ascme-labs.com")
    kate = _login(client, "kate@ascme-labs.com")
    for headers in (dana, kate):
        seen = client.get(f"/api/orgs/{org['id']}/destination", headers=headers)
        assert seen.status_code == 200, seen.text
        assert seen.json()["url"] == "github.com/acme/app"
        refused = client.patch(f"/api/orgs/{org['id']}/destination",
                               json={"url": "github.com/other/app", "branch": "elsewhere"},
                               headers=headers)
        assert refused.status_code == 403, refused.text
    stored = client.get(f"/api/orgs/{org['id']}/destination", headers=auth).json()
    assert stored["url"] == "github.com/acme/app" and stored["branch"] == "policy"


def test_unlinked_admin_saves_a_path_and_a_second_project_reads_it(client, auth, tmp_path):
    other = client.post("/api/projects", json={"name": "Other Box"}, headers=auth).json()["id"]
    r = client.put(DEST, json={"path": str(tmp_path), "branch": "policy"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "configured" and r.json()["source"] == "local"
    assert r.json()["path"] == str(tmp_path) and r.json()["branch"] == "policy"
    first = _fact(client, auth, "core")
    second = _fact(client, auth, other)
    assert first["path"] == second["path"] == str(tmp_path)
    assert first["branch"] == second["branch"] == "policy"


def test_empty_field_is_unconfigured_and_no_commit_is_attempted(client, auth, tmp_path, monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    r = client.put(DEST, json={"path": str(tmp_path), "branch": ""}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "unconfigured"
    from app.services import sync_storage
    db = SessionLocal()
    assert sync_storage.publish_target(db) is None
    db.close()
    assert calls == []


def test_linking_keeps_the_old_path_as_previous_and_publishes_the_remote(client, auth, tmp_path,
                                                                        monkeypatch):
    client.put(DEST, json={"path": str(tmp_path), "branch": "local"}, headers=auth)
    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    # The link itself turned publishing off, before any pull.
    assert _fact(client, auth)["publishing"] is False

    def fetch(url, key):
        return _Resp(200, {"url": "github.com/acme/app", "branch": "policy"})

    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination", fetch)
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["previous"]["path"] == str(tmp_path)
    assert body["path"] == "" and body["url"] == "github.com/acme/app"
    assert body["publishing"] is False
    from app.services import sync_storage
    db = SessionLocal()
    target = sync_storage.publish_target(db)
    db.close()
    assert target == {"kind": "remote", "url": "github.com/acme/app", "branch": "policy"}
    assert str(tmp_path) not in target.values()


def test_unlinking_clears_the_org_pair_and_does_not_restore_the_path(client, auth, tmp_path,
                                                                    monkeypatch):
    client.put(DEST, json={"path": str(tmp_path), "branch": "local"}, headers=auth)
    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination",
                        lambda url, key: _Resp(200, {"url": "github.com/acme/app", "branch": "policy"}))
    client.post(f"{DEST}/pull", headers=auth)
    assert client.delete("/api/sync/link", headers=auth).status_code == 200
    body = _fact(client, auth)
    assert body["url"] == "" and body["branch"] == ""
    assert body["state"] == "unconfigured"
    assert body["path"] == ""  # the previous path is not put back
    assert body["previous"]["path"] == str(tmp_path)
    assert body["publishing"] is False
    from app.services import sync_storage
    db = SessionLocal()
    assert sync_storage.publish_target(db) is None
    db.close()


def test_a_url_equal_to_a_project_code_remote_is_allowed_and_reported(client, auth, monkeypatch):
    from app.models import Project

    org = _org(client, auth, monkeypatch)
    db = SessionLocal()
    db.get(Project, "core").repo_url = "github.com/acme/app"
    db.get(Project, "core").org_id = org["id"]
    db.commit()
    db.close()
    saved = client.patch(f"/api/orgs/{org['id']}/destination",
                         json={"url": "github.com/acme/app", "branch": "policy"}, headers=auth)
    assert saved.status_code == 200, saved.text
    match = saved.json()["code_remote_match"]
    assert match["checked"] is True
    assert {"project_id": "core", "repo_url": "github.com/acme/app"} in match["projects"]

    monkeypatch.setattr(settings, "hosted_mode", False)
    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination",
                        lambda url, key: _Resp(200, {"url": "github.com/acme/app", "branch": "policy"}))
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["code_remote_match"]["checked"] is True
    assert {"project_id": "core", "repo_url": "github.com/acme/app"} in body["code_remote_match"]["projects"]


def test_org_branch_change_turns_publishing_off_and_does_not_touch_git(client, auth, monkeypatch):
    import subprocess

    from app.models import SyncStorageConfig
    from app.services.sync_storage import SINGLETON_ID

    org = _org(client, auth, monkeypatch)
    client.patch(f"/api/orgs/{org['id']}/destination",
                 json={"url": "github.com/acme/app", "branch": "policy"}, headers=auth)
    monkeypatch.setattr(settings, "hosted_mode", False)
    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    db = SessionLocal()
    row = db.get(SyncStorageConfig, SINGLETON_ID)
    row.destination_source = "org"
    row.received_url = "github.com/acme/app"
    row.received_branch = "policy"
    row.publish_enabled = True
    db.commit()
    db.close()
    monkeypatch.setattr(settings, "hosted_mode", True)
    changed = client.patch(f"/api/orgs/{org['id']}/destination",
                           json={"branch": "policy-2026"}, headers=auth)
    assert changed.status_code == 200 and changed.json()["branch"] == "policy-2026"
    monkeypatch.setattr(settings, "hosted_mode", False)

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))

    def fetch(url, key):
        from app.models import Organization
        s = SessionLocal()
        try:
            saved = s.get(Organization, org["id"])
            return _Resp(200, {"url": saved.publish_repo_url, "branch": saved.publish_branch})
        finally:
            s.close()

    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination", fetch)
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["branch"] == "policy-2026" and body["url"] == "github.com/acme/app"
    assert body["publishing"] is False
    assert calls == []


def test_a_pull_with_no_answer_leaves_the_destination_and_publishing(client, auth, monkeypatch):
    import httpx

    from app.models import SyncStorageConfig
    from app.services.sync_storage import SINGLETON_ID

    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    db = SessionLocal()
    row = db.get(SyncStorageConfig, SINGLETON_ID)
    row.destination_source = "org"
    row.received_url = "github.com/acme/app"
    row.received_branch = "policy"
    row.publish_enabled = True
    db.commit()
    db.close()

    def fetch(url, key):
        raise httpx.ConnectError("timed out")

    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination", fetch)
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["url"] == "github.com/acme/app" and body["branch"] == "policy"
    assert body["publishing"] is True
    assert body["pull"]["state"] == "unknown" and "timed out" in body["pull"]["reason"]


def test_a_refused_pull_does_not_clear_the_destination(client, auth, monkeypatch):
    from app.models import SyncStorageConfig
    from app.services.sync_storage import SINGLETON_ID

    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    db = SessionLocal()
    row = db.get(SyncStorageConfig, SINGLETON_ID)
    row.destination_source = "org"
    row.received_url = "github.com/acme/app"
    row.received_branch = "policy"
    row.publish_enabled = True
    db.commit()
    db.close()
    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination",
                        lambda url, key: _Resp(404, {"detail": "missing"}))
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["url"] == "github.com/acme/app" and body["branch"] == "policy"
    assert body["publishing"] is True
    assert body["pull"]["state"] == "refused"


def test_the_same_pair_pulled_again_does_not_turn_publishing_off(client, auth, monkeypatch):
    from app.models import SyncStorageConfig
    from app.services.sync_storage import SINGLETON_ID

    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    db = SessionLocal()
    row = db.get(SyncStorageConfig, SINGLETON_ID)
    row.destination_source = "org"
    row.received_url = "github.com/acme/app"
    row.received_branch = "policy"
    row.publish_enabled = True
    db.commit()
    db.close()
    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination",
                        lambda url, key: _Resp(200, {"url": "github.com/acme/app", "branch": "policy"}))
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["publishing"] is True
    assert body["pull"]["state"] == "ok"


def test_a_non_admin_sees_the_destination_and_cannot_save(client, auth, tmp_path):
    # Kate is a project member, not a box admin (Dana is: the operator made her admin of core).
    client.put(DEST, json={"path": str(tmp_path), "branch": "policy"}, headers=auth)
    kate = _login(client, "kate@ascme-labs.com")
    seen = client.get(f"{URL}?project_id=core", headers=kate)
    assert seen.status_code == 200, seen.text
    assert seen.json()["destination"]["path"] == str(tmp_path)
    assert seen.json()["destination"]["editable"] is False
    refused = client.put(DEST, json={"path": "/tmp/elsewhere", "branch": "nope"}, headers=kate)
    assert refused.status_code == 403
    assert _fact(client, auth)["path"] == str(tmp_path)
    assert _fact(client, auth)["branch"] == "policy"


def test_a_linked_box_cannot_save_a_local_destination(client, auth, tmp_path):
    client.put(DEST, json={"path": str(tmp_path), "branch": "policy"}, headers=auth)
    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    refused = client.put(DEST, json={"path": "/tmp/elsewhere", "branch": "nope"}, headers=auth)
    assert refused.status_code == 403
    assert "the org admin owns the destination" in refused.text
    assert refused.json()["rule_id"] == "sync.destination_owned_by_org"
    body = _fact(client, auth)
    assert body["path"] == str(tmp_path) and body["branch"] == "policy"
    assert body["editable"] is False


def test_cloud_destination_is_the_org_pair_for_a_sync_key(client, auth, monkeypatch):
    from app.models import Project

    org = _org(client, auth, monkeypatch)
    client.patch(f"/api/orgs/{org['id']}/destination",
                 json={"url": "github.com/acme/app", "branch": "policy"}, headers=auth)
    db = SessionLocal()
    db.get(Project, "core").org_id = org["id"]
    db.commit()
    db.close()
    monkeypatch.setattr(settings, "hosted_mode", False)
    key = client.post("/api/api-keys",
                      json={"name": "box", "scopes": ["sync"], "project_id": "core"},
                      headers=auth).json()["plaintext"]
    got = client.get(DEST, headers={"X-API-Key": key})
    assert got.status_code == 200, got.text
    assert got.json() == {"url": "github.com/acme/app", "branch": "policy"}


def test_a_server_error_is_unknown_and_leaves_the_pair(client, auth, monkeypatch):
    from app.models import SyncStorageConfig
    from app.services.sync_storage import SINGLETON_ID

    client.post("/api/sync/link",
                json={"cloud_url": "https://cloud.example", "api_key": "gb_sk_x"}, headers=auth)
    db = SessionLocal()
    row = db.get(SyncStorageConfig, SINGLETON_ID)
    row.destination_source = "org"
    row.received_url = "github.com/acme/app"
    row.received_branch = "policy"
    row.publish_enabled = True
    db.commit()
    db.close()
    monkeypatch.setattr("app.services.sync_storage.fetch_org_destination",
                        lambda url, key: _Resp(503, {}))
    body = client.post(f"{DEST}/pull", headers=auth).json()
    assert body["url"] == "github.com/acme/app" and body["publishing"] is True
    assert body["pull"]["state"] == "unknown" and body["pull"]["reason"]
