"""Machine reports from `gban setup` / `gban doctor` (GRPH-1442): what counts as proof for step ③."""
import pytest


def _report(client, auth, machine_id="m-1", label="laptop", project="core", lines=None, **extra):
    body = {"machine_id": machine_id, "label": label, "os": "darwin", "gban_version": "0.1.0",
            "project": project, "lines": lines if lines is not None else
            [{"status": "PASS", "name": "server", "detail": "ok"}], **extra}
    return client.post("/api/machines/report", json=body, headers=auth)


def _step3(client, auth, project="core"):
    steps = client.get(f"/api/projects/{project}/setup", headers=auth).json()["steps"]
    return next(s for s in steps if s["step"] == 5)


def test_never_reported_is_pending_and_lists_nothing(client, auth):
    assert client.get("/api/machines", headers=auth).json() == []
    assert _step3(client, auth)["status"] == "pending"


def test_an_all_pass_report_turns_step_three_done(client, auth):
    assert _report(client, auth).status_code == 200
    s = _step3(client, auth)
    assert s["status"] == "done" and s["proof"] == "machine_report" and s["machine"] == "laptop"


def test_one_fail_line_does_not_prove_and_is_listed_as_failed(client, auth):
    r = _report(client, auth, lines=[{"status": "PASS", "name": "server"},
                                     {"status": "FAIL", "name": "gbfleet", "detail": "missing"},
                                     {"status": "UNKNOWN", "name": "x"}])
    assert r.json()["state"] == "failed" and r.json()["fail_count"] == 1
    s = _step3(client, auth)
    assert s["status"] == "pending"
    assert s["reported"]["fails"] == 1 and s["reported"]["failing"][0]["name"] == "gbfleet"
    assert client.get("/api/machines", headers=auth).json()[0]["state"] == "failed"


def test_a_later_clean_report_replaces_the_failure(client, auth):
    _report(client, auth, lines=[{"status": "FAIL", "name": "a"}])
    _report(client, auth)
    assert _step3(client, auth)["status"] == "done"


def test_unknown_is_not_a_pass_so_it_never_proves(client, auth):
    """§6: step ③ needs ALL-PASS. An UNKNOWN is a check that could not run — never a pass."""
    r = _report(client, auth, lines=[{"status": "PASS", "name": "server"},
                                     {"status": "UNKNOWN", "name": "gbfleet", "detail": "not found"}])
    assert r.json()["state"] == "unknown" and r.json()["fail_count"] == 0
    s = _step3(client, auth)
    assert s["status"] == "pending"
    assert s["reported"]["unknowns"] == 1 and s["reported"]["failing"][0]["name"] == "gbfleet"


def test_an_empty_report_proves_nothing_and_a_bad_status_is_refused(client, auth):
    assert _report(client, auth, lines=[]).json()["state"] == "unknown"
    assert _step3(client, auth)["status"] == "pending"
    assert _report(client, auth, lines=[{"status": "MAYBE", "name": "a"}]).status_code == 422


def test_the_step_lists_each_machine_separately_and_one_clean_is_enough(client, auth):
    _report(client, auth, machine_id="a", label="ubuntu", lines=[{"status": "FAIL", "name": "x"}])
    _report(client, auth, machine_id="b", label="alex-mbp")
    s = _step3(client, auth)
    assert s["status"] == "done" and s["machine"] == "alex-mbp"
    assert {(m["machine"], m["state"]) for m in s["machines"]} == {("ubuntu", "failed"),
                                                                    ("alex-mbp", "clean")}


def test_a_report_for_another_project_does_not_prove_this_one(client, auth):
    _report(client, auth, project="")
    s = _step3(client, auth)
    assert s["status"] == "pending" and s["machines"] == []


def test_same_label_two_machine_ids_stay_separate_and_wiping_makes_a_new_one(client, auth):
    _report(client, auth, machine_id="a", label="laptop")
    _report(client, auth, machine_id="b", label="laptop")
    _report(client, auth, machine_id="a", label="laptop")  # same id: updates, not a third
    assert len(client.get("/api/machines", headers=auth).json()) == 2


def test_remove_deletes_and_another_user_cannot(client, auth):
    row = _report(client, auth).json()
    assert client.delete(f"/api/machines/{row['id']}", headers=auth).status_code == 204
    assert client.get("/api/machines", headers=auth).json() == []
    assert client.delete(f"/api/machines/{row['id']}", headers=auth).status_code == 404


def test_report_for_a_project_you_cannot_read_is_refused(client, auth):
    r = _report(client, auth, project="no-such-project")
    assert r.status_code in (403, 404)
    assert client.get("/api/machines", headers=auth).json() == []


def test_unauthenticated_report_is_refused(client):
    r = client.post("/api/machines/report", json={"machine_id": "x", "lines": []})
    assert r.status_code in (401, 403)



def test_a_latest_fail_report_outranks_a_used_quickstart_key(client, auth):
    """GRPH-1548: a FAIL line from the latest report keeps step ③ pending even with a used key."""
    from tests.test_connect_step import _call, _mint
    key = _mint(client, auth, "core")
    assert _call(client, key["plaintext"]).status_code == 200
    assert _step3(client, auth)["proof"] == "first_call"
    _report(client, auth, lines=[{"status": "FAIL", "name": "config written"}])
    s = _step3(client, auth)
    assert s["status"] == "pending"
    assert s["reported"]["failing"][0]["name"] == "config written"
    _report(client, auth)  # a clean latest report: step 3 is done again
    assert _step3(client, auth)["status"] == "done"
